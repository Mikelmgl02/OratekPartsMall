"""Supplier-only server draft of the next quotation. Reads write nothing; saves are partial, versioned and retry-safe.

No draft operation changes SupplierRequest.version or updated_at, writes a DealEvent, or appears in request_summary,
deal_data or request_detail. Writes hold the same order row lock as DealActions, so saves and publishing serialize.
"""
import hashlib
import json
from decimal import Decimal

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .availability import check_lines
from .pricing_models import CURRENCY_CHOICES, PERMISSION_CHOICES, choice_values, pricing_settings, record_pricing_event
from .quotation_exceptions import ACKNOWLEDGEABLE, EXCEPTION_CODES, SEVERITIES, compute_exceptions
from .models import User
from .quote_draft_models import (AUDIT_PRICE_SOURCE_CHOICES, DRAFT_STATUS_CHOICES, PRICE_SOURCE_CHOICES, QUANTITY_SOURCE_CHOICES, DealQuotationAudit,
                                 DealQuotationDraft, DealQuotationDraftLine)
from .request_models import DealQuotation, SupplierRequest
from .request_views import RequestConflict, WholeRequestQuantity
from .views import PERMISSION_RANK, membership_for

EDITABLE_STATUSES = ('reviewed', 'adjustment')
CONFLICT_DETAIL = 'Otro miembro de tu equipo modificó este borrador.'
NOT_EDITABLE_DETAIL = 'Esta orden ya no admite cambios en el borrador de cotización. Actualiza el acuerdo.'


class DraftStock(serializers.Serializer):
    reported_quantity = serializers.IntegerField()
    reserved_quantity = serializers.IntegerField()
    available_quantity = serializers.IntegerField()
    shortfall = serializers.IntegerField()
    updated_at = serializers.DateTimeField()


class DraftException(serializers.Serializer):
    code = serializers.ChoiceField(choices=EXCEPTION_CODES)
    severity = serializers.ChoiceField(choices=SEVERITIES)
    message = serializers.CharField()
    context = serializers.CharField(allow_blank=True)
    acknowledged = serializers.BooleanField()


class DraftSuggestion(serializers.Serializer):
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    fingerprint = serializers.CharField()
    explanation = serializers.DictField()


class DraftLineSerializer(serializers.Serializer):
    order_line_id = serializers.UUIDField()
    codigo = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    description = serializers.CharField(allow_blank=True)
    requested = serializers.IntegerField()
    stock = DraftStock(allow_null=True, help_text='Existencias actuales de tu inventario; null si el artículo cambió desde la solicitud.')
    quantity = serializers.IntegerField(allow_null=True)
    quantity_source = serializers.ChoiceField(choices=choice_values(QUANTITY_SOURCE_CHOICES))
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    price_source = serializers.ChoiceField(choices=choice_values(PRICE_SOURCE_CHOICES))
    suggestion = DraftSuggestion(allow_null=True)
    note = serializers.CharField(allow_blank=True, help_text='Nota interna; nunca se muestra al cliente.')
    exceptions = DraftException(many=True)


class DraftSummary(serializers.Serializer):
    blocking = serializers.IntegerField()
    to_confirm = serializers.IntegerField()
    info = serializers.IntegerField()
    total = serializers.DecimalField(max_digits=24, decimal_places=2)
    line_count = serializers.IntegerField()


class DraftPermissions(serializers.Serializer):
    can_publish = serializers.BooleanField()
    publish_requires = serializers.ChoiceField(choices=PERMISSION_CHOICES)


class DraftAuthor(serializers.Serializer):
    name = serializers.CharField()


class QuoteDraftSerializer(serializers.Serializer):
    persisted = serializers.BooleanField(help_text='False mientras el borrador es virtual: se crea con el primer guardado.')
    draft_version = serializers.IntegerField()
    status = serializers.ChoiceField(choices=choice_values(DRAFT_STATUS_CHOICES))
    base_quotation_id = serializers.UUIDField(allow_null=True)
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))
    terms = serializers.CharField(allow_blank=True)
    terms_origin = serializers.ChoiceField(choices=['saved', 'previous', 'none'])
    updated_at = serializers.DateTimeField(allow_null=True)
    updated_by = DraftAuthor(allow_null=True)
    permissions = DraftPermissions()
    lines = DraftLineSerializer(many=True)
    order_exceptions = DraftException(many=True)
    summary = DraftSummary()


