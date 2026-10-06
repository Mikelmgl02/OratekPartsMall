"""Account-scoped deal transitions. A row lock and version protect every decision."""
import hashlib
import json
from collections import Counter
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

from .availability import record_accept_check, shortfall_since_quote
from .models import Membership
from .pricing_engine import ENGINE_VERSION, client_profile, price_lines
from .pricing_models import pricing_settings, record_pricing_event
from .quotation_exceptions import audit_findings, compute_exceptions, publish_blockers
from .quote_assistant import applied_runs
from .quote_assistant_models import QuoteAssistantRun
from .quote_draft_models import DealQuotationAudit, DealQuotationDraft, DealQuotationLineAudit
from .quote_drafts import stock_by_line
from .request_models import (SupplierRequest, DealQuotation, DealQuotationLine, DealEvent,
                             DealMessage, DealCommand)
from .request_views import (RequestConflict, WholeRequestQuantity, request_detail_queryset,
                            request_detail, sent_request_queryset, sent_request_summary)
from .views import PERMISSION_RANK, account_for

EXCEPTIONS_DETAIL = 'Revisa las alertas antes de enviar.'
RETURN_SHORTFALL_DETAIL = 'Las existencias cambiaron desde esta versión. Prepara una nueva versión y confirma las alertas.'
# Shown to the client: it must never carry quantities or any other digit.
ACCEPT_SHORTFALL_DETAIL = ('El proveedor debe confirmar la disponibilidad de algunos artículos antes de cerrar el acuerdo. '
                           'Solicita un ajuste para que el proveedor prepare una versión actualizada.')
PUBLISH_DENIED = 'Tu permiso en la cuenta no permite publicar cotizaciones. Solicita la aprobación de un administrador de tu cuenta.'
SETTINGS_SNAPSHOT = ('version', 'over_stock_policy', 'over_request_policy', 'accept_shortfall_policy', 'publish_min_permission', 'prefill_quantity',
                     'usd_pab_parity')


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
    # No default: legacy payloads keep a byte-identical DealCommand fingerprint.
    draft_version = serializers.IntegerField(min_value=1, required=False)

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


