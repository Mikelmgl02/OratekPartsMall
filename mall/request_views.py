"""Submit account requests and let each supplier review only its own lines."""
import hashlib
import json
import uuid

from django.db import IntegrityError, transaction
from django.db.models import Case, CharField, Count, Exists, Prefetch, Q, Sum, OuterRef, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .availability import line_item_identity_ok
from .models import Account, Part, SupplierItem
from .quote_draft_models import DealQuotationDraft
from .request_models import ClientRequestSubmission, SupplierRequest, SupplierRequestLine, RequestContribution, DealEvent, DealQuotation, DealQuotationLine
from .views import account_for


class RequestConflict(APIException):
    status_code = 409
    default_detail = 'La solicitud cambió. Revisa el borrador antes de enviarlo otra vez.'


class WholeRequestQuantity(serializers.IntegerField):
    def to_internal_value(self, data):
        if isinstance(data, bool):
            self.fail('invalid')
        return super().to_internal_value(data)


class RequestSubmissionLine(serializers.Serializer):
    supplier_item_id = serializers.UUIDField()
    quantity = WholeRequestQuantity(min_value=1, max_value=9999)
    expected_part_id = serializers.UUIDField(required=False)
    expected_codigo = serializers.CharField(required=False, max_length=120)
    expected_brand = serializers.CharField(required=False, max_length=120, allow_blank=True)

    def validate_expected_codigo(self, value):
        return value.strip().upper()

    def validate_expected_brand(self, value):
        return value.strip().upper()


class RequestSubmission(serializers.Serializer):
    submission_id = serializers.UUIDField()
    lines = RequestSubmissionLine(many=True, min_length=1)
    notes = serializers.CharField(max_length=10000, required=False, allow_blank=True, default='')

    def validate_lines(self, lines):
        identifiers = [line['supplier_item_id'] for line in lines]
        if len(identifiers) != len(set(identifiers)):
            raise serializers.ValidationError('No repitas el mismo artículo del proveedor en una solicitud.')
        return sorted(lines, key=lambda line: str(line['supplier_item_id']))

    def validate_notes(self, value):
        return value.strip().upper()


class RequestFilters(serializers.Serializer):
    search = serializers.CharField(required=False, allow_blank=True, max_length=200)
    status = serializers.ChoiceField(required=False, choices=[value for value, _ in SupplierRequest.STATUS_CHOICES])


def quote_summary_fields():
    quote = DealQuotation.objects.filter(order_id=OuterRef('pk')).annotate(
        offered_units=Coalesce(Sum('lines__quantity'), 0)).order_by('-revision')
    return {key: Subquery(quote.values(field)[:1]) for key, field in [
        ('quotation_revision', 'revision'), ('quotation_total', 'total'),
        ('quotation_currency', 'currency'), ('quoted_unit_count', 'offered_units')]}


def request_queryset(supplier):
    return SupplierRequest.objects.filter(supplier=supplier).annotate(
        line_count=Count('lines'), unit_count=Coalesce(Sum('lines__quantity'), 0), **quote_summary_fields()).order_by('-created_at', 'id')


def request_summary(row):
    return {'id': str(row.pk), 'reference': row.reference,
            'client': {'id': str(row.client_id), 'name': row.client_name},
            'status': row.status, 'version': row.version, 'created_at': row.created_at.isoformat(),
            'updated_at': row.updated_at.isoformat(),
            'handshaked_at': row.handshaked_at.isoformat() if row.handshaked_at else None,
            'reviewed_at': row.reviewed_at.isoformat() if row.reviewed_at else None,
            'line_count': row.line_count, 'unit_count': row.unit_count,
            'quotation_revision': row.quotation_revision, 'quoted_unit_count': row.quoted_unit_count,
            'quotation_total': str(row.quotation_total) if row.quotation_total is not None else None,
            'quotation_currency': row.quotation_currency}


def availability_alert():
    """Supplier list only: the latest revision's accept check when stock blocked the client's confirmation or it closed with a shortfall."""
    alerts = ['blocked', 'accepted_with_shortfall']
    latest = DealQuotation.objects.filter(order_id=OuterRef('pk')).order_by('-revision').annotate(alert=Case(
        *[When(audit__accept_check__result=value, then=Value(value)) for value in alerts], default=None, output_field=CharField()))
    return Subquery(latest.values('alert')[:1])


