"""Supplier-only server draft of the next quotation. Reads write nothing; saves are partial, versioned and retry-safe.

No draft operation changes SupplierRequest.version or updated_at, writes a DealEvent, or appears in request_summary,
deal_data or request_detail. Writes hold the same order row lock as DealActions, so saves and publishing serialize.
The AI assistant's runs and decisions live here too: running never changes the draft, and applying a proposal is a draft save.
"""
import hashlib
import json
import uuid
from collections import Counter
from decimal import Decimal

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from .availability import check_lines
from .pricing_engine import ENGINE_VERSION, client_profile, price_lines
from .pricing_models import CURRENCY_CHOICES, PERMISSION_CHOICES, choice_values, pricing_settings, record_pricing_event
from .quotation_exceptions import ACKNOWLEDGEABLE, EXCEPTION_CODES, SEVERITIES, compute_exceptions
from .models import User
from .quote_assistant import (APPLICABLE, DECISION_STATES, IN_PROGRESS, MAX_LINES, NO_CLIENT_TEXT, NOT_APPLICABLE, PROPOSAL_KINDS, TOO_MANY_LINES,
                              QuoteAssistantProviderError, assistant_block, assistant_findings, build_input, ensure_enabled, execute_run,
                              has_client_text, start_run)
from .quote_assistant_models import RUN_STATUS_CHOICES, QuoteAssistantRun
from .quote_draft_models import (AUDIT_PRICE_SOURCE_CHOICES, DRAFT_STATUS_CHOICES, PRICE_SOURCE_CHOICES, QUANTITY_SOURCE_CHOICES, DealQuotationAudit,
                                 DealQuotationDraft, DealQuotationDraftLine)
from .request_models import DealQuotation, SupplierRequest
from .request_views import RequestConflict, WholeRequestQuantity
from .views import PERMISSION_RANK, membership_for

EDITABLE_STATUSES = ('reviewed', 'adjustment')
REPRICE_SCOPES = ('blank', 'engine', 'lines')
VALUE_FIELDS = ('quantity', 'quantity_source', 'unit_price', 'price_source', 'note', 'engine_unit_price', 'engine_fingerprint', 'explanation')
ENGINE_FIELDS = ('engine_unit_price', 'engine_fingerprint', 'explanation')
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
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True, help_text='Null si tu lista no tiene precio para este artículo.')
    fingerprint = serializers.CharField()
    explanation = serializers.DictField(help_text='Cómo se calculó: lista, revisión, paridad, redondeo y precio mínimo. Solo para el proveedor.')


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


class DraftPriceList(serializers.Serializer):
    id = serializers.UUIDField()
    code = serializers.CharField()
    name = serializers.CharField()
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))


class DraftProfile(serializers.Serializer):
    exists = serializers.BooleanField(help_text='False si el cliente aún no tiene perfil comercial: se usa tu lista predeterminada.')
    version = serializers.IntegerField()
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    customer_code = serializers.CharField(allow_blank=True)
    internal_notes = serializers.CharField(allow_blank=True, help_text='Notas internas de tu equipo; nunca se muestran al cliente.')


class DraftPricing(serializers.Serializer):
    engine = serializers.CharField()
    configured = serializers.BooleanField(help_text='False si no tienes listas de precios ni reglas que den precio: no hay precios sugeridos.')
    price_list = DraftPriceList(allow_null=True, help_text='La lista que usa este cliente: la de su perfil o tu lista predeterminada.')
    profile = DraftProfile(help_text='Perfil comercial privado del cliente.')
    stale = serializers.IntegerField(help_text='Artículos cuyo precio sugerido cambió desde que se calculó.')
    fillable = serializers.IntegerField(help_text='Artículos sin precio que tienen un precio sugerido.')


class RepricedLine(serializers.Serializer):
    order_line_id = serializers.UUIDField()
    previous_unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    reason = serializers.ChoiceField(choices=['quantity', *REPRICE_SCOPES])


class QuoteAssistantProposalSerializer(serializers.Serializer):
    id = serializers.CharField()
    kind = serializers.ChoiceField(choices=PROPOSAL_KINDS, help_text='quantity_change, remove_line, line_note y terms se aplican al borrador; price_request, '
                                                                     'unmatched y question solo se descartan (las preguntas se copian a la conversación).')
    detail = serializers.CharField(allow_blank=True, help_text='terms: entrega, retiro, factura u otro. price_request: descuento, igualar_precio o pregunta_precio.')
    order_line_id = serializers.UUIDField(allow_null=True)
    codigo = serializers.CharField(allow_blank=True)
    quantity = serializers.IntegerField(allow_null=True, help_text='Unidades propuestas (quantity_change, remove_line). El asistente nunca propone precios.')
    text = serializers.CharField(allow_blank=True)
    evidence = serializers.CharField(allow_blank=True, help_text='Cita literal de lo que escribió el cliente.')
    decision = serializers.ChoiceField(choices=list(DECISION_STATES.values()), allow_null=True)
    can_apply = serializers.BooleanField()


class QuoteAssistantRunSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=choice_values(RUN_STATUS_CHOICES))
    cached = serializers.BooleanField(help_text='Servida desde una interpretación igual de los últimos 30 días, sin costo.')
    stale = serializers.BooleanField(help_text='Hay mensajes nuevos desde esta interpretación.')
    draft_version = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    finished_at = serializers.DateTimeField(allow_null=True)
    created_by = DraftAuthor()
    summary = serializers.CharField(allow_blank=True)
    rejected_items = serializers.IntegerField(help_text='Propuestas descartadas por mencionar importes.')
    proposals = QuoteAssistantProposalSerializer(many=True)


class QuoteAssistantStateSerializer(serializers.Serializer):
    configured = serializers.BooleanField(help_text='La plataforma tiene el asistente disponible.')
    enabled = serializers.BooleanField(help_text='El propietario de la cuenta activó el asistente en Precios › Configuración.')
    runs_left = serializers.IntegerField(help_text='Interpretaciones que quedan para esta versión.')
    unavailable = serializers.CharField(allow_blank=True, help_text='Por qué no se puede interpretar ahora; vacío si se puede.')
    latest_run = QuoteAssistantRunSerializer(allow_null=True)


class QuoteDraftSerializer(serializers.Serializer):
    persisted = serializers.BooleanField(help_text='False mientras el borrador es virtual: se crea con el primer guardado.')
    draft_version = serializers.IntegerField()
    status = serializers.ChoiceField(choices=choice_values(DRAFT_STATUS_CHOICES))
    base_quotation_id = serializers.UUIDField(allow_null=True)
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))
    terms = serializers.CharField(allow_blank=True)
    terms_origin = serializers.ChoiceField(choices=['saved', 'previous', 'profile', 'none'])
    updated_at = serializers.DateTimeField(allow_null=True)
    updated_by = DraftAuthor(allow_null=True)
    review_requested_by = DraftAuthor(allow_null=True, help_text='Quién solicitó la aprobación; null mientras el borrador está en edición.')
    review_requested_at = serializers.DateTimeField(allow_null=True)
    permissions = DraftPermissions()
    pricing = DraftPricing()
    lines = DraftLineSerializer(many=True)
    order_exceptions = DraftException(many=True)
    summary = DraftSummary()
    repriced = RepricedLine(many=True, help_text='Precios que este guardado recalculó con tu lista; vacío en lecturas.')
    assistant = QuoteAssistantStateSerializer(help_text='Asistente de cotización con IA; oculto mientras configured o enabled sea false.')


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
    request_review = serializers.BooleanField(required=False, help_text='true: solicita la aprobación (status review_requested); false: la retira. '
                                                                         'Cambiar cantidades, precios, moneda o condiciones sin enviarlo la retira.')

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


class QuoteAssistantRunRequest(serializers.Serializer):
    run_id = serializers.UUIDField(help_text='Generado por el cliente: repetirlo devuelve la misma interpretación sin volver a llamar a la IA.')
    expected_draft_version = serializers.IntegerField(min_value=0)


class QuoteAssistantDecision(serializers.Serializer):
    proposal_id = serializers.RegexField(r'^p[0-9]{1,3}$', max_length=4)
    action = serializers.ChoiceField(choices=list(DECISION_STATES))


class QuoteAssistantDecisionsRequest(DraftDiscard):
    decisions = QuoteAssistantDecision(many=True, min_length=1, max_length=100)

    def validate_decisions(self, decisions):
        identifiers = [decision['proposal_id'] for decision in decisions]
        if len(identifiers) != len(set(identifiers)):
            raise serializers.ValidationError('Decide cada propuesta una sola vez.')
        return decisions


class QuoteDraftRepriceRequest(DraftDiscard):
    scope = serializers.ChoiceField(choices=REPRICE_SCOPES, help_text='blank: completa los precios vacíos; engine: recalcula los precios de tu lista; '
                                                                       'lines: reemplaza los precios de los artículos indicados.')
    order_line_ids = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=2000)

    def validate(self, data):
        if data['scope'] == 'lines' and not data.get('order_line_ids'):
            raise serializers.ValidationError('Indica los artículos cuyo precio quieres reemplazar.')
        return data


def supplier_order(user, account_id, pk, *, lock=False):
    account, membership = membership_for(user, account_id, 'supplier')
    rows = SupplierRequest.objects.select_for_update() if lock else SupplierRequest.objects.all()
    return account, membership, get_object_or_404(rows.filter(supplier=account), pk=pk)


