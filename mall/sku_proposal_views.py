"""Productos nuevos (superusers): the lines in preparación of mall.new_products, with their supplier items. Same routes and JSON as the
proposals had before inventory refactor S6 (mall.sku_proposals), read from the line (plan §3 S6.2):

- id is the line's id (a pending proposal's id was kept as its line's); version is the line's, the CAS every decision checks;
- status maps from estado: preparación with items is pending (without items, resolved), publicada approved, unificada linked,
  rechazada rejected; which lines count is productos_q();
- sku is the codigo; brand the main's; oem the OEM number (a non-main oem Reference, else the main when it is oem); references the
  other public References (the alternos); internal_code whether the main is the supplier's own code; items the variants;
- earlier is always null (a product approved before is a line its items attach to); catalog_match lists the catalog lines that
  already hold its codes; part is the line itself once approved, the line it went into once linked.

Additive keys: estado, origen, declared_references (codes its items declare that wait in a reference case: rule 5b)."""
import uuid

from django.db.models import (Case, CharField, Count, Exists, F, IntegerField, Max, OuterRef, Prefetch, Q, Subquery, Sum, Value,
                              When)
from django.db.models.functions import Coalesce, Greatest
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, serializers
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import new_products, references, sku_proposals
from .lines import all_lines, prepared_by_rule_7, prepared_q
from .list_ordering import StableOrdering
from .management import IsSuperuser
from .models import Part, Reference, SupplierItem
from .technical_data import grupo_nombre

PREVIEW_ITEMS = 5
MAX_BULK = 100
STATUS_LABELS = dict(sku_proposals.STATUSES)
REASON_LABELS = dict(Part.REASONS)
STATUSES = tuple(STATUS_LABELS)


def has_items():
    return Exists(SupplierItem.objects.filter(part_id=OuterRef('pk')))


def status_q(state):
    """The lines one status of Productos nuevos lists (the module's docstring), on a queryset annotated with has_items."""
    # A line rule 7 brought here from referencia keeps origen biblioteca: its mark (mall.lines.prepared_q) keeps it listed once decided.
    ours = Q(origen='proveedor') | prepared_q()
    return {
        'pending': Q(estado='preparacion', has_items=True),
        'resolved': Q(estado='preparacion', has_items=False),
        'approved': Q(estado='publicada') & ours,
        'linked': Q(estado='unificada') & ours,
        'rejected': Q(estado='rechazada') & (ours | Q(has_items=True)),
    }[state]


def productos_q():
    """Every line Productos nuevos shows (status all)."""
    found = Q()
    for state in STATUSES:
        found |= status_q(state)
    return found


def status_case():
    return Case(*[When(status_q(state), then=Value(state)) for state in STATUSES], default=Value(''), output_field=CharField())


def item_json(item):
    return {'id': str(item.pk), 'supplier': str(item.supplier_id), 'supplier_name': item.supplier.name, 'supplier_invent_id': item.supplier_invent_id,
            'codigo': item.codigo, 'brand': item.brand, 'description': item.description, 'references': item.references,
            'source': item.source, 'available': item.available_quantity, 'matching_status': item.matching_status}


def ai_review(line):
    """The line's latest Revisión IA verdict: whether it still holds (stale once the line changed), why, and the run."""
    latest = getattr(line, 'latest_verdicts', None)
    if latest is None:
        from .sku_review import given
        latest = list(given().filter(line=line).select_related('link_part').order_by('-created_at', '-pk')[:1])
    if not latest:
        return None
    verdict = latest[0]
    return {'verdict': verdict.verdict, 'confidence': verdict.confidence, 'reasons': verdict.reasons, 'reason': verdict.reason,
            'suggestions': verdict.suggestions, 'stale': verdict.proposal_version != line.version, 'run': str(verdict.run_id),
            'link_part': {'id': str(verdict.link_part_id), 'sku': verdict.link_part.sku} if verdict.link_part_id else None,
            'created_at': verdict.created_at}


def codes_of(line):
    """(main, oem, alternos) of a line from its live public References (prefetched as live_codes when the queryset did). The alternos
    leave out the OEM number and the codes its items hang from (the suppliers' own printed codes: the items show them)."""
    codes = getattr(line, 'live_codes', None)
    if codes is None:
        codes = list(Reference.objects.filter(part_id=line.pk).order_by('pk'))
    main = references.main_in(codes)
    oem = new_products.oem_of(line, codes)
    items = getattr(line, 'variants_list', None)
    hung = ({item.reference_id for item in items} if items is not None else
            set(SupplierItem.objects.filter(part_id=line.pk).values_list('reference_id', flat=True)))
    alternos = [ref for ref in codes if not ref.is_main and ref.tipo != 'interno' and (oem is None or ref.pk != oem.pk) and ref.pk not in hung]
    return main, oem, alternos