def draft_state():
    """Supplier list only: the status of the private draft prepared on the latest revision of an order still open to quoting."""
    quotations = DealQuotation.objects.filter(order_id=OuterRef('order_id'))
    latest = Q(base_quotation=Subquery(quotations.order_by('-revision').values('pk')[:1])) | Q(base_quotation__isnull=True, has_quotation=False)
    drafts = DealQuotationDraft.objects.filter(order_id=OuterRef('pk')).annotate(has_quotation=Exists(quotations)).filter(latest)
    return Case(When(status__in=['reviewed', 'adjustment'], then=Subquery(drafts.values('status')[:1])), default=None, output_field=CharField())


def supplier_request_summary(row):
    """Supplier list rows only: private draft and availability badges never reach the client's views or DealCommand results."""
    return {**request_summary(row), 'draft_state': getattr(row, 'draft_state', None),
            'availability_alert': getattr(row, 'availability_alert', None)}


def request_detail(row):
    from .deal_views import deal_data
    return {**request_summary(row), **deal_data(row), 'notes': row.notes,
            'lines': [{'id': str(line.pk), 'supplier_item_id': str(line.supplier_item_id),
                       'supplier_invent_id': line.supplier_invent_id, 'part_id': str(line.part_id),
                       'sku': line.sku, 'name': line.name, 'codigo': line.codigo, 'brand': line.brand,
                       'description': line.description, 'quantity': line.quantity,
                       'stock': request_line_stock(line, row.supplier_id)}
                      for line in row.lines.all()]}


def request_line_stock(line, supplier_id):
    """Current supplier-only stock, provided the captured item still matches."""
    item = line.supplier_item
    if not line_item_identity_ok(line, supplier_id):
        return None
    return {'reported_quantity': item.reported_quantity, 'reserved_quantity': item.reserved_quantity,
            'available_quantity': item.available_quantity,
            'shortfall': max(0, line.quantity - item.available_quantity),
            'updated_at': item.updated_at.isoformat()}


def request_detail_queryset(supplier):
    return request_queryset(supplier).prefetch_related(
        Prefetch('lines', queryset=SupplierRequestLine.objects.select_related('supplier_item')))


def sent_request_queryset(client):
    return SupplierRequest.objects.filter(client=client).annotate(
        line_count=Count('lines'), unit_count=Coalesce(Sum('lines__quantity'), 0), **quote_summary_fields()).order_by('-created_at', 'id')


def sent_request_summary(row):
    summary = request_summary(row)
    summary.pop('client')
    return {**summary, 'supplier': {'id': str(row.supplier_id), 'name': row.supplier_name},
            'submission_id': str(row.submission.submission_id)}