def latest_quotation(row):
    return row.quotations.prefetch_related('lines').order_by('-revision').first()


def current_draft(row, latest):
    """The saved draft for the latest revision; a row prepared on an older base is stale and treated as absent."""
    return DealQuotationDraft.objects.select_related('updated_by', 'review_requested_by').prefetch_related('lines').filter(order=row, base_quotation=latest).first()


def order_lines(row):
    return {line.pk: line for line in row.lines.select_related('supplier_item')}


def stock_by_line(lines):
    return {entry['order_line_id']: entry for entry in check_lines((line, line.quantity) for line in lines)}


def initial_header(latest, settings_row, profile):
    """Currency: previous revision, then the client's preferred currency, then the supplier default. Terms: previous revision, then the
    profile's default terms (first quotation only)."""
    if latest:
        return {'currency': latest.currency, 'terms': latest.terms, 'terms_origin': 'previous'}
    currency = profile.preferred_currency if profile and profile.preferred_currency else settings_row.default_currency
    if profile and profile.default_terms:
        return {'currency': currency, 'terms': profile.default_terms, 'terms_origin': 'profile'}
    return {'currency': currency, 'terms': '', 'terms_origin': 'none'}


def initial_quantities(lines, latest, settings_row, stock):
    """Virtual prefill of quantities: previous revision, then the requested quantity (or what is available, per settings)."""
    previous = {line.order_line_id: line for line in latest.lines.all()} if latest else {}
    values = {}
    for line in lines:
        prior, available = previous.get(line.pk), stock[line.pk]['available']
        reduced = not prior and settings_row.prefill_quantity == 'available' and available is not None and available < line.quantity
        values[line.pk] = {'quantity': prior.quantity if prior else available if reduced else line.quantity,
                           'quantity_source': 'previous' if prior else 'available' if reduced else 'requested'}
    return values


def engine_values(result):
    return {'unit_price': result.unit_price, 'price_source': 'engine', 'engine_unit_price': result.unit_price,
            'engine_fingerprint': result.fingerprint, 'explanation': result.explanation}


NO_ENGINE = {'engine_unit_price': None, 'engine_fingerprint': '', 'explanation': {}}


def initial_values(lines, latest, quantities, pricing):
    """Virtual prefill of prices: previous revision, then the engine suggestion, then blank."""
    previous = {line.order_line_id: line for line in latest.lines.all()} if latest else {}
    values = {}
    for line in lines:
        prior, result = previous.get(line.pk), pricing[line.pk]
        price = {'unit_price': prior.unit_price, 'price_source': 'previous', **NO_ENGINE} if prior else engine_values(result) \
            if result.unit_price is not None else {'unit_price': None, 'price_source': 'none', **NO_ENGINE}
        values[line.pk] = {**quantities[line.pk], **price, 'note': ''}
    return values


def priced(row, account, currency, values, lines, settings_row, profile):
    """Live suggestions for the draft's currency and offered quantities (the requested quantity while none is set), with the pair's profile."""
    return price_lines(account, row.client_id, currency, [(line, values[line.pk]['quantity']) for line in lines], settings_row=settings_row, profile=profile)


def profile_block(profile):
    return {'exists': profile is not None, 'version': profile.version if profile else 0, 'discount_percent': profile.discount_percent if profile else Decimal('0'),
            'customer_code': profile.customer_code if profile else '', 'internal_notes': profile.internal_notes if profile else ''}


