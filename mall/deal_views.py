"""Account-scoped deal transitions. A row lock and version protect every decision."""
import hashlib
import json
from decimal import Decimal

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .request_models import (SupplierRequest, DealQuotation, DealQuotationLine, DealEvent,
                             DealMessage, DealCommand)
from .request_views import (RequestConflict, WholeRequestQuantity, request_detail_queryset,
                            request_detail, sent_request_queryset, sent_request_summary)
from .views import account_for


def iso(value):
    return value.isoformat() if value else None


def deal_data(row):
    quotes = row.quotations.prefetch_related('lines__order_line').order_by('revision')
    quotations = [{'id': str(quote.pk), 'revision': quote.revision, 'currency': quote.currency,
                   'terms': quote.terms, 'total': str(quote.total), 'created_at': iso(quote.created_at),
                   'supplier_confirmed_at': iso(quote.supplier_confirmed_at),
                   'client_confirmed_at': iso(quote.client_confirmed_at),
                   'lines': [{'order_line_id': str(line.order_line_id), 'codigo': line.order_line.codigo,
                              'sku': line.order_line.sku, 'description': line.order_line.description,
                              'quantity': line.quantity, 'unit_price': str(line.unit_price),
                              'total': str((line.unit_price * line.quantity).quantize(Decimal('0.01')))}
                             for line in quote.lines.all()]} for quote in quotes]
    events = list(row.events.select_related('account', 'actor').order_by('-id')[:100])
    return {'client': {'id': str(row.client_id), 'name': row.client_name},
            'supplier': {'id': str(row.supplier_id), 'name': row.supplier_name},
            'quotations': quotations, 'quotation': quotations[-1] if quotations else None,
            'events': [{'id': event.pk, 'kind': event.kind, 'description': event.description,
                        'account_name': event.account.name, 'actor_name': event.actor.get_full_name() or event.actor.username,
                        'created_at': iso(event.created_at), 'quotation_id': str(event.quotation_id) if event.quotation_id else None}
                       for event in reversed(events)]}


def client_detail(row):
    return {**sent_request_summary(row), **deal_data(row), 'notes': row.notes,
            'lines': [{'id': str(line.pk), 'part_id': str(line.part_id), 'sku': line.sku,
                       'name': line.name, 'codigo': line.codigo, 'brand': line.brand,
                       'description': line.description, 'quantity': line.quantity} for line in row.lines.all()]}


def participant(user, account_id, pk, *, lock=False):
    account = account_for(user, account_id)
    rows = SupplierRequest.objects.all()
    if lock:
        rows = rows.select_for_update()
    from django.db.models import Q
    row = get_object_or_404(rows.filter(Q(client=account) | Q(supplier=account)), pk=pk)
    capabilities = set(account.roles.values_list('capability', flat=True))
    if not ((row.client_id == account.pk and 'client' in capabilities)
            or (row.supplier_id == account.pk and 'supplier' in capabilities)):
        raise PermissionDenied('La cuenta no tiene acceso a este acuerdo.')
    return account, row


class QuoteLine(serializers.Serializer):
    order_line_id = serializers.UUIDField()
    quantity = WholeRequestQuantity(min_value=0, max_value=9999)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0'))


class DealActionSerializer(serializers.Serializer):
    operation_id = serializers.UUIDField()
    expected_version = serializers.IntegerField(min_value=1)
    action = serializers.ChoiceField(choices=['quote', 'accept', 'request_adjustment', 'return_quote'])
    quotation_id = serializers.UUIDField(required=False)
    currency = serializers.ChoiceField(choices=['USD', 'PAB'], default='USD')
    terms = serializers.CharField(required=False, default='', allow_blank=True, max_length=5000)
    reason = serializers.CharField(required=False, default='', allow_blank=True, max_length=4000)
    lines = QuoteLine(many=True, required=False, min_length=1)

    def validate(self, data):
        data['terms'] = data['terms'].strip().upper()
        data['reason'] = data['reason'].strip().upper()
        if data['action'] == 'quote' and not data.get('lines'):
            raise serializers.ValidationError('Agrega las cantidades y precios de la cotización.')
        if data['action'] in ['accept', 'request_adjustment', 'return_quote'] and not data.get('quotation_id'):
            raise serializers.ValidationError('Indica la cotización que estás revisando.')
        if data['action'] == 'request_adjustment' and not data['reason']:
            raise serializers.ValidationError('Describe el ajuste que necesitas.')
        return data