def command_fingerprint(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def bind_draft(row, latest, data):
    """A quote is bound to the saved draft when one exists for the latest revision or the payload names a draft_version."""
    draft = DealQuotationDraft.objects.prefetch_related('lines').filter(order=row, base_quotation=latest).first()
    if draft is None and 'draft_version' not in data:
        return None
    if draft is None or draft.draft_version != data.get('draft_version'):
        raise RequestConflict('Hay un borrador guardado; revísalo antes de publicar.' if draft and 'draft_version' not in data
                              else 'El borrador cambió. Revísalo antes de publicar.')
    saved = {line.order_line_id: line for line in draft.lines.all()}
    def same(line):
        value = saved.get(line['order_line_id'])
        # A blank price with quantity 0 counts as 0.00.
        return value is not None and value.quantity == line['quantity'] and line['unit_price'] == (
            value.unit_price if value.unit_price is not None else Decimal('0') if value.quantity == 0 else None)
    if draft.currency != data['currency'] or draft.terms != data['terms'] or len(saved) != len(data['lines']) or not all(map(same, data['lines'])):
        raise RequestConflict('La cotización no coincide con el borrador guardado.')
    return draft


def quote_exceptions(row, account, data, draft, settings_row=None):
    """Exceptions recomputed with live stock and live suggestions: the draft's own values and acknowledgements, or the payload on the
    legacy path (which carries no price source, so it never raises stale_price)."""
    settings_row, lines = settings_row or pricing_settings(account), {line.pk: line for line in row.lines.select_related('supplier_item')}
    drafted = {line.order_line_id: line for line in draft.lines.all()} if draft else {}
    offered = {line['order_line_id']: line for line in data['lines']}
    values = {pk: {field: getattr(drafted[pk], field) for field in ('quantity', 'unit_price', 'quantity_source', 'price_source', 'engine_fingerprint')}
              if pk in drafted else {'quantity': offered[pk]['quantity'], 'unit_price': offered[pk]['unit_price']} for pk in lines}
    stock, profile = stock_by_line(lines.values()), client_profile(account, row.client_id)
    pricing = price_lines(account, row.client_id, data['currency'], [(line, values[pk]['quantity']) for pk, line in lines.items()], settings_row=settings_row,
                          profile=profile)
    result = compute_exceptions(list(lines.values()), values, stock, settings_row, pricing=pricing,
                                acknowledgements={pk: line.acknowledgements for pk, line in drafted.items()},
                                order_acknowledgements=draft.order_acknowledgements if draft else (), no_profile=profile is None and row.client_id != account.pk)
    return {'settings': settings_row, 'stock': stock, 'drafted': drafted, 'result': result, 'pricing': pricing, 'profile': profile}


def write_quote_audit(quotation, prior, request, account, draft, context, permission):
    """Supplier-only trace of a publication: live stock at quote time, server-derived sources and every alert with its confirmation."""
    settings_row, stock, drafted, result, pricing = context['settings'], context['stock'], context['drafted'], context['result'], context['pricing']
    previous = {line.order_line_id: line for line in prior.lines.all()} if prior else {}
    configured = any(suggestion.configured for suggestion in pricing.values())
    profile, assisted = context['profile'], applied_runs(quotation.order, prior) if draft else {}
    DealQuotationAudit.objects.create(quotation=quotation, draft_version=draft.draft_version if draft else None, publisher=request.user,
                                      publisher_permission=permission, engine_version=ENGINE_VERSION if configured else '',
                                      profile_id=profile.pk if profile else None, profile_version=profile.version if profile else None,
                                      settings_snapshot={field: getattr(settings_row, field) for field in SETTINGS_SNAPSHOT},
                                      order_exceptions=audit_findings(result['order']))
    def sources(line):
        drafted_line, prior_line, suggestion = drafted.get(line.order_line_id), previous.get(line.order_line_id), pricing[line.order_line_id]
        # Derived on the server, never taken from the client: the live suggestion, then the previous revision, else a typed price.
        price = 'none' if drafted_line and drafted_line.unit_price is None else 'engine' if suggestion.unit_price is not None and suggestion.unit_price == line.unit_price \
            else 'previous' if prior_line and prior_line.unit_price == line.unit_price else 'manual'
        quantity = drafted_line.quantity_source if drafted_line else 'previous' if prior_line and prior_line.quantity == line.quantity \
            else 'requested' if line.quantity == line.order_line.quantity else 'manual'
        return price, quantity
    audits = []
    for line in quotation.lines.select_related('order_line'):
        entry, (price_source, quantity_source), suggestion = stock[line.order_line_id], sources(line), pricing[line.order_line_id]
        audits.append(DealQuotationLineAudit(line=line, available_at_quote=entry['available'], identity_ok_at_quote=entry['identity_ok'],
                                             price_source=price_source, quantity_source=quantity_source, suggested_price=suggestion.unit_price,
                                             engine_fingerprint=suggestion.fingerprint if suggestion.configured else '',
                                             explanation=suggestion.explanation if suggestion.configured else {},
                                             exceptions=audit_findings(result['lines'][line.order_line_id]), assistant_run_id=assisted.get(line.order_line_id)))
    DealQuotationLineAudit.objects.bulk_create(audits)
    found = [item for items in [result['order'], *result['lines'].values()] for item in items if item['severity'] == 'confirm']
    record_pricing_event(account, request.user, 'quotation_published', client_id=quotation.order.client_id, order=quotation.order, object_id=str(quotation.pk),
                         payload={'revision': quotation.revision, 'draft_version': draft.draft_version if draft else None,
                                  'confirmed': sum(item['acknowledged'] for item in found), 'unacknowledged': sum(not item['acknowledged'] for item in found),
                                  'price_sources': dict(Counter(audit.price_source for audit in audits))})


def publish_permission(account, user):
    """The caller's permission and the supplier's settings, read for a supplier decision: quote and return_quote need publish_min_permission."""
    permission = Membership.objects.filter(account=account, user=user).values_list('permission', flat=True).first()
    settings_row = pricing_settings(account)
    if PERMISSION_RANK.get(permission, -1) < PERMISSION_RANK[settings_row.publish_min_permission]:
        raise PermissionDenied(PUBLISH_DENIED)
    return permission, settings_row


class DealActions(APIView):
    @extend_schema(request=DealActionSerializer, responses=OpenApiTypes.OBJECT,
                   description='Decisiones del acuerdo. quote y return_quote exigen el permiso mínimo para publicar del proveedor (403 antes de comprobar '
                               'la versión). Repetir un operation_id de la misma cuenta con el mismo contenido devuelve el resultado guardado.')
    @transaction.atomic
    def post(self, request, account_id, pk):
        account, row = participant(request.user, account_id, pk, lock=True)
        serializer = DealActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        fingerprint = command_fingerprint(data)
        # The replay key is (order, account, unguessable operation_id) and returns before every gate, the publish permission included: a
        # same-account retry gets the stored result of a decision already taken (readable by any member) and never repeats or re-authorizes it.
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
        # Opt-in per supplier (staff by default): checked before the version, so a member without permission gets 403, never a 409.
        permission, settings_row = publish_permission(account, request.user) if supplier_action else (None, None)
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
            draft = bind_draft(row, latest, data)
            context = quote_exceptions(row, account, data, draft, settings_row)
            # Bound quotes need every alert reviewed; the legacy (draftless) path enforces only explicit block policies.
            blockers = publish_blockers(context['result'], legacy=draft is None)
            if blockers:
                return Response({'detail': EXCEPTIONS_DETAIL, 'exceptions': blockers}, status=409)
            prior = latest
            total = sum((line['unit_price'] * line['quantity'] for line in data['lines']), Decimal('0')).quantize(Decimal('0.01'))
            if total >= Decimal('100000000000000'):
                raise ValidationError('El total de la cotización supera el importe permitido.')
            latest = DealQuotation.objects.create(order=row, revision=(latest.revision + 1) if latest else 1,
                currency=data['currency'], terms=data['terms'], total=total,
                published_by=request.user, supplier_confirmed_at=now)
            DealQuotationLine.objects.bulk_create([DealQuotationLine(quotation=latest,
                order_line_id=line['order_line_id'], quantity=line['quantity'], unit_price=line['unit_price']) for line in data['lines']])
            write_quote_audit(latest, prior, request, account, draft, context, permission)
            row.status = 'quoted'
            description = f'COTIZACIÓN V{latest.revision} CONFIRMADA POR EL PROVEEDOR Y ENVIADA AL CLIENTE.'
            # The published revision consumes the draft (and any row prepared on an older revision).
            DealQuotationDraft.objects.filter(order=row).delete()
        elif action == 'accept':
            if row.status != 'quoted':
                raise RequestConflict('La cotización no está disponible para confirmar.')
            # Stock is read without a lock (lock order: this order row, then items); revisions published before the trace are exempt.
            check = shortfall_since_quote(latest, {line.pk: line for line in row.lines.select_related('supplier_item')})
            if check is not None:
                policy = pricing_settings(row.supplier).accept_shortfall_policy
                outcome = 'ok' if not check['short'] else 'blocked' if policy == 'block' else 'accepted_with_shortfall'
                record_accept_check(check, outcome, now)
                if check['short']:
                    record_pricing_event(row.supplier, request.user, 'accept_blocked_shortfall' if outcome == 'blocked' else 'accept_with_shortfall',
                                         client_id=row.client_id, order=row, object_id=str(latest.pk),
                                         payload={'revision': latest.revision, 'lines': [line for line in check['lines'] if line['shortfall']]})
                if outcome == 'blocked':
                    # Returned rather than raised so the trace commits; no DealCommand is stored and the deal stays quoted.
                    return Response({'detail': ACCEPT_SHORTFALL_DETAIL}, status=409)
            latest.client_confirmed_at = now
            latest.client_confirmed_by = request.user
            latest.save(update_fields=['client_confirmed_at', 'client_confirmed_by'])
            row.status = 'handshaked'
            row.handshaked_at = now
            DealQuotationDraft.objects.filter(order=row).delete()
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
            check = shortfall_since_quote(latest, {line.pk: line for line in row.lines.select_related('supplier_item')})
            if check and check['short']:
                raise RequestConflict(RETURN_SHORTFALL_DETAIL)
            row.status = 'quoted'
            description = data['reason'] or 'EL PROVEEDOR DEVOLVIÓ LA MISMA COTIZACIÓN PARA CONFIRMACIÓN.'
            DealQuotationDraft.objects.filter(order=row).delete()
            # The discarded draft took the applied assistant proposals with it; a later adjustment of this revision starts over.
            QuoteAssistantRun.objects.filter(order=row, base_quotation=latest).update(decisions={})
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
        # Privacy disclosure (decision D3): both parties learn that the supplier's AI assistant may read this conversation. Only the flag
        # travels; nothing about the assistant's runs or proposals ever does.
        return Response({'results': [message_data(message) for message in page], 'cursor': cursor,
                         'has_more': messages.filter(id__gt=cursor).exists(),
                         'has_earlier': bool(page) and messages.filter(id__lt=page[0].pk).exists(),
                         'assistant_notice': pricing_settings(row.supplier).assistant_enabled})

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