def status_for(estado, origen, with_items, prepared=False):
    """status_q in Python, for a loaded line: the status Productos nuevos shows it under, or '' when it does not. prepared: rule 7
    brought it from referencia (mall.lines.prepared_by_rule_7)."""
    ours = origen == 'proveedor' or prepared
    if estado == 'preparacion':
        return 'pending' if with_items else 'resolved'
    if estado == 'publicada' and ours:
        return 'approved'
    if estado == 'unificada' and ours:
        return 'linked'
    if estado == 'rechazada' and (ours or with_items):
        return 'rejected'
    return ''


def status_of(line):
    state = getattr(line, 'status', None)
    if state is None:
        items = getattr(line, 'variants_list', None)
        with_items = bool(items) if items is not None else SupplierItem.objects.filter(part_id=line.pk).exists()
        state = status_for(line.estado, line.origen, with_items, prepared_by_rule_7(line))
    return state


def declared(line, items):
    """The codes the line's items declare that wait in an open reference case (rule 5b), one per code."""
    from .matching_models import MatchingCase
    found, rows = set(), []
    for case in MatchingCase.objects.filter(item_id__in=[item.pk for item in items], kind=new_products.CASE_KIND, status='review').order_by('key'):
        code, brand = case.new_code, case.new_brand
        if (code, brand) not in found:
            found.add((code, brand))
            rows.append({'code': code, 'brand': brand, 'case': str(case.pk)})
    return rows


def proposal_json(line, *, all_items=False):
    items = getattr(line, 'variants_list', None)
    if items is None:
        items = list(SupplierItem.objects.filter(part_id=line.pk).select_related('supplier').order_by('pk'))
    main, oem, alternos = codes_of(line)
    part_type, state = line.part_type, status_of(line)
    part = {'id': str(line.pk), 'sku': line.sku} if state == 'approved' else (
        {'id': str(line.merged_into_id), 'sku': line.merged_into.sku} if state == 'linked' and line.merged_into_id else None)
    return {
        'id': str(line.pk), 'status': state, 'status_label': STATUS_LABELS.get(state, ''), 'version': line.version,
        'estado': line.estado, 'origen': line.origen,
        'sku': line.sku, 'description': line.description, 'brand': main.brand if main is not None else '',
        'oem': {'code': oem.code, 'brand': oem.brand} if oem is not None else None,
        'part_type': {'id': str(part_type.pk), 'category': grupo_nombre(part_type), 'name': part_type.name} if part_type else None,
        'references': [{'brand': ref.brand, 'code': ref.code, 'ref_type': ref.ref_type} for ref in alternos],
        'field_sources': line.field_sources or {}, 'internal_code': main is not None and main.tipo == 'interno',
        'ai_review': ai_review(line) if state == 'pending' else None,
        'item_count': len(items), 'supplier_count': len({item.supplier_id for item in items}),
        'stock': sum(item.available_quantity for item in items),
        'items': [item_json(item) for item in (items if all_items else items[:PREVIEW_ITEMS])],
        # The detail only: the catalog lines its codigo or OEM number already belongs to (Vincular), and its declared codes.
        **({'catalog_match': new_products.catalog_match(line, main, oem) if state == 'pending' else [],
            'declared_references': declared(line, items)} if all_items else {}),
        'part': part,
        'decided_by': line.decided_by.username if line.decided_by_id else None, 'decided_at': line.decided_at,
        'reject_reason': line.decision_reason if line.estado == 'rechazada' else '',
        'reject_reason_label': REASON_LABELS.get(line.decision_reason, '') if line.estado == 'rechazada' else '',
        'reject_note': line.decision_note if line.estado == 'rechazada' else '',
        'created_at': line.created_at, 'updated_at': line.changed_at or line.created_at,
        'earlier': None,
    }