def draft_payload(row, account, membership, repriced=()):
    settings_row, latest, profile = pricing_settings(account), latest_quotation(row), client_profile(account, row.client_id)
    draft, by_pk = current_draft(row, latest), order_lines(row)
    lines = list(by_pk.values())
    stock = stock_by_line(lines)
    stored = {line.order_line_id: line for line in draft.lines.all()} if draft else {}
    saved = {pk: {field: getattr(line, field) for field in VALUE_FIELDS} for pk, line in stored.items()}
    header = {'currency': draft.currency, 'terms': draft.terms, 'terms_origin': 'saved'} if draft else initial_header(latest, settings_row, profile)
    unsaved = [line for line in lines if line.pk not in saved]
    quantities = {**saved, **initial_quantities(unsaved, latest, settings_row, stock)}
    pricing = priced(row, account, header['currency'], quantities, lines, settings_row, profile)
    initial = initial_values(unsaved, latest, quantities, pricing)
    current = {line.pk: saved.get(line.pk) or initial[line.pk] for line in lines}
    assistant = assistant_block(row, settings_row, latest, by_pk)
    exceptions = compute_exceptions(lines, current, stock, settings_row, pricing=pricing, acknowledgements={pk: line.acknowledgements for pk, line in stored.items()},
                                    order_acknowledgements=draft.order_acknowledgements if draft else (), no_profile=profile is None and row.client_id != account.pk,
                                    extra=assistant_findings(assistant))
    total, values = Decimal('0'), []
    for line in lines:
        value, entry, result = current[line.pk], stock[line.pk], pricing[line.pk]
        if value['quantity'] is not None and value['unit_price'] is not None:
            total += value['quantity'] * value['unit_price']
        values.append({'order_line_id': line.pk, 'codigo': line.codigo, 'brand': line.brand, 'description': line.description or line.name,
                       'requested': line.quantity, 'stock': {'reported_quantity': entry['reported'], 'reserved_quantity': entry['reserved'],
                       'available_quantity': entry['available'], 'shortfall': max(0, line.quantity - entry['available']),
                       'updated_at': entry['updated_at']} if entry['identity_ok'] else None,
                       **value, 'suggestion': {'unit_price': result.unit_price, 'fingerprint': result.fingerprint, 'explanation': result.explanation}
                       if result.configured else None, 'exceptions': exceptions['lines'][line.pk]})
    configured = next((result for result in pricing.values() if result.configured), None)
    user, reviewer = (draft.updated_by, draft.review_requested_by) if draft else (None, None)
    return QuoteDraftSerializer({
        'persisted': bool(draft), 'draft_version': draft.draft_version if draft else 0, 'status': draft.status if draft else 'editing',
        'base_quotation_id': latest.pk if latest else None, **header, 'updated_at': draft.updated_at if draft else None,
        'updated_by': {'name': user.get_full_name() or user.username} if user else None,
        'review_requested_by': {'name': reviewer.get_full_name() or reviewer.username} if reviewer else None,
        'review_requested_at': draft.review_requested_at if draft else None,
        'permissions': {'can_publish': PERMISSION_RANK.get(membership.permission, -1) >= PERMISSION_RANK[settings_row.publish_min_permission],
                        'publish_requires': settings_row.publish_min_permission},
        'pricing': {'engine': ENGINE_VERSION, 'configured': configured is not None, 'profile': profile_block(profile),
                    'price_list': next((result.explanation['price_list'] for result in pricing.values() if result.explanation['price_list']), None),
                    'stale': sum(any(item['code'] == 'stale_price' for item in found) for found in exceptions['lines'].values()),
                    'fillable': sum(current[line.pk]['unit_price'] is None and pricing[line.pk].unit_price is not None for line in lines)},
        'lines': values, 'order_exceptions': exceptions['order'], 'repriced': list(repriced), 'assistant': assistant,
        'summary': {**exceptions['summary'], 'total': total.quantize(Decimal('0.01')), 'line_count': len(values)}}).data


def ensure_draft(row, account, user, latest, draft, lines, settings_row, profile, now):
    """Materializes the virtual draft (replacing any row prepared on an older revision) and lines missing from it -> (draft, {pk: line})."""
    saved = {}
    if draft is None:
        DealQuotationDraft.objects.filter(order=row).delete()
        header = initial_header(latest, settings_row, profile)
        draft = DealQuotationDraft.objects.create(order=row, supplier=account, base_quotation=latest, currency=header['currency'], terms=header['terms'],
                                                  draft_version=0, created_by=user, updated_by=user, pricing_context=pricing_context(settings_row, profile, now))
    else:
        saved = {line.order_line_id: line for line in draft.lines.all()}
    missing = [line for pk, line in lines.items() if pk not in saved]
    if missing:
        quantities = initial_quantities(missing, latest, settings_row, stock_by_line(missing))
        initial = initial_values(missing, latest, quantities, priced(row, account, draft.currency, quantities, missing, settings_row, profile))
        DealQuotationDraftLine.objects.bulk_create([DealQuotationDraftLine(draft=draft, order_line=line, **initial[line.pk]) for line in missing])
        saved = {line.order_line_id: line for line in DealQuotationDraftLine.objects.filter(draft=draft)}
    return draft, saved


def pricing_context(settings_row, profile, now):
    return {'engine': ENGINE_VERSION, 'settings_version': settings_row.version, 'profile_id': str(profile.pk) if profile else None,
            'profile_version': profile.version if profile else None, 'priced_at': now.isoformat()}


def apply_engine(line, result):
    for field, value in engine_values(result).items():
        setattr(line, field, value)


def apply_price(line, price, result):
    """A typed price equal to the live suggestion stays an engine price; any other value is manual (blank is none)."""
    if price is not None and result and result.unit_price == price:
        return apply_engine(line, result)
    line.unit_price, line.price_source = price, 'none' if price is None else 'manual'
    for field, value in NO_ENGINE.items():
        setattr(line, field, value)