class ClientSentRequests(APIView):
    @extend_schema(operation_id='v1_accounts_sent_requests_list', parameters=[OpenApiParameter('search', OpenApiTypes.STR), OpenApiParameter('status', OpenApiTypes.STR),
                               OpenApiParameter('page', OpenApiTypes.INT)], responses=OpenApiTypes.OBJECT)
    def get(self, request, account_id):
        client = account_for(request.user, account_id, 'client')
        filters = RequestFilters(data=request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        rows = sent_request_queryset(client).select_related('submission')
        if data.get('status'):
            rows = rows.filter(status=data['status'])
        if data.get('search'):
            term = data['search'].strip()
            matching_lines = SupplierRequestLine.objects.filter(
                Q(sku__icontains=term) | Q(name__icontains=term) | Q(codigo__icontains=term)
                | Q(brand__icontains=term) | Q(description__icontains=term))
            rows = rows.filter(Q(reference__icontains=term) | Q(supplier_name__icontains=term)
                               | Q(pk__in=matching_lines.values('request_id')))
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response([sent_request_summary(row) for row in page])


class ClientSentRequestDetail(APIView):
    @extend_schema(operation_id='v1_accounts_sent_requests_retrieve', responses=OpenApiTypes.OBJECT)
    def get(self, request, account_id, pk):
        client = account_for(request.user, account_id, 'client')
        row = get_object_or_404(sent_request_queryset(client).select_related('submission').prefetch_related('lines'), pk=pk)
        from .deal_views import deal_data
        return Response({**sent_request_summary(row), **deal_data(row), 'notes': row.notes,
                         'lines': [{'id': str(line.pk), 'part_id': str(line.part_id), 'sku': line.sku,
                                    'name': line.name, 'codigo': line.codigo, 'brand': line.brand,
                                    'description': line.description, 'quantity': line.quantity}
                                   for line in row.lines.all()]})


class ClientPartRequestState(APIView):
    """Only the active client's requested and agreed quantities, never supplier stock."""
    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, account_id, part_id):
        client = account_for(request.user, account_id, 'client')
        part = get_object_or_404(Part, pk=part_id, active=True, merged_into__isnull=True)
        family = Part.objects.filter(Q(pk=part.pk) | Q(merged_into=part)).values('pk')
        # A closed deal counts the units of the quotation the client accepted, which can differ from those requested (or be 0).
        agreed = DealQuotationLine.objects.filter(order_line=OuterRef('pk'), quotation__client_confirmed_at__isnull=False).order_by('-quotation__revision').values('quantity')[:1]
        lines = SupplierRequestLine.objects.filter(request__client=client, part_id__in=family).annotate(agreed_quantity=Coalesce(Subquery(agreed), 0))
        quantities = {
            'sent_quantity': Coalesce(Sum('quantity'), 0),
            'pending_quantity': Coalesce(Sum('quantity', filter=Q(request__status='pending')), 0),
            'reviewed_quantity': Coalesce(Sum('quantity', filter=Q(request__status='reviewed')), 0),
            'quoted_quantity': Coalesce(Sum('quantity', filter=Q(request__status='quoted')), 0),
            'adjustment_quantity': Coalesce(Sum('quantity', filter=Q(request__status='adjustment')), 0),
            'handshaked_quantity': Coalesce(Sum('agreed_quantity', filter=Q(request__status='handshaked')), 0),
        }
        totals = lines.aggregate(**quantities)
        items = lines.values('supplier_item_id', 'request__supplier_id', 'codigo', 'brand').annotate(**quantities).order_by(
            'codigo', 'brand', 'supplier_item_id', 'request__supplier_id')
        return Response({'part_id': str(part.pk), 'totals': totals,
                         'items': [{'supplier_item_id': str(row['supplier_item_id']),
                                    'supplier_id': str(row['request__supplier_id']),
                                    'codigo': row['codigo'], 'brand': row['brand'],
                                    **{key: row[key] for key in quantities}} for row in items]})


def submission_result(submission):
    if submission.result is not None:
        return submission.result
    requests = submission.requests.annotate(line_count=Count('lines'), unit_count=Coalesce(Sum('lines__quantity'), 0),
                                            **quote_summary_fields()).order_by('supplier_name', 'id')
    return {'submission_id': str(submission.submission_id), 'created_at': submission.created_at.isoformat(),
            'requests': [{'id': str(row.pk), 'reference': row.reference,
                          'supplier': {'id': str(row.supplier_id), 'name': row.supplier_name},
                          'line_count': row.line_count, 'unit_count': row.unit_count,
            'quotation_revision': row.quotation_revision, 'quoted_unit_count': row.quoted_unit_count,
            'quotation_total': str(row.quotation_total) if row.quotation_total is not None else None,
            'quotation_currency': row.quotation_currency} for row in requests]}