def proposals():
    """The lines of Productos nuevos with what the grid shows and sorts by: status, the main's brand, items, stock, the latest verdict."""
    from .sku_review import given
    from .sku_review_models import SkuReviewVerdict
    latest = given().filter(line=OuterRef('pk')).order_by('-created_at', '-pk')
    own_latest = given().filter(line=OuterRef('line_id')).order_by('-created_at', '-pk').values('pk')[:1]
    newest = Prefetch('review_verdicts', to_attr='latest_verdicts',
                      queryset=SkuReviewVerdict.objects.filter(pk=Subquery(own_latest)).select_related('link_part'))
    items = Prefetch('supplier_items', to_attr='variants_list', queryset=SupplierItem.objects.select_related('supplier').order_by('pk'))
    codes = Prefetch('codes', to_attr='live_codes', queryset=Reference.objects.order_by('pk'))
    main_brand = Reference.objects.filter(part_id=OuterRef('pk'), is_main=True).values('brand')[:1]
    stock = (SupplierItem.objects.filter(part_id=OuterRef('pk')).values('part_id')
             .annotate(total=Sum(Greatest(F('reported_quantity') - F('reserved_quantity'), Value(0)))).values('total'))
    count = SupplierItem.objects.filter(part_id=OuterRef('pk')).values('part_id').annotate(total=Count('pk')).values('total')
    return (all_lines().annotate(has_items=has_items()).filter(productos_q())
            .select_related('part_type__grupo', 'decided_by', 'merged_into')
            .prefetch_related(items, codes, newest)
            .annotate(status=status_case(), brand=Coalesce(Subquery(main_brand), Value(''), output_field=CharField()),
                      item_total=Coalesce(Subquery(count, output_field=IntegerField()), Value(0)),
                      stock_total=Coalesce(Subquery(stock, output_field=IntegerField()), Value(0)),
                      ai_verdict=Subquery(latest.values('verdict')[:1]), ai_version=Subquery(latest.values('proposal_version')[:1])))


class ProposalSerializer(serializers.BaseSerializer):
    def to_representation(self, instance):
        return proposal_json(instance)


def search_q(search):
    """A grid search: the codigo, the description, any live code of the line, an item's code or its supplier's name."""
    carried = SupplierItem.objects.filter(Q(codigo__icontains=search) | Q(supplier__name__icontains=search)).values('part_id')
    coded = Reference.objects.filter(code__icontains=search).values('part_id')
    return Q(sku__icontains=search) | Q(description__icontains=search) | Q(pk__in=coded) | Q(pk__in=carried)


class ProposalList(generics.ListAPIView):
    permission_classes = [IsSuperuser]
    serializer_class = ProposalSerializer
    filter_backends = [StableOrdering]
    ordering_fields = ['sku', 'description', 'brand', 'status', 'created_at', 'updated_at', 'item_total', 'stock_total']
    ordering = ['-stock_total', 'sku']

    def get_queryset(self):
        params = self.request.query_params
        rows = proposals()
        state = params.get('status') or 'pending'
        if state != 'all':
            if state not in STATUS_LABELS:
                raise ValidationError({'status': 'Usa pending, approved, linked, rejected, resolved o all.'})
            rows = rows.filter(status_q(state))
        search = (params.get('search') or '').strip()
        if search:
            rows = rows.filter(search_q(search))
        if params.get('brand'):
            rows = rows.filter(brand__iexact=params['brand'].strip())
        if params.get('has_oem') in ('true', 'false'):
            oem = Exists(Reference.objects.filter(part_id=OuterRef('pk'), tipo='oem'))
            rows = rows.annotate(has_oem=oem).filter(has_oem=params['has_oem'] == 'true')
        ai = params.get('ai')
        if ai:
            if ai not in ('good', 'bad', 'unsure', 'stale', 'none'):
                raise ValidationError({'ai': 'Usa good, bad, unsure, stale o none.'})
            rows = (rows.filter(ai_verdict__isnull=True) if ai == 'none' else rows.filter(ai_verdict__isnull=False).exclude(ai_version=F('version'))
                    if ai == 'stale' else rows.filter(ai_verdict=ai, ai_version=F('version')))
        if params.get('supplier'):
            try:
                uuid.UUID(params['supplier'])
            except ValueError:
                raise ValidationError({'supplier': 'Elige un proveedor.'})
            rows = rows.filter(pk__in=SupplierItem.objects.filter(supplier_id=params['supplier']).values('part_id'))
        return rows

    @extend_schema(operation_id='v1_management_catalog_proposals_list', responses=OpenApiTypes.OBJECT, parameters=[
        OpenApiParameter('status', OpenApiTypes.STR, description='pending (por omisión), approved, linked, rejected, resolved o all.'),
        OpenApiParameter('search', OpenApiTypes.STR), OpenApiParameter('brand', OpenApiTypes.STR), OpenApiParameter('has_oem', OpenApiTypes.STR),
        OpenApiParameter('supplier', OpenApiTypes.UUID), OpenApiParameter('ordering', OpenApiTypes.STR), OpenApiParameter('page', OpenApiTypes.INT),
        OpenApiParameter('ai', OpenApiTypes.STR, description='Revisión IA vigente: good, bad, unsure; stale (cambió desde la revisión) o none (sin revisar).')])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class ProposalSummary(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_proposals_summary', responses=OpenApiTypes.OBJECT)
    def get(self, request):
        rows = all_lines().annotate(has_items=has_items())
        summary = {state: rows.filter(status_q(state)).count() for state in STATUSES}
        # When a product waiting for a decision last appeared or changed in a way a person sees (changed_at, never an AI fill): the
        # storefront reminder, once closed, shows again only after this moves (frontend/components/new-products-snackbar.tsx).
        summary['pending_changed_at'] = rows.filter(status_q('pending')).aggregate(at=Max('changed_at'))['at']
        return Response(summary)


class ActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=['approve', 'reject', 'link'])
    expected_version = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    part = serializers.UUIDField(required=False)


