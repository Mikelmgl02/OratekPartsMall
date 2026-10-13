"""The cesta on the server: what a person gathers to ask quotations for, per client account (BasketItem), the same on every device and
for the WhatsApp assistant. Signed out, the storefront's example cesta stays in the browser.

GET accounts/<account>/basket/ lists it; POST applies changes: add units, set a quantity (0 takes the article out), or keep the larger of
the two (max: how a browser's old local cesta moves in once). Adding an article again from the catalog brings its line up to date (its
SKU, code and brand as they are now), which is how a line that changed since it was added is fixed. Only articles a client may ask a quotation for go in (matched to an active
SKU of an active supplier, never the account's own), up to MAX_LINES. Each line keeps the article as it was added; sending the solicitud
(mall.request_views.submit_request) takes the sent units out in the same transaction.
"""
from django.db import IntegrityError, transaction
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .basket_models import BasketEvent, BasketItem
from .models import Account, SupplierItem
from .request_views import RequestConflict
from .views import account_for

MAX_LINES = 200
MAX_QUANTITY = 9999


class BasketChange(serializers.Serializer):
    supplier_item_id = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=0, max_value=MAX_QUANTITY)
    mode = serializers.ChoiceField(choices=['add', 'set', 'max'], default='add')
    expected_part_id = serializers.UUIDField(required=False)  # the SKU the person chose it under


class BasketChanges(serializers.Serializer):
    changes = BasketChange(many=True, min_length=1, max_length=MAX_LINES)


def requestable(item):
    """Whether a client may ask a quotation for this article now: matched to an active SKU of an active supplier."""
    supplier = item.supplier
    return (item.matching_status == 'matched' and item.part_id is not None and item.part.active and item.part.merged_into_id is None
            and supplier.active and any(role.capability == 'supplier' for role in supplier.roles.all()))


def basket_data(request, user, account):
    """The person's cesta for the account, line by line as the storefront shows it: the SKU (as the catalog lists it), the supplier and the
    article as added, and whether it can still be sent (available) or changed since (changed: its SKU, code or brand)."""
    from .oem_links import equivalents_for
    from .catalog_availability import catalog_availability
    from .serializers import PartSerializer
    rows = list(BasketItem.objects.filter(user=user, account=account)
                .select_related('supplier_item__supplier', 'supplier_item__part', 'part')
                .prefetch_related('supplier_item__supplier__roles', 'part__codes', 'part__images'))
    parts = list({row.part_id: row.part for row in rows}.values())
    context = {'request': request, 'availability_by_part': catalog_availability(parts), 'equivalents_by_part': equivalents_for([part.pk for part in parts])}
    shown = dict(zip((part.pk for part in parts), PartSerializer(parts, many=True, context=context).data))
    lines = []
    for row in rows:
        item = row.supplier_item
        lines.append({
            'key': f'{row.part_id}:{item.supplier_id}:{item.pk}', 'part': shown[row.part_id], 'quantity': row.quantity, 'source': row.source,
            'offer': {'supplier_id': str(item.supplier_id), 'supplier_name': item.supplier.name, 'in_stock': item.available_quantity > 0,
                      'selected_item': {'id': str(item.pk), 'codigo': row.codigo, 'brand': row.brand, 'description': item.description}},
            'available': requestable(item) and item.supplier_id != account.pk,
            'changed': item.part_id != row.part_id or item.codigo != row.codigo or item.brand != row.brand,
            'added_at': row.created_at.isoformat()})
    return {'lines': lines, 'count': len(lines)}


def event(user, account, item, part, codigo, brand, kind, before, after, source, reference=''):
    """One entry of the cesta's ledger, with the article as it is at that moment."""
    return BasketEvent(user=user, account=account, supplier_item=item, part=part, sku=part.sku if part else '', codigo=codigo, brand=brand,
                       supplier_name=item.supplier.name if item else '', kind=kind, quantity_before=before, quantity_after=after, source=source,
                       reference=reference)