@transaction.atomic
def submit_request(*, client, actor, data):
    normalized = {'notes': data['notes'], 'lines': [{field: str(value) if isinstance(value, uuid.UUID) else value
                                                   for field, value in line.items()} for line in data['lines']]}
    fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
    client = Account.objects.select_for_update().get(pk=client.pk)
    if not client.active or not client.roles.filter(capability='client').exists():
        raise ValidationError('Tu cuenta activa necesita un rol de cliente para enviar solicitudes.')
    previous = ClientRequestSubmission.objects.filter(client=client, submission_id=data['submission_id']).first()
    if previous:
        if previous.payload_hash != fingerprint:
            raise RequestConflict('Este identificador de envío ya se utilizó con otra solicitud.')
        # Lost responses remain safely retryable even after stock changes.
        return submission_result(previous)
    identifiers = [line['supplier_item_id'] for line in data['lines']]
    observed = list(SupplierItem.objects.filter(pk__in=identifiers).only('id', 'part_id'))
    part_ids = sorted({item.part_id for item in observed if item.part_id})
    # Merge operations lock parts before supplier rows. Lock in that same
    # order so canonical changes cannot race with historical line snapshots.
    parts = {part.pk: part for part in Part.objects.select_for_update().filter(pk__in=part_ids).order_by('pk')}
    items = {item.pk: item for item in SupplierItem.objects.select_for_update().filter(pk__in=identifiers).order_by('pk')}
    if len(items) != len(identifiers):
        raise ValidationError('Uno de los artículos ya no está disponible. Actualiza tu cesta.')
    suppliers = {account.pk: account for account in Account.objects.filter(pk__in={item.supplier_id for item in items.values()}).prefetch_related('roles')}
    quantities = {line['supplier_item_id']: line['quantity'] for line in data['lines']}
    expected = {line['supplier_item_id']: line for line in data['lines']}
    groups = {}
    for identifier in identifiers:
        item = items[identifier]
        supplier = suppliers[item.supplier_id]
        part = parts.get(item.part_id)
        selected = expected[identifier]
        if (('expected_part_id' in selected and selected['expected_part_id'] != item.part_id)
                or ('expected_codigo' in selected and selected['expected_codigo'] != item.codigo)
                or ('expected_brand' in selected and selected['expected_brand'] != item.brand)):
            raise RequestConflict(f'El SKU, código o marca del artículo {item.codigo} cambió desde que lo agregaste. Selecciónalo otra vez en el catálogo.')
        if item.part_id and item.part_id not in parts:
            raise RequestConflict('El SKU de un artículo cambió mientras se preparaba la solicitud. Actualiza tu cesta.')
        if (not supplier.active or not any(role.capability == 'supplier' for role in supplier.roles.all())
                or item.matching_status != 'matched' or not part or not part.active or part.merged_into_id):
            raise ValidationError(f'El artículo {item.codigo} ya no está disponible para solicitar una cotización.')
        # A quotation request expresses demand; the supplier confirms supply later.
        # Preserve the requested quantity even when current stock is insufficient.
        groups.setdefault(supplier.pk, []).append((item, part, quantities[identifier]))
    submission = ClientRequestSubmission.objects.create(client=client, actor=actor, submission_id=data['submission_id'],
                                                        payload_hash=fingerprint, notes=data['notes'])
    receipts = []
    for supplier_id in sorted(groups):
        selections = groups[supplier_id]
        supplier = suppliers[supplier_id]
        # All sends for a client serialize on its account row. The order row
        # also serializes against the supplier taking it into review.
        row = SupplierRequest.objects.select_for_update().filter(
            client=client, supplier=supplier, status='pending').order_by('created_at', 'id').first()
        appended = row is not None
        if row is None:
            identifier = uuid.uuid4()
            row = SupplierRequest.objects.create(id=identifier, submission=submission, client=client, supplier=supplier,
                client_name=client.name, supplier_name=supplier.name,
                reference=f'ORD-{timezone.localdate():%Y%m%d}-{identifier.hex[:8].upper()}', notes=data['notes'])
        else:
            row.version += 1
            if data['notes']:
                row.notes = '\n'.join(filter(None, [row.notes, data['notes']]))
            row.save(update_fields=['version', 'notes', 'updated_at'])
        captured = []
        for item, part, quantity in selections:
            line = row.lines.filter(supplier_item=item).first()
            if line:
                if (line.part_id != part.pk or line.codigo != item.codigo or line.brand != item.brand
                        or line.supplier_invent_id != item.supplier_invent_id):
                    raise RequestConflict('Un artículo de la orden pendiente cambió de código o SKU. Revisa esa orden antes de agregarlo otra vez.')
                if line.quantity + quantity > 9999:
                    raise ValidationError(f'La cantidad acumulada de {item.codigo} supera 9.999 unidades.')
                line.quantity += quantity
                line.save(update_fields=['quantity'])
            else:
                line = SupplierRequestLine.objects.create(request=row, supplier_item=item, part=part,
                    supplier_invent_id=item.supplier_invent_id, sku=part.sku, name=part.name,
                    codigo=item.codigo, brand=item.brand, description=item.description or part.description, quantity=quantity)
            captured.append({'supplier_item_id': str(item.pk), 'codigo': item.codigo, 'sku': part.sku, 'quantity': quantity})
        RequestContribution.objects.create(submission=submission, order=row, lines=captured, appended=appended)
        DealEvent.objects.create(order=row, actor=actor, account=client,
            kind='order_appended' if appended else 'order_sent',
            description=f'{sum(quantity for _, _, quantity in selections)} UNIDADES ENVIADAS')
        summary = sent_request_queryset(client).get(pk=row.pk)
        receipts.append({'id': str(row.pk), 'reference': row.reference,
                         'supplier': {'id': str(supplier.pk), 'name': supplier.name},
                         'line_count': summary.line_count, 'unit_count': summary.unit_count,
                         'added_unit_count': sum(quantity for _, _, quantity in selections), 'appended': appended})
    submission.result = {'submission_id': str(submission.submission_id), 'created_at': submission.created_at.isoformat(),
                         'requests': receipts}
    submission.save(update_fields=['result'])
    return submission.result



