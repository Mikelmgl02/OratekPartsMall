"""Pedir lo que falta a otro proveedor: the units an order will not bring, for the client to ask another supplier in its cesta.

What is missing of each item it asked for: what the current quotation does not offer (quoted, adjustment); once confirmed, what the deal
will not deliver (the agreement less the shortfalls the client accepted or still has to answer); all of it when the order or deal ended
cancelled. Before a quotation nothing is known yet.

GET accounts/<client>/sent-requests/<order>/missing/: each missing item with the other suppliers that have its SKU in stock now, as the
catalog lists them (in_stock only, never quantities; the order's supplier and the account itself left out), a suggested article (the same
brand first, then a supplier the account already ordered from, then by name) and how many units of its SKU the person's cesta already has.
POST {lines: [{order_line_id, supplier_item_id, quantity}]}: adds them to the person's cesta, all or none (mall.basket.apply_changes, source
'faltante', each in the cesta's ledger with the order's reference); an article gone out of stock since refuses it (409).
"""
from django.db import IntegrityError
from django.db.models import F, Sum
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .basket import MAX_QUANTITY, apply_changes, basket_data
from .basket_models import BasketEvent, BasketItem
from .deal_lifecycle import agreement
from .lines import canonical, storefront_q
from .models import SupplierItem
from .request_models import SupplierRequest
from .request_views import RequestConflict
from .views import account_for


def current_part(part):
    """The SKU an article is listed under now: a part merged since points at the one it went into (mall.lines.canonical)."""
    return canonical(part)


def missing_lines(order):
    """[(order line, units it will not bring)] for the items the order falls short on."""
    if order.status == 'cancelled':
        coming = {}
    elif order.handshaked_at:
        current = agreement(order)
        coming = {entry['line'].order_line_id: entry['final'] - entry['pending'] for entry in current['lines']} if current else {}
    else:
        latest = order.quotations.order_by('-revision').first()
        if latest is None:
            return []
        coming = {line.order_line_id: line.quantity for line in latest.lines.all()}
    return [(line, line.quantity - max(0, coming.get(line.pk, 0))) for line in order.lines.select_related('part__merged_into').order_by('sku', 'codigo', 'id')
            if line.quantity > max(0, coming.get(line.pk, 0))]


def offers_for(order, account, parts):
    """{part id: [SupplierItem]} in stock now for these SKUs, from requestable articles of active suppliers other than the order's and the
    account's own (the catalog's offers)."""
    items = (SupplierItem.objects.filter(storefront_q('part__'), part_id__in=parts, matching_status='matched',
                                         supplier__active=True, supplier__roles__capability='supplier', reported_quantity__gt=F('reserved_quantity'))
             .exclude(supplier_id__in=[order.supplier_id, account.pk]).select_related('supplier').distinct())
    found = {}
    for item in items:
        found.setdefault(item.part_id, []).append(item)
    return found


def missing_data(user, account, order):
    lines = missing_lines(order)
    parts = {line.pk: current_part(line.part) for line, _ in lines}
    offers = offers_for(order, account, {part.pk for part in parts.values()})
    known = set(SupplierRequest.objects.filter(client=account).values_list('supplier_id', flat=True))
    in_basket = dict(BasketItem.objects.filter(user=user, account=account, part_id__in={part.pk for part in parts.values()}).values('part_id')
                     .annotate(units=Sum('quantity')).values_list('part_id', 'units'))
    rows = []
    for line, units in lines:
        part = parts[line.pk]
        ranked = sorted(offers.get(part.pk, []), key=lambda item: (item.brand != line.brand, item.supplier_id not in known, item.supplier.name,
                                                                   item.codigo, str(item.pk)))
        suppliers = {}
        for item in ranked:  # grouped by supplier, the best first
            suppliers.setdefault(item.supplier_id, {'supplier_id': str(item.supplier_id), 'supplier_name': item.supplier.name, 'known': item.supplier_id in known,
                                                    'items': []})['items'].append({'id': str(item.pk), 'codigo': item.codigo, 'brand': item.brand,
                                                                                   'description': item.description})
        rows.append({'order_line_id': str(line.pk), 'part_id': str(part.pk), 'sku': part.sku, 'codigo': line.codigo, 'brand': line.brand,
                     'description': line.description or line.name, 'requested': line.quantity, 'missing': units, 'in_basket': in_basket.get(part.pk, 0),
                     'suggested': str(ranked[0].pk) if ranked else None, 'offers': list(suppliers.values())})
    previous = BasketEvent.objects.filter(user=user, account=account, source='faltante', reference=order.reference).order_by('-id').first()
    return {'order': {'id': str(order.pk), 'reference': order.reference, 'status': order.status, 'supplier_name': order.supplier_name},
            'lines': rows, 'added_at': previous.created_at.isoformat() if previous else None}


class MissingLine(serializers.Serializer):
    order_line_id = serializers.UUIDField()
    supplier_item_id = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1, max_value=MAX_QUANTITY)


class MissingRequest(serializers.Serializer):
    lines = MissingLine(many=True, min_length=1, max_length=200)


class OrderMissing(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Lo que falta de esta orden (lo que la cotización no ofrece o el acuerdo no '
                   'entregará) con los otros proveedores que tienen cada SKU en existencia, para pedírselo en tu cesta.')
    def get(self, request, account_id, pk):
        account = account_for(request.user, account_id, 'client')
        order = get_object_or_404(SupplierRequest, pk=pk, client=account)
        return Response(missing_data(request.user, account, order))

    @extend_schema(request=MissingRequest, responses=OpenApiTypes.OBJECT, description='Agrega a tu cesta lo que falta de esta orden, cada artículo '
                   'con el proveedor elegido entre los que lo tienen en existencia. Todo o nada; responde tu cesta.')
    def post(self, request, account_id, pk):
        account = account_for(request.user, account_id, 'client')
        order = get_object_or_404(SupplierRequest, pk=pk, client=account)
        data = MissingRequest(data=request.data)
        data.is_valid(raise_exception=True)
        lines = {line.pk: (line, units) for line, units in missing_lines(order)}
        parts = {pk: current_part(line.part) for pk, (line, _) in lines.items()}
        offers = offers_for(order, account, {part.pk for part in parts.values()})
        changes = []
        for chosen in data.validated_data['lines']:
            if chosen['order_line_id'] not in lines:
                raise ValidationError({'lines': ['Ese artículo ya no le falta a esta orden. Vuelve a abrir la lista.']})
            part = parts[chosen['order_line_id']]
            item = next((item for item in offers.get(part.pk, []) if item.pk == chosen['supplier_item_id']), None)
            if item is None:
                line = lines[chosen['order_line_id']][0]
                raise RequestConflict(f'El proveedor que elegiste para {line.codigo} ya no lo tiene en existencia. Vuelve a abrir la lista.')
            changes.append({'supplier_item_id': item.pk, 'quantity': chosen['quantity'], 'mode': 'add', 'expected_part_id': part.pk})
        try:
            apply_changes(request.user, account, changes, source='faltante', reference=order.reference)
        except IntegrityError:  # the same article added at the same moment elsewhere
            raise RequestConflict('Tu cesta cambió en otro lugar. Inténtalo de nuevo.')
        return Response(basket_data(request, request.user, account))