def apply_changes(user, account, changes, source='web', reference=''):
    """The changes to the person's cesta, all or none, each in its ledger (BasketEvent; a browser's old cesta moving in, mode max, as
    moved_in; reference: the order whose missing units they are). A new article must be requestable, not the account's own, and still under
    the SKU the person chose it (expected_part_id)."""
    ids = [change['supplier_item_id'] for change in changes]
    events = []
    with transaction.atomic():
        Account.objects.select_for_update().filter(pk=account.pk).first()  # one change (or one solicitud being sent) at a time
        rows = {row.supplier_item_id: row for row in BasketItem.objects.select_for_update(of=('self',)).select_related('part').filter(
            user=user, account=account, supplier_item_id__in=ids)}
        items = {item.pk: item for item in SupplierItem.objects.filter(pk__in=ids).select_related('supplier', 'part').prefetch_related('supplier__roles')}
        count = BasketItem.objects.filter(user=user, account=account).count()
        for change in changes:
            identifier, quantity, mode = change['supplier_item_id'], change['quantity'], change['mode']
            row = rows.get(identifier)
            if row is not None:
                item = items.get(identifier)
                if mode == 'add' and change.get('expected_part_id') and item is not None:  # chosen again in the catalog: as it is now
                    if change['expected_part_id'] != item.part_id:
                        raise RequestConflict(f'El SKU del artículo {item.codigo} cambió. Selecciónalo otra vez en el catálogo.')
                    if requestable(item) and item.supplier_id != account.pk:
                        row.part_id, row.part, row.codigo, row.brand = item.part_id, item.part, item.codigo, item.brand
                total = row.quantity + quantity if mode == 'add' else max(row.quantity, quantity) if mode == 'max' else quantity
                before, after = row.quantity, max(0, min(total, MAX_QUANTITY))
                if after != before:
                    events.append(event(user, account, item, row.part, row.codigo, row.brand, 'removed' if not after else 'changed', before, after, source,
                                        reference))
                if total <= 0:
                    row.delete()
                    rows.pop(identifier)
                    count -= 1
                else:
                    row.quantity = after
                    row.save(update_fields=['quantity', 'part', 'codigo', 'brand', 'updated_at'])
                continue
            if quantity <= 0:
                continue
            item = items.get(identifier)
            if item is None or not requestable(item):
                raise ValidationError({'changes': [f'{item.codigo if item else "Un artículo"} ya no está disponible para solicitar una cotización.']})
            if item.supplier_id == account.pk:
                raise ValidationError({'changes': [f'{item.codigo} es de tu propia cuenta: no puedes pedirte una cotización a ti mismo.']})
            if change.get('expected_part_id') and change['expected_part_id'] != item.part_id:
                raise RequestConflict(f'El SKU del artículo {item.codigo} cambió. Selecciónalo otra vez en el catálogo.')
            if count >= MAX_LINES:
                raise ValidationError({'changes': [f'Tu cesta admite hasta {MAX_LINES} artículos. Envía una solicitud antes de agregar más.']})
            rows[identifier] = BasketItem.objects.create(user=user, account=account, supplier_item=item, part_id=item.part_id, codigo=item.codigo,
                                                         brand=item.brand, quantity=min(quantity, MAX_QUANTITY), source=source)
            events.append(event(user, account, item, item.part, item.codigo, item.brand, 'moved_in' if mode == 'max' else 'added', 0,
                                min(quantity, MAX_QUANTITY), source, reference))
            count += 1
        BasketEvent.objects.bulk_create(events)


def settle(user, account, lines, references=None):
    """A solicitud sent: its units leave the person's cesta (inside its transaction), in the ledger with the order each went in; units
    added since stay."""
    rows = {row.supplier_item_id: row for row in BasketItem.objects.select_for_update(of=('self',)).select_related('part', 'supplier_item__supplier').filter(
        user=user, account=account, supplier_item_id__in=[line['supplier_item_id'] for line in lines])}
    events = []
    for line in lines:
        row = rows.get(line['supplier_item_id'])
        if row is None:
            continue
        after = max(0, row.quantity - line['quantity'])
        events.append(event(user, account, row.supplier_item, row.part, row.codigo, row.brand, 'sent', row.quantity, after, 'web',
                            (references or {}).get(row.supplier_item_id, '')))
        if not after:
            row.delete()
        else:
            row.quantity = after
            row.save(update_fields=['quantity', 'updated_at'])
    BasketEvent.objects.bulk_create(events)


class BasketView(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT, description='Tu cesta para esta cuenta de cliente: cada artículo con su SKU, su proveedor, '
                   'la cantidad y si aún se puede solicitar (available) o cambió desde que lo agregaste (changed).')
    def get(self, request, account_id):
        account = account_for(request.user, account_id, 'client')
        return Response(basket_data(request, request.user, account))

    @extend_schema(request=BasketChanges, responses=OpenApiTypes.OBJECT, description='Cambia tu cesta, todo o nada: mode add suma unidades, '
                   'set fija la cantidad (0 quita el artículo) y max deja la mayor (para traer una cesta guardada en el navegador). Responde la cesta.')
    def post(self, request, account_id):
        account = account_for(request.user, account_id, 'client')
        data = BasketChanges(data=request.data)
        data.is_valid(raise_exception=True)
        try:
            apply_changes(request.user, account, data.validated_data['changes'])
        except IntegrityError:  # the same article added at the same moment elsewhere
            raise RequestConflict('Tu cesta cambió en otro lugar. Inténtalo de nuevo.')
        return Response(basket_data(request, request.user, account))


ACTIVITY_PAGE = 30


def event_data(entry):
    return {'id': entry.pk, 'kind': entry.kind, 'sku': entry.sku, 'codigo': entry.codigo, 'brand': entry.brand, 'supplier_name': entry.supplier_name,
            'quantity_before': entry.quantity_before, 'quantity_after': entry.quantity_after, 'source': entry.source, 'reference': entry.reference,
            'created_at': entry.created_at.isoformat()}


class BasketActivity(APIView):
    @extend_schema(parameters=[OpenApiParameter('before', OpenApiTypes.INT)], responses=OpenApiTypes.OBJECT,
                   description='El historial de tu cesta para esta cuenta, lo más reciente primero, 30 a la vez (before: el id del último que ya '
                   'tienes): qué agregaste, cambiaste, quitaste, trajiste del navegador o enviaste en una solicitud, desde el sitio o WhatsApp.')
    def get(self, request, account_id):
        account = account_for(request.user, account_id, 'client')
        rows = BasketEvent.objects.filter(user=request.user, account=account)
        before = request.query_params.get('before')
        if before:
            if not before.isdigit():
                raise ValidationError({'before': ['Usa el id de un movimiento.']})
            rows = rows.filter(id__lt=int(before))
        page = list(rows.order_by('-id')[:ACTIVITY_PAGE + 1])
        return Response({'results': [event_data(entry) for entry in page[:ACTIVITY_PAGE]], 'has_more': len(page) > ACTIVITY_PAGE})