class ProposalDetail(APIView):
    permission_classes = [IsSuperuser]

    def current(self, pk):
        line = proposals().filter(pk=pk).first()
        if line is None:
            raise NotFound('No existe ese producto.')
        return proposal_json(line, all_items=True)

    @extend_schema(operation_id='v1_management_catalog_proposals_retrieve', responses=OpenApiTypes.OBJECT)
    def get(self, request, pk):
        return Response(self.current(pk))

    @extend_schema(operation_id='v1_management_catalog_proposals_edit', request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def patch(self, request, pk):
        data = dict(request.data)
        version = data.pop('expected_version', None)
        if not isinstance(version, int) or isinstance(version, bool):
            raise ValidationError({'expected_version': 'Envía la versión que estás editando.'})
        return self.answer(pk, lambda: new_products.edit(pk, request.user, version, data))

    @extend_schema(operation_id='v1_management_catalog_proposals_decide', request=ActionSerializer, responses=OpenApiTypes.OBJECT)
    def post(self, request, pk):
        serializer = ActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data['action'] == 'approve':
            run = lambda: new_products.publish(pk, request.user, data['expected_version'])
        elif data['action'] == 'reject':
            run = lambda: new_products.reject(pk, request.user, data['expected_version'], data.get('reason', ''), data.get('note', ''))
        else:
            if not data.get('part'):
                raise ValidationError({'part': 'Elige el SKU del catálogo.'})
            run = lambda: new_products.link(pk, request.user, data['expected_version'], data['part'])
        return self.answer(pk, run)

    def answer(self, pk, run):
        try:
            run()
        except new_products.ProposalConflict as conflict:
            return Response({'detail': conflict.detail, 'proposal': self.current(pk)}, status=conflict.status_code)
        except new_products.DuplicateProposal as refused:
            return Response({'detail': refused.detail, 'owners': refused.owners}, status=400)
        return Response(self.current(pk))


def message_of(error):
    detail = error.detail
    if isinstance(detail, str):
        return detail
    values = detail.values() if isinstance(detail, dict) else [detail]
    return ' '.join(str(value) for values in values for value in (values if isinstance(values, list) else [values]))


class ProposalBulk(APIView):
    """Aprobar, rechazar o vincular en lote: each product on its own (one refused does not stop the others), at the version the grid or
    the Revisión IA report showed. reject rows carry reason (and note); link rows carry the catalog SKU (part)."""
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_proposals_bulk', request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        action = request.data.get('action') if isinstance(request.data, dict) else None
        rows = request.data.get('proposals') if isinstance(request.data, dict) else None
        if action not in ('approve', 'reject', 'link') or not isinstance(rows, list) or not rows or len(rows) > MAX_BULK:
            raise ValidationError({'detail': f'Envía action approve, reject o link y de 1 a {MAX_BULK} propuestas con su versión.'})
        results = []
        for row in rows:
            if not isinstance(row, dict):
                results.append({'id': '', 'ok': False, 'message': 'Falta el id o la versión.'})
                continue
            ident = str(row.get('id') or '')
            try:
                uuid.UUID(ident)
            except ValueError:
                ident = ''
            if not ident or not isinstance(row.get('version'), int) or isinstance(row.get('version'), bool):
                results.append({'id': ident, 'ok': False, 'message': 'Falta el id o la versión.'})
                continue
            try:
                if action == 'approve':
                    line = new_products.publish(ident, request.user, row['version'])
                elif action == 'reject':
                    line = new_products.reject(ident, request.user, row['version'], str(row.get('reason') or ''), str(row.get('note') or ''))
                else:
                    try:
                        part = str(uuid.UUID(str(row.get('part') or '')))
                    except ValueError:
                        raise ValidationError({'part': 'Elige el SKU del catálogo.'})
                    line = new_products.link(ident, request.user, row['version'], part)
            except new_products.DuplicateProposal as refused:
                results.append({'id': ident, 'ok': False, 'message': refused.detail})
            except (APIException, ValidationError) as error:
                results.append({'id': ident, 'ok': False, 'message': str(message_of(error))})
            else:
                sku = line.sku if line.estado == 'publicada' else line.merged_into.sku if line.merged_into_id else None
                results.append({'id': str(line.pk), 'ok': True, **({'sku': sku} if sku else {})})
        done = sum(result['ok'] for result in results)
        return Response({'action': action, 'done': done, 'approved': done if action == 'approve' else 0, 'results': results})