def engine_followers(row, account, currency, saved, lines, requantified, settings_row, profile):
    """Lines whose engine price follows their quantity change: only one still current before the change. A stale engine price (the list
    moved since it was calculated) keeps its value and raises stale_price, so a quantity edit never adopts a list change unnoticed."""
    followers = [pk for pk in requantified if saved[pk].price_source == 'engine']
    return {pk for pk, result in priced(row, account, currency, {pk: {'quantity': requantified[pk]} for pk in followers}, [lines[pk] for pk in followers],
                                        settings_row, profile).items() if result.fingerprint == saved[pk].engine_fingerprint} if followers else set()


def repriced_line(line, before, reason):
    return {'order_line_id': line.order_line_id, 'previous_unit_price': before, 'unit_price': line.unit_price, 'reason': reason}


LINE_UPDATE_FIELDS = ['quantity', 'quantity_source', 'unit_price', 'price_source', 'note', 'acknowledgements', *ENGINE_FIELDS, 'updated_at']


def review_state(draft, row, latest, account, user, requested, edited, now):
    """Approval request: request_review true asks for it (again after a content edit), false withdraws it, and editing what the client would
    receive (quantities, prices, currency, terms) without the flag withdraws it too. Notes and alert confirmations keep it."""
    if requested and (draft.status != 'review_requested' or edited):
        draft.status, draft.review_requested_by, draft.review_requested_at = 'review_requested', user, now
        record_pricing_event(account, user, 'draft_review_requested', client_id=row.client_id, order=row, object_id=str(row.pk),
                             payload={'draft_version': draft.draft_version, 'revision': latest.revision + 1 if latest else 1})
    elif requested is False or (requested is None and edited):
        draft.status, draft.review_requested_by, draft.review_requested_at = 'editing', None, None


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
        now, settings_row, profile = timezone.now(), pricing_settings(account), client_profile(account, row.client_id)
        draft, saved = ensure_draft(row, account, request.user, latest, draft, lines, settings_row, profile, now)
        changes, changed, repriced, requantified, edited = data.get('lines', []), {}, [], {}, False
        for change in changes:
            line = saved[change['order_line_id']]
            if 'quantity' in change and change['quantity'] != line.quantity:
                requantified[line.order_line_id] = line.quantity
                line.quantity, line.quantity_source, changed[line.order_line_id], edited = change['quantity'], 'manual', line, True
        currency = data.get('currency', draft.currency)
        # Typed prices are compared with the live suggestion; engine prices follow a quantity change (a volume rule may apply).
        pricing = priced(row, account, currency, {pk: {'quantity': line.quantity} for pk, line in saved.items()},
                         [lines[pk] for pk in saved], settings_row, profile) if requantified or any('unit_price' in change for change in changes) else {}
        current = engine_followers(row, account, draft.currency, saved, lines, requantified, settings_row, profile)
        for change in changes:
            line, result = saved[change['order_line_id']], pricing.get(change['order_line_id'])
            if 'unit_price' in change and change['unit_price'] != line.unit_price:
                apply_price(line, change['unit_price'], result)
                changed[line.order_line_id], edited = line, True
            elif line.order_line_id in current and result and result.unit_price is not None:
                before = line.unit_price
                apply_engine(line, result)
                if before != line.unit_price:
                    repriced.append(repriced_line(line, before, 'quantity'))
            if 'note' in change and change['note'] != line.note:
                line.note, changed[line.order_line_id] = change['note'], line
            if 'acknowledge' in change or 'revoke' in change:
                # One acknowledgement per code, tied to the context the supplier saw; re-confirming the same context keeps the original.
                acks = {ack['code']: ack for ack in line.acknowledgements if ack['code'] not in change.get('revoke', [])}
                for ack in change.get('acknowledge', []):
                    if acks.get(ack['code'], {}).get('context') != ack['context']:
                        acks[ack['code']] = {'code': ack['code'], 'context': ack['context'], 'user_id': request.user.pk, 'at': now.isoformat()}
                if list(acks.values()) != line.acknowledgements:
                    line.acknowledgements, changed[line.order_line_id] = list(acks.values()), line
        for line in changed.values():
            line.updated_at = now
        DealQuotationDraftLine.objects.bulk_update(list(changed.values()), LINE_UPDATE_FIELDS)
        for field in ('currency', 'terms'):
            if field in data and data[field] != getattr(draft, field):
                setattr(draft, field, data[field])
                edited = True
        draft.draft_version += 1
        review_state(draft, row, latest, account, request.user, data.get('request_review'), edited, now)
        draft.last_save_id, draft.last_save_hash, draft.updated_by = data['save_id'], fingerprint, request.user
        draft.save()
        return Response(draft_payload(row, account, membership, repriced))