class QuoteDraftEnvelope(serializers.Serializer):
    editable = serializers.BooleanField()
    draft = QuoteDraftSerializer(allow_null=True)


class QuoteDraftConflict(serializers.Serializer):
    detail = serializers.CharField()
    draft = QuoteDraftSerializer()


class DraftAcknowledgement(serializers.Serializer):
    code = serializers.ChoiceField(choices=ACKNOWLEDGEABLE)
    context = serializers.CharField(max_length=100, allow_blank=True, help_text='El contexto que viste; la confirmación vale mientras siga igual.')


class DraftLineChange(serializers.Serializer):
    order_line_id = serializers.UUIDField()
    quantity = WholeRequestQuantity(min_value=0, max_value=9999, allow_null=True, required=False)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0'), allow_null=True, required=False)
    note = serializers.CharField(max_length=500, allow_blank=True, required=False)
    acknowledge = DraftAcknowledgement(many=True, required=False, max_length=len(ACKNOWLEDGEABLE))
    revoke = serializers.ListField(child=serializers.ChoiceField(choices=ACKNOWLEDGEABLE), required=False, max_length=len(ACKNOWLEDGEABLE))

    def validate_note(self, value):
        return value.strip().upper()

    def validate(self, data):
        codes = [ack['code'] for ack in data.get('acknowledge', [])] + data.get('revoke', [])
        if len(codes) != len(set(codes)):
            raise serializers.ValidationError('Confirma o retira cada alerta una sola vez.')
        return data


class QuoteDraftSave(serializers.Serializer):
    save_id = serializers.UUIDField()
    expected_draft_version = serializers.IntegerField(min_value=0)
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES), required=False)
    terms = serializers.CharField(max_length=5000, allow_blank=True, required=False)
    lines = DraftLineChange(many=True, required=False)

    def validate_terms(self, value):
        return value.strip().upper()

    def validate_lines(self, lines):
        identifiers = [line['order_line_id'] for line in lines]
        if len(identifiers) != len(set(identifiers)):
            raise serializers.ValidationError('Incluye cada artículo una sola vez en cada guardado.')
        return lines


class DraftDiscard(serializers.Serializer):
    save_id = serializers.UUIDField()
    expected_draft_version = serializers.IntegerField(min_value=0)


def supplier_order(user, account_id, pk, *, lock=False):
    account, membership = membership_for(user, account_id, 'supplier')
    rows = SupplierRequest.objects.select_for_update() if lock else SupplierRequest.objects.all()
    return account, membership, get_object_or_404(rows.filter(supplier=account), pk=pk)


def latest_quotation(row):
    return row.quotations.prefetch_related('lines').order_by('-revision').first()


def current_draft(row, latest):
    """The saved draft for the latest revision; a row prepared on an older base is stale and treated as absent."""
    return DealQuotationDraft.objects.select_related('updated_by').prefetch_related('lines').filter(order=row, base_quotation=latest).first()


def order_lines(row):
    return {line.pk: line for line in row.lines.select_related('supplier_item')}


def stock_by_line(lines):
    return {entry['order_line_id']: entry for entry in check_lines((line, line.quantity) for line in lines)}


def initial_header(latest, settings_row):
    if latest:
        return {'currency': latest.currency, 'terms': latest.terms, 'terms_origin': 'previous'}
    return {'currency': settings_row.default_currency, 'terms': '', 'terms_origin': 'none'}


def initial_values(lines, latest, settings_row, stock):
    """Virtual prefill: previous revision, then the requested quantity (or what is available, per settings), then no price."""
    previous = {line.order_line_id: line for line in latest.lines.all()} if latest else {}
    values = {}
    for line in lines:
        prior, available = previous.get(line.pk), stock[line.pk]['available']
        if prior:
            values[line.pk] = {'quantity': prior.quantity, 'quantity_source': 'previous', 'unit_price': prior.unit_price,
                               'price_source': 'previous', 'note': ''}
            continue
        reduced = settings_row.prefill_quantity == 'available' and available is not None and available < line.quantity
        values[line.pk] = {'quantity': available if reduced else line.quantity, 'quantity_source': 'available' if reduced else 'requested',
                           'unit_price': None, 'price_source': 'none', 'note': ''}
    return values