class DealActions(APIView):
    @extend_schema(request=DealActionSerializer, responses=OpenApiTypes.OBJECT)
    @transaction.atomic
    def post(self, request, account_id, pk):
        account, row = participant(request.user, account_id, pk, lock=True)
        serializer = DealActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
        previous = DealCommand.objects.filter(order=row, account=account, operation_id=data['operation_id']).first()
        if previous:
            if previous.payload_hash != fingerprint:
                raise RequestConflict('Este identificador ya se usó para otra decisión.')
            return Response(previous.result)
        action = data['action']
        supplier_action = action in ['quote', 'return_quote']
        expected_account_id = row.supplier_id if supplier_action else row.client_id
        capability = 'supplier' if supplier_action else 'client'
        if account.pk != expected_account_id or not account.roles.filter(capability=capability).exists():
            raise PermissionDenied('Esta decisión corresponde a la otra parte del acuerdo.')
        if row.version != data['expected_version']:
            raise RequestConflict('El acuerdo cambió. Actualízalo para revisar su estado y cotización actuales.')
        latest = row.quotations.order_by('-revision').first()
        if action != 'quote' and (not latest or latest.pk != data['quotation_id']):
            raise RequestConflict('Esta cotización ya no es la vigente. Actualiza el acuerdo.')
        now = timezone.now()
        if action == 'quote':
            if row.status not in ['reviewed', 'adjustment']:
                raise RequestConflict('Solo puedes cotizar una orden en revisión o con un ajuste solicitado.')
            identifiers = [line['order_line_id'] for line in data['lines']]
            order_lines = set(row.lines.values_list('pk', flat=True))
            if len(identifiers) != len(set(identifiers)) or set(identifiers) != order_lines:
                raise ValidationError('Cotiza cada artículo de esta orden una sola vez. Usa cantidad 0 para los no disponibles.')
            if not any(line['quantity'] > 0 for line in data['lines']):
                raise ValidationError('La cotización necesita al menos una unidad disponible.')
            total = sum((line['unit_price'] * line['quantity'] for line in data['lines']), Decimal('0')).quantize(Decimal('0.01'))
            if total >= Decimal('100000000000000'):
                raise ValidationError('El total de la cotización supera el importe permitido.')
            latest = DealQuotation.objects.create(order=row, revision=(latest.revision + 1) if latest else 1,
                currency=data['currency'], terms=data['terms'], total=total,
                published_by=request.user, supplier_confirmed_at=now)
            DealQuotationLine.objects.bulk_create([DealQuotationLine(quotation=latest,
                order_line_id=line['order_line_id'], quantity=line['quantity'], unit_price=line['unit_price']) for line in data['lines']])
            row.status = 'quoted'
            description = f'COTIZACIÓN V{latest.revision} CONFIRMADA POR EL PROVEEDOR Y ENVIADA AL CLIENTE.'
        elif action == 'accept':
            if row.status != 'quoted':
                raise RequestConflict('La cotización no está disponible para confirmar.')
            latest.client_confirmed_at = now
            latest.client_confirmed_by = request.user
            latest.save(update_fields=['client_confirmed_at', 'client_confirmed_by'])
            row.status = 'handshaked'
            row.handshaked_at = now
            description = 'AMBAS PARTES CONFIRMARON LA COTIZACIÓN. ACUERDO CERRADO · HANDSHAKED.'
        elif action == 'request_adjustment':
            if row.status != 'quoted':
                raise RequestConflict('Solo puedes pedir un ajuste de una cotización pendiente de confirmación.')
            row.status = 'adjustment'
            description = data['reason']
            DealMessage.objects.create(order=row, account=account, actor=request.user,
                                       message_id=data['operation_id'], body=data['reason'])
        else:
            if row.status != 'adjustment':
                raise RequestConflict('Solo puedes devolver una cotización que tenga un ajuste solicitado.')
            row.status = 'quoted'
            description = data['reason'] or 'EL PROVEEDOR DEVOLVIÓ LA MISMA COTIZACIÓN PARA CONFIRMACIÓN.'
        row.version += 1
        row.save(update_fields=['status', 'version', 'handshaked_at', 'updated_at'])
        DealEvent.objects.create(order=row, account=account, actor=request.user, kind=action,
                                 description=description, quotation=latest)
        fresh = request_detail_queryset(row.supplier).get(pk=row.pk)
        result = request_detail(fresh) if supplier_action else client_detail(fresh)
        DealCommand.objects.create(order=row, account=account, operation_id=data['operation_id'],
                                   payload_hash=fingerprint, result=result)
        return Response(result)


class MessageSerializer(serializers.Serializer):
    message_id = serializers.UUIDField()
    body = serializers.CharField(max_length=4000, trim_whitespace=True)

    def validate_body(self, value):
        return value.upper()


class MessageCursor(serializers.Serializer):
    after = serializers.IntegerField(min_value=0, required=False)
    before = serializers.IntegerField(min_value=1, required=False)

    def validate(self, data):
        if 'after' in data and 'before' in data:
            raise serializers.ValidationError('Usa un solo cursor de conversación.')
        return data


def message_data(message):
    return {'id': message.pk, 'message_id': str(message.message_id), 'body': message.body,
            'account_id': str(message.account_id), 'account_name': message.account.name,
            'actor_name': message.actor.get_full_name() or message.actor.username, 'created_at': iso(message.created_at)}


class DealMessages(APIView):
    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, account_id, pk):
        _, row = participant(request.user, account_id, pk)
        filters = MessageCursor(data=request.query_params)
        filters.is_valid(raise_exception=True)
        after = filters.validated_data.get('after')
        messages = row.messages.select_related('account', 'actor')
        before = filters.validated_data.get('before')
        if before is not None:
            page = list(reversed(list(messages.filter(id__lt=before).order_by('-id')[:100])))
        elif after is None:
            page = list(reversed(list(messages.order_by('-id')[:100])))
        else:
            page = list(messages.filter(id__gt=after).order_by('id')[:100])
        cursor = page[-1].pk if page else (after or 0)
        return Response({'results': [message_data(message) for message in page], 'cursor': cursor,
                         'has_more': messages.filter(id__gt=cursor).exists(),
                         'has_earlier': bool(page) and messages.filter(id__lt=page[0].pk).exists()})

    @extend_schema(request=MessageSerializer, responses=OpenApiTypes.OBJECT)
    @transaction.atomic
    def post(self, request, account_id, pk):
        account, row = participant(request.user, account_id, pk, lock=True)
        serializer = MessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        message = row.messages.filter(account=account, message_id=data['message_id']).first()
        if message:
            if message.body != data['body']:
                raise RequestConflict('Este identificador ya se utilizó para otro mensaje.')
        else:
            message = DealMessage.objects.create(order=row, account=account, actor=request.user, **data)
        return Response(message_data(message), status=200)