class QuoteDraftReprice(APIView):
    @extend_schema(operation_id='v1_accounts_requests_draft_reprice', request=QuoteDraftRepriceRequest,
                   responses={200: QuoteDraftSerializer, 409: QuoteDraftConflict},
                   description='Aplica tus precios sugeridos al borrador: blank completa precios vacíos ("Aplicar precios sugeridos"), engine recalcula '
                               'los precios de tu lista ("Recalcular precios sugeridos") y lines reemplaza los precios de los artículos indicados.')
    @transaction.atomic
    def post(self, request, account_id, pk):
        account, membership, row = supplier_order(request.user, account_id, pk, lock=True)
        serializer = QuoteDraftRepriceRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if row.status not in EDITABLE_STATUSES:
            raise RequestConflict(NOT_EDITABLE_DETAIL)
        lines, scope, targets = order_lines(row), data['scope'], set(data.get('order_line_ids', []))
        if targets - set(lines):
            raise ValidationError({'order_line_ids': 'Uno de los artículos no pertenece a esta orden.'})
        # Saves and reprices share the last save id: a lost-response retry returns the draft, another use of the id is refused.
        fingerprint = hashlib.sha256(json.dumps({'reprice': data}, sort_keys=True, default=str).encode()).hexdigest()
        latest = latest_quotation(row)
        draft = current_draft(row, latest)
        if draft and draft.last_save_id == data['save_id']:
            if draft.last_save_hash != fingerprint:
                raise RequestConflict('Este identificador ya se usó para otro cambio del borrador.')
            return Response(draft_payload(row, account, membership))
        if data['expected_draft_version'] != (draft.draft_version if draft else 0):
            return draft_conflict(row, account, membership)
        now, settings_row, profile = timezone.now(), pricing_settings(account), client_profile(account, row.client_id)
        draft, saved = ensure_draft(row, account, request.user, latest, draft, lines, settings_row, profile, now)
        pricing = priced(row, account, draft.currency, {pk: {'quantity': line.quantity} for pk, line in saved.items()}, [lines[pk] for pk in saved], settings_row, profile)
        changed, repriced = [], []
        for pk, line in saved.items():
            result, before = pricing[pk], (line.unit_price, line.price_source, line.engine_fingerprint)
            if result.unit_price is not None and ((scope == 'blank' and line.unit_price is None) or (scope == 'lines' and pk in targets)):
                apply_engine(line, result)
            elif scope == 'engine' and line.price_source == 'engine':
                # Recalculating follows the list, including a price it no longer has: the line is then blank and must be completed.
                if result.unit_price is not None:
                    apply_engine(line, result)
                else:
                    apply_price(line, None, None)
            if (line.unit_price, line.price_source, line.engine_fingerprint) != before:
                line.updated_at = now
                changed.append(line)
                if line.unit_price != before[0]:
                    repriced.append(repriced_line(line, before[0], scope))
        DealQuotationDraftLine.objects.bulk_update(changed, LINE_UPDATE_FIELDS)
        draft.draft_version += 1
        # A reprice that moved a price changes what the client would receive, so it withdraws a pending approval request.
        review_state(draft, row, latest, account, request.user, None, bool(repriced), now)
        draft.last_save_id, draft.last_save_hash, draft.updated_by = data['save_id'], fingerprint, request.user
        draft.pricing_context = pricing_context(settings_row, profile, now)
        draft.save()
        return Response(draft_payload(row, account, membership, repriced))


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
            # The virtual draft has none of the applied proposals, so the assistant's decisions on this revision start over.
            QuoteAssistantRun.objects.filter(order=row, base_quotation=draft.base_quotation).update(decisions={})
            record_pricing_event(account, request.user, 'draft_discarded', client_id=row.client_id, order=row, object_id=str(row.pk),
                                 payload={'draft_version': draft.draft_version, 'save_id': str(data['save_id'])})
        return Response(draft_payload(row, account, membership))


class QuoteAssistantThrottle(UserRateThrottle):
    scope, rate = 'quote_assistant', '10/min'


ASSISTANT_ERRORS = {403: OpenApiResponse(description='El asistente no está activado para la cuenta o su presupuesto es 0.'),
                    429: OpenApiResponse(description='Se alcanzó un límite de uso del asistente.'),
                    502: OpenApiResponse(description='La IA falló o devolvió algo que no se pudo verificar; el borrador no cambió.'),
                    503: OpenApiResponse(description='El asistente de IA no está configurado en el servidor.')}


def assistant_state(row, account, *, run=None):
    return QuoteAssistantStateSerializer(assistant_block(row, pricing_settings(account), latest_quotation(row), order_lines(row), run=run)).data