def draft_payload(row, account, membership):
    settings_row, latest = pricing_settings(account), latest_quotation(row)
    draft, lines = current_draft(row, latest), list(order_lines(row).values())
    stock = stock_by_line(lines)
    stored = {line.order_line_id: line for line in draft.lines.all()} if draft else {}
    saved = {pk: {field: getattr(line, field) for field in ('quantity', 'quantity_source', 'unit_price', 'price_source', 'note')} for pk, line in stored.items()}
    initial = initial_values([line for line in lines if line.pk not in saved], latest, settings_row, stock)
    header = {'currency': draft.currency, 'terms': draft.terms, 'terms_origin': 'saved'} if draft else initial_header(latest, settings_row)
    current = {line.pk: saved.get(line.pk) or initial[line.pk] for line in lines}
    exceptions = compute_exceptions(lines, current, stock, settings_row, acknowledgements={pk: line.acknowledgements for pk, line in stored.items()},
                                    order_acknowledgements=draft.order_acknowledgements if draft else ())
    total, values = Decimal('0'), []
    for line in lines:
        value, entry = current[line.pk], stock[line.pk]
        if value['quantity'] is not None and value['unit_price'] is not None:
            total += value['quantity'] * value['unit_price']
        values.append({'order_line_id': line.pk, 'codigo': line.codigo, 'brand': line.brand, 'description': line.description or line.name,
                       'requested': line.quantity, 'stock': {'reported_quantity': entry['reported'], 'reserved_quantity': entry['reserved'],
                       'available_quantity': entry['available'], 'shortfall': max(0, line.quantity - entry['available']),
                       'updated_at': entry['updated_at']} if entry['identity_ok'] else None,
                       **value, 'suggestion': None, 'exceptions': exceptions['lines'][line.pk]})
    user = draft.updated_by if draft else None
    return QuoteDraftSerializer({
        'persisted': bool(draft), 'draft_version': draft.draft_version if draft else 0, 'status': draft.status if draft else 'editing',
        'base_quotation_id': latest.pk if latest else None, **header, 'updated_at': draft.updated_at if draft else None,
        'updated_by': {'name': user.get_full_name() or user.username} if user else None,
        'permissions': {'can_publish': PERMISSION_RANK.get(membership.permission, -1) >= PERMISSION_RANK[settings_row.publish_min_permission],
                        'publish_requires': settings_row.publish_min_permission},
        'lines': values, 'order_exceptions': exceptions['order'],
        'summary': {**exceptions['summary'], 'total': total.quantize(Decimal('0.01')), 'line_count': len(values)}}).data


def draft_conflict(row, account, membership):
    return Response({'detail': CONFLICT_DETAIL, 'draft': draft_payload(row, account, membership)}, status=409)