class AccountRequests(APIView):
    @extend_schema(operation_id='v1_accounts_requests_list', parameters=[OpenApiParameter('search', OpenApiTypes.STR), OpenApiParameter('status', OpenApiTypes.STR),
                               OpenApiParameter('page', OpenApiTypes.INT)], responses=OpenApiTypes.OBJECT)
    def get(self, request, account_id):
        supplier = account_for(request.user, account_id, 'supplier')
        filters = RequestFilters(data=request.query_params)
        filters.is_valid(raise_exception=True)
        data = filters.validated_data
        rows = request_queryset(supplier).annotate(availability_alert=availability_alert(), draft_state=draft_state())
        if data.get('status'):
            rows = rows.filter(status=data['status'])
        if data.get('search'):
            term = data['search'].strip()
            matching_lines = SupplierRequestLine.objects.filter(
                Q(sku__icontains=term) | Q(name__icontains=term) | Q(codigo__icontains=term)
                | Q(brand__icontains=term) | Q(description__icontains=term) | Q(supplier_invent_id__icontains=term))
            rows = rows.filter(Q(reference__icontains=term) | Q(client_name__icontains=term)
                               | Q(pk__in=matching_lines.values('request_id')))
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response([supplier_request_summary(row) for row in page])

    @extend_schema(request=RequestSubmission, responses=OpenApiTypes.OBJECT,
                   description='Envía una solicitud separada a cada proveedor. No reserva ni descuenta existencias.')
    def post(self, request, account_id):
        client = account_for(request.user, account_id, 'client')
        serializer = RequestSubmission(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = submit_request(client=client, actor=request.user, data=serializer.validated_data)
        except IntegrityError:
            raise RequestConflict('La solicitud cambió mientras se guardaba. Reintenta el mismo envío antes de crear otro.')
        return Response(result)


class SupplierRequestDetail(APIView):
    @extend_schema(operation_id='v1_accounts_requests_retrieve', responses=OpenApiTypes.OBJECT)
    def get(self, request, account_id, pk):
        supplier = account_for(request.user, account_id, 'supplier')
        row = get_object_or_404(request_detail_queryset(supplier), pk=pk)
        return Response(request_detail(row))


class SupplierRequestReview(APIView):
    @extend_schema(request=None, responses=OpenApiTypes.OBJECT,
                   description='Abre la orden y bloquea sus artículos mientras el proveedor prepara la cotización.')
    @transaction.atomic
    def post(self, request, account_id, pk):
        supplier = account_for(request.user, account_id, 'supplier')
        row = get_object_or_404(SupplierRequest.objects.select_for_update().filter(supplier=supplier), pk=pk)
        if row.status == 'pending':
            row.status = 'reviewed'
            row.reviewed_at = timezone.now()
            row.reviewed_by = request.user
            row.version += 1
            row.save(update_fields=['status', 'reviewed_at', 'reviewed_by', 'version', 'updated_at'])
            DealEvent.objects.create(order=row, account=supplier, actor=request.user, kind='review_started',
                                     description='EL PROVEEDOR ABRIÓ LA ORDEN. ARTÍCULOS BLOQUEADOS.')
        row = request_detail_queryset(supplier).get(pk=row.pk)
        return Response(request_detail(row))