class QuoteAssistantView(APIView):
    def get_throttles(self):
        # A soft per-worker limit on starting runs; the database caps are the real budget.
        return [*super().get_throttles(), QuoteAssistantThrottle()] if self.request.method == 'POST' else super().get_throttles()

    def throttled(self, request, wait):
        raise Throttled(wait, 'Demasiadas solicitudes al asistente. Espera un momento y vuelve a intentarlo.')

    @extend_schema(operation_id='v1_accounts_requests_draft_assistant_retrieve', responses=QuoteAssistantStateSerializer,
                   description='Estado del asistente de cotización con IA y su última interpretación para esta versión. No escribe nada.')
    def get(self, request, account_id, pk):
        account, _, row = supplier_order(request.user, account_id, pk)
        return Response(assistant_state(row, account))

    @extend_schema(operation_id='v1_accounts_requests_draft_assistant_create', request=QuoteAssistantRunRequest,
                   responses={200: QuoteAssistantStateSerializer, 409: QuoteDraftConflict, **ASSISTANT_ERRORS},
                   description='Interpreta las notas, el motivo del ajuste y la conversación del cliente en propuestas revisables. Nunca cambia el '
                               'borrador ni propone precios. Repetir run_id devuelve la misma interpretación; una igual de los últimos 30 días no tiene costo.')
    def post(self, request, account_id, pk):
        now = timezone.now()
        # First transaction: the order lock, the checks and the claim. The provider is then called with no lock held.
        with transaction.atomic():
            account, membership, row = supplier_order(request.user, account_id, pk, lock=True)
            serializer = QuoteAssistantRunRequest(data=request.data)
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            existing = QuoteAssistantRun.objects.select_related('created_by').filter(pk=data['run_id']).first()
            if existing:
                if existing.order_id != row.pk:
                    raise RequestConflict('Este identificador ya se usó para otra interpretación.')
                if existing.status == 'claimed':
                    raise RequestConflict(IN_PROGRESS)
                if existing.status == 'failed':
                    raise QuoteAssistantProviderError(existing.error or None)
                return Response(assistant_state(row, account, run=existing))
            if row.status not in EDITABLE_STATUSES:
                raise RequestConflict(NOT_EDITABLE_DETAIL)
            model = ensure_enabled(pricing_settings(account))
            latest = latest_quotation(row)
            draft = current_draft(row, latest)
            if data['expected_draft_version'] != (draft.draft_version if draft else 0):
                return draft_conflict(row, account, membership)
            lines = list(order_lines(row).values())
            if len(lines) > MAX_LINES:
                raise RequestConflict(TOO_MANY_LINES)
            payload = build_input(row, latest, lines)
            if not has_client_text(payload):
                raise RequestConflict(NO_CLIENT_TEXT)
            run, provider_request = start_run(row, account, request.user, data['run_id'], latest, draft.draft_version if draft else 0, lines, payload, model, now)
        if provider_request is not None:
            run = execute_run(run, request.user, row, provider_request, lines, payload['texts'])
        return Response(assistant_state(row, account, run=run))