class QuoteDraftView(APIView):
    @extend_schema(operation_id='v1_accounts_requests_draft_retrieve', responses=QuoteDraftEnvelope,
                   description='Borrador privado de la próxima cotización. Sin borrador guardado devuelve uno virtual; leerlo no escribe nada.')
    def get(self, request, account_id, pk):
        account, membership, row = supplier_order(request.user, account_id, pk)
        if row.status not in EDITABLE_STATUSES:
            return Response({'editable': False, 'draft': None})
        return Response({'editable': True, 'draft': draft_payload(row, account, membership)})

    @extend_schema(operation_id='v1_accounts_requests_draft_update', request=QuoteDraftSave,
                   responses={200: QuoteDraftSerializer, 409: QuoteDraftConflict},
                   description='Guardado parcial con save_id y expected_draft_version (0 crea el borrador virtual). '
                               'No cambia la versión del acuerdo ni escribe actividad visible para el cliente.')
    @transaction.atomic
    def post(self, request, account_id, pk):
        account, membership, row = supplier_order(request.user, account_id, pk, lock=True)
        serializer = QuoteDraftSave(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if row.status not in EDITABLE_STATUSES:
            raise RequestConflict(NOT_EDITABLE_DETAIL)
        lines = order_lines(row)
        if any(change['order_line_id'] not in lines for change in data.get('lines', [])):
            raise ValidationError({'lines': 'Uno de los artículos no pertenece a esta orden.'})
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
        latest = latest_quotation(row)
        draft = current_draft(row, latest)
        # The order row lock serializes saves, so remembering the last save is enough to make a lost-response retry safe.
        if draft and draft.last_save_id == data['save_id']:
            if draft.last_save_hash != fingerprint:
                raise RequestConflict('Este identificador ya se usó para otro cambio del borrador.')
            return Response(draft_payload(row, account, membership))
        if data['expected_draft_version'] != (draft.draft_version if draft else 0):
            return draft_conflict(row, account, membership)
        now = timezone.now()
        settings_row, saved = None, {}
        if draft is None:
            # Materialize the virtual draft (replacing any row prepared on an older revision), then apply the changes.
            DealQuotationDraft.objects.filter(order=row).delete()
            header = initial_header(latest, settings_row := pricing_settings(account))
            draft = DealQuotationDraft.objects.create(order=row, supplier=account, base_quotation=latest, currency=header['currency'],
                                                      terms=header['terms'], draft_version=0, created_by=request.user, updated_by=request.user)
        else:
            saved = {line.order_line_id: line for line in draft.lines.all()}
        missing = [line for pk, line in lines.items() if pk not in saved]
        if missing:
            initial = initial_values(missing, latest, settings_row or pricing_settings(account), stock_by_line(missing))
            DealQuotationDraftLine.objects.bulk_create([DealQuotationDraftLine(draft=draft, order_line=line, **initial[line.pk]) for line in missing])
            saved = {line.order_line_id: line for line in DealQuotationDraftLine.objects.filter(draft=draft)}
        changed = []
        for change in data.get('lines', []):
            line, dirty = saved[change['order_line_id']], False
            if 'quantity' in change and change['quantity'] != line.quantity:
                line.quantity, line.quantity_source, dirty = change['quantity'], 'manual', True
            if 'unit_price' in change and change['unit_price'] != line.unit_price:
                line.unit_price, line.price_source, dirty = change['unit_price'], 'none' if change['unit_price'] is None else 'manual', True
            if 'note' in change and change['note'] != line.note:
                line.note, dirty = change['note'], True
            if 'acknowledge' in change or 'revoke' in change:
                # One acknowledgement per code, tied to the context the supplier saw; re-confirming the same context keeps the original.
                acks = {ack['code']: ack for ack in line.acknowledgements if ack['code'] not in change.get('revoke', [])}
                for ack in change.get('acknowledge', []):
                    if acks.get(ack['code'], {}).get('context') != ack['context']:
                        acks[ack['code']] = {'code': ack['code'], 'context': ack['context'], 'user_id': request.user.pk, 'at': now.isoformat()}
                if list(acks.values()) != line.acknowledgements:
                    line.acknowledgements, dirty = list(acks.values()), True
            if dirty:
                line.updated_at = now
                changed.append(line)
        DealQuotationDraftLine.objects.bulk_update(changed, ['quantity', 'quantity_source', 'unit_price', 'price_source', 'note', 'acknowledgements', 'updated_at'])
        for field in ('currency', 'terms'):
            if field in data:
                setattr(draft, field, data[field])
        draft.draft_version += 1
        draft.last_save_id, draft.last_save_hash, draft.updated_by = data['save_id'], fingerprint, request.user
        draft.save()
        return Response(draft_payload(row, account, membership))


class QuoteDraftDiscard(APIView):
    @extend_schema(operation_id='v1_accounts_requests_draft_discard', request=DraftDiscard,
                   responses={200: QuoteDraftSerializer, 409: QuoteDraftConflict},
                   description='Elimina el borrador guardado y devuelve el borrador virtual. Sin borrador no hace nada.')
    @transaction.atomic
    def post(self, request, account_id, pk):
        account, membership, row = supplier_order(request.user, account_id, pk, lock=True)
        serializer = DraftDiscard(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if row.status not in EDITABLE_STATUSES:
            raise RequestConflict(NOT_EDITABLE_DETAIL)
        draft = current_draft(row, latest_quotation(row))
        if draft and draft.draft_version != data['expected_draft_version']:
            return draft_conflict(row, account, membership)
        DealQuotationDraft.objects.filter(order=row).delete()
        if draft:
            record_pricing_event(account, request.user, 'draft_discarded', client_id=row.client_id, order=row, object_id=str(row.pk),
                                 payload={'draft_version': draft.draft_version, 'save_id': str(data['save_id'])})
        return Response(draft_payload(row, account, membership))


class TraceException(serializers.Serializer):
    code = serializers.ChoiceField(choices=EXCEPTION_CODES)
    severity = serializers.ChoiceField(choices=SEVERITIES)
    context = serializers.CharField(allow_blank=True)
    acknowledged_by = DraftAuthor(allow_null=True)
    acknowledged_at = serializers.DateTimeField(allow_null=True)


class TraceLine(serializers.Serializer):
    order_line_id = serializers.UUIDField()
    codigo = serializers.CharField()
    brand = serializers.CharField(allow_blank=True)
    description = serializers.CharField(allow_blank=True)
    quantity = serializers.IntegerField()
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    available_at_quote = serializers.IntegerField(allow_null=True)
    identity_ok_at_quote = serializers.BooleanField()
    available_at_accept = serializers.IntegerField(allow_null=True)
    suggested_price = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    price_source = serializers.ChoiceField(choices=choice_values(AUDIT_PRICE_SOURCE_CHOICES))
    quantity_source = serializers.ChoiceField(choices=choice_values(QUANTITY_SOURCE_CHOICES))
    engine_fingerprint = serializers.CharField(allow_blank=True)
    explanation = serializers.DictField()
    exceptions = TraceException(many=True)


class TraceAcceptLine(serializers.Serializer):
    order_line_id = serializers.UUIDField()
    quantity = serializers.IntegerField()
    available_at_quote = serializers.IntegerField(allow_null=True)
    available_at_accept = serializers.IntegerField(allow_null=True)
    identity_ok_at_quote = serializers.BooleanField()
    identity_ok_at_accept = serializers.BooleanField()
    shortfall = serializers.BooleanField()


class TraceAcceptCheck(serializers.Serializer):
    result = serializers.ChoiceField(choices=['ok', 'blocked', 'accepted_with_shortfall'])
    lines = TraceAcceptLine(many=True)
    at = serializers.DateTimeField()


class QuotationTraceSerializer(serializers.Serializer):
    available = serializers.BooleanField(help_text='False para cotizaciones publicadas antes de la trazabilidad; el resto de campos se omite.')
    quotation_id = serializers.UUIDField(required=False)
    revision = serializers.IntegerField(required=False)
    draft_version = serializers.IntegerField(allow_null=True, required=False, help_text='Null si se publicó sin borrador.')
    publisher = DraftAuthor(required=False)
    publisher_permission = serializers.ChoiceField(choices=PERMISSION_CHOICES, required=False)
    published_at = serializers.DateTimeField(required=False)
    settings_snapshot = serializers.DictField(required=False)
    order_exceptions = TraceException(many=True, required=False)
    accept_check = TraceAcceptCheck(allow_null=True, required=False)
    lines = TraceLine(many=True, required=False)


class QuotationTrace(APIView):
    @extend_schema(operation_id='v1_accounts_requests_quotations_trace_retrieve', responses=QuotationTraceSerializer,
                   description='Trazabilidad privada de una cotización publicada: existencias al cotizar y al confirmar, origen de cada precio '
                               'y alertas confirmadas. Solo para el proveedor.')
    def get(self, request, account_id, pk, quotation_id):
        _, _, row = supplier_order(request.user, account_id, pk)
        quotation = get_object_or_404(DealQuotation.objects.filter(order=row), pk=quotation_id)
        audit = DealQuotationAudit.objects.select_related('publisher').filter(quotation=quotation).first()
        if audit is None:
            return Response({'available': False})
        lines = list(quotation.lines.select_related('order_line', 'audit'))
        found = [audit.order_exceptions] + [line.audit.exceptions for line in lines]
        users = {user.pk: user for user in User.objects.filter(pk__in={item['acknowledged_by'] for items in found for item in items if item['acknowledged_by']})}
        def trace(items):
            return [{**item, 'acknowledged_by': {'name': users[item['acknowledged_by']].get_full_name() or users[item['acknowledged_by']].username}
                     if item['acknowledged_by'] in users else None} for item in items]
        return Response(QuotationTraceSerializer({
            'available': True, 'quotation_id': quotation.pk, 'revision': quotation.revision, 'draft_version': audit.draft_version,
            'publisher': {'name': audit.publisher.get_full_name() or audit.publisher.username}, 'publisher_permission': audit.publisher_permission,
            'published_at': quotation.created_at, 'settings_snapshot': audit.settings_snapshot, 'order_exceptions': trace(audit.order_exceptions),
            'accept_check': audit.accept_check,
            'lines': [{'order_line_id': line.order_line_id, 'codigo': line.order_line.codigo, 'brand': line.order_line.brand,
                       'description': line.order_line.description or line.order_line.name, 'quantity': line.quantity, 'unit_price': line.unit_price,
                       **{field: getattr(line.audit, field) for field in ('available_at_quote', 'identity_ok_at_quote', 'available_at_accept', 'suggested_price',
                                                                         'price_source', 'quantity_source', 'engine_fingerprint', 'explanation')},
                       'exceptions': trace(line.audit.exceptions)} for line in lines]}).data)