class QuoteAssistantDecisions(APIView):
    @extend_schema(operation_id='v1_accounts_requests_draft_assistant_decisions', request=QuoteAssistantDecisionsRequest,
                   responses={200: QuoteDraftSerializer, 409: QuoteDraftConflict},
                   description='Aplica o descarta propuestas del asistente. Aplicar es un guardado del borrador: cambia solo cantidades (origen assistant, '
                               'con el precio de tu lista recalculado), la nota interna o las condiciones. Las solicitudes de precio solo se descartan.')
    @transaction.atomic
    def post(self, request, account_id, pk, run_id):
        account, membership, row = supplier_order(request.user, account_id, pk, lock=True)
        serializer = QuoteAssistantDecisionsRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if row.status not in EDITABLE_STATUSES:
            raise RequestConflict(NOT_EDITABLE_DETAIL)
        run = get_object_or_404(QuoteAssistantRun.objects.select_for_update().filter(order=row, status='completed'), pk=run_id)
        latest = latest_quotation(row)
        if run.base_quotation_id != (latest.pk if latest else None):
            raise RequestConflict('Esta interpretación corresponde a una versión anterior de la cotización. Vuelve a interpretar la solicitud.')
        proposals = {proposal['id']: proposal for proposal in run.output.get('proposals', [])}
        for decision in data['decisions']:
            proposal = proposals.get(decision['proposal_id'])
            if proposal is None:
                raise ValidationError({'decisions': 'Una de las propuestas no existe en esta interpretación.'})
            if decision['action'] == 'apply' and proposal['kind'] not in APPLICABLE:
                raise ValidationError({'decisions': NOT_APPLICABLE[proposal['kind']]})
        fingerprint = hashlib.sha256(json.dumps({'assistant': str(run.pk), 'decisions': data['decisions']}, sort_keys=True).encode()).hexdigest()
        draft = current_draft(row, latest)
        if draft and draft.last_save_id == data['save_id']:
            if draft.last_save_hash != fingerprint:
                raise RequestConflict('Este identificador ya se usó para otro cambio del borrador.')
            return Response(draft_payload(row, account, membership))
        # A proposal is decided once; repeating the same decision is a no-op.
        pending = []
        for decision in data['decisions']:
            taken = run.decisions.get(decision['proposal_id'])
            if taken and taken['action'] != DECISION_STATES[decision['action']]:
                raise RequestConflict('Una de las propuestas ya se aplicó o se descartó.')
            if not taken:
                pending.append(decision)
        applies, now, repriced = [proposals[decision['proposal_id']] for decision in pending if decision['action'] == 'apply'], timezone.now(), []
        # Dismissing never touches the draft; applying is a draft save, versioned and retry-safe like any other.
        if applies:
            if data['expected_draft_version'] != (draft.draft_version if draft else 0):
                return draft_conflict(row, account, membership)
            settings_row, profile, lines = pricing_settings(account), client_profile(account, row.client_id), order_lines(row)
            draft, saved = ensure_draft(row, account, request.user, latest, draft, lines, settings_row, profile, now)
            changed, requantified, terms, edited = {}, {}, draft.terms, False
            for proposal in applies:
                line = saved.get(uuid.UUID(proposal['order_line_id'])) if proposal['order_line_id'] else None
                if proposal['kind'] in ('quantity_change', 'remove_line') and proposal['quantity'] != line.quantity:
                    requantified.setdefault(line.order_line_id, line.quantity)
                    line.quantity, line.quantity_source, changed[line.order_line_id], edited = proposal['quantity'], 'assistant', line, True
                elif proposal['kind'] == 'line_note':
                    line.note, changed[line.order_line_id] = ' · '.join(filter(None, [line.note, proposal['text']])), line
                    if len(line.note) > 500:
                        raise ValidationError({'decisions': f'La nota interna de {lines[line.order_line_id].codigo} superaría 500 caracteres.'})
                elif proposal['kind'] == 'terms':
                    terms = '\n'.join(filter(None, [terms, proposal['text']]))
            if len(terms) > 5000:
                raise ValidationError({'decisions': 'Las condiciones superarían 5.000 caracteres.'})
            # The assistant's quantity is priced like a typed one: the deterministic engine reprices a current engine price (a volume
            # rule may apply). The assistant itself never produces or changes a price.
            if requantified:
                current = engine_followers(row, account, draft.currency, saved, lines, requantified, settings_row, profile)
                pricing = priced(row, account, draft.currency, {pk: {'quantity': line.quantity} for pk, line in saved.items()}, [lines[pk] for pk in saved],
                                 settings_row, profile)
                for pk in current:
                    line, before = saved[pk], saved[pk].unit_price
                    if pricing[pk].unit_price is not None:
                        apply_engine(line, pricing[pk])
                        if before != line.unit_price:
                            repriced.append(repriced_line(line, before, 'quantity'))
            for line in changed.values():
                line.updated_at = now
            DealQuotationDraftLine.objects.bulk_update(list(changed.values()), LINE_UPDATE_FIELDS)
            if terms != draft.terms:
                draft.terms, edited = terms, True
            draft.draft_version += 1
            review_state(draft, row, latest, account, request.user, None, edited, now)
            draft.last_save_id, draft.last_save_hash, draft.updated_by = data['save_id'], fingerprint, request.user
            draft.save()
        for decision in pending:
            run.decisions[decision['proposal_id']] = {'action': DECISION_STATES[decision['action']], 'user_id': request.user.pk, 'at': now.isoformat(),
                                                      'draft_version': draft.draft_version if applies else None}
        if pending:
            run.save(update_fields=['decisions'])
        if applies:
            record_pricing_event(account, request.user, 'assistant_applied', client_id=row.client_id, order=row, object_id=str(run.pk), payload={
                'run_id': str(run.pk), 'draft_version': draft.draft_version, 'kinds': dict(Counter(proposal['kind'] for proposal in applies)),
                'applied': [decision['proposal_id'] for decision in pending if decision['action'] == 'apply'],
                'dismissed': [decision['proposal_id'] for decision in pending if decision['action'] == 'dismiss']})
        return Response(draft_payload(row, account, membership, repriced))


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
    assistant_run_id = serializers.UUIDField(allow_null=True, help_text='Interpretación del asistente aplicada a este artículo; null si ninguna.')


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
                                                                         'price_source', 'quantity_source', 'engine_fingerprint', 'explanation', 'assistant_run_id')},
                       'exceptions': trace(line.audit.exceptions)} for line in lines]}).data)
