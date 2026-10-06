"""Resumable catalog classification with explicitly reviewed catalog writes."""
import math
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .catalog_assistant_models import CatalogAssistantJob, CatalogAssistantSuggestion, CategorySuggestionCache
from .category_suggestions import CATEGORY_BATCH_SIZE, classify_categories
from .technical_data import bind_part_types
from .catalog_classification import (AI_BATCH_SIZE, PROVIDER_TIMEOUT, ClassificationConflict,
    ClassificationUnavailable, call_provider, candidate_context, provider_configuration, validate_suggestions)
from .catalog_grouping import merge_catalog_parts
from .management import IsSuperuser, UppercaseCharField
from .models import CatalogMerge, Part


class AssistantStart(serializers.Serializer):
    id = serializers.UUIDField()
    scope = serializers.ChoiceField(choices=['unclassified', 'all'])
    search = UppercaseCharField(max_length=200, allow_blank=True, default='')
    instructions = UppercaseCharField(max_length=2000, allow_blank=True, default='')
    task = serializers.ChoiceField(choices=['categories', 'grouping'], default='grouping')


class AssistantDecision(serializers.Serializer):
    id = serializers.UUIDField()
    target_sku = UppercaseCharField(max_length=200)
    category = UppercaseCharField(max_length=120, allow_blank=True)
    subcategory = UppercaseCharField(max_length=120, allow_blank=True)


class AssistantAction(serializers.Serializer):
    mode = serializers.ChoiceField(choices=['classify', 'apply', 'dismiss', 'apply_category'])
    batch_index = serializers.IntegerField(min_value=0, required=False)
    decisions = AssistantDecision(many=True, allow_empty=False, max_length=AI_BATCH_SIZE, required=False)
    ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=AI_BATCH_SIZE, required=False)
    category = UppercaseCharField(max_length=120, required=False)
    subcategory = UppercaseCharField(max_length=120, required=False)

    def validate(self, data):
        if data['mode'] == 'apply_category':
            if not data.get('category') or not data.get('subcategory'):
                raise ValidationError('Selecciona el grupo y subgrupo que revisaste.')
            return data
        required = {'classify': 'batch_index', 'apply': 'decisions', 'dismiss': 'ids'}[data['mode']]
        if required not in data:
            raise ValidationError('Indica el lote o las propuestas revisadas para esta acción.')
        return data


def eligible_parts(scope, search):
    parts = Part.objects.filter(active=True, merged_into__isnull=True)
    if scope == 'unclassified':
        parts = parts.filter(Q(category='') | Q(subcategory=''))
    if search:
        parts = parts.filter(Q(sku__icontains=search) | Q(name__icontains=search) |
            Q(description__icontains=search) | Q(codes__code__icontains=search) |
            Q(category__icontains=search) | Q(subcategory__icontains=search)).distinct()
    return parts.order_by('sku')


def current_row(part):
    return {'id': str(part.pk), 'sku': part.sku, 'name': part.name, 'description': part.description,
            'is_OEM': part.is_OEM,
            'category': part.category, 'subcategory': part.subcategory, 'active': part.active,
            'merged_into': str(part.merged_into_id) if part.merged_into_id else None,
            'codes': [{'brand': code.brand, 'code': code.code, 'ref_type': code.ref_type, 'reference_source': code.reference_source} for code in part.codes.all()]}


def claim_active(job):
    return bool(job.claim_id and job.claimed_at and job.claimed_at > timezone.now() - timedelta(seconds=PROVIDER_TIMEOUT + 30))


def owned_job(user, pk, lock=False):
    query = CatalogAssistantJob.objects.filter(owner=user, pk=pk)
    if lock:
        query = query.select_for_update()
    job = query.first()
    if not job:
        raise NotFound('El análisis solicitado no existe.')
    return job


def summary(job):
    counts = {item['status']: item['count'] for item in job.suggestions.values('status').annotate(count=Count('id'))}
    total = math.ceil(len(job.source_ids) / job.batch_size)
    return {'id': str(job.pk), 'scope': job.scope, 'search': job.search, 'instructions': job.instructions,
            'model': job.model, 'task': job.task, 'metrics': job.metrics,
            'total_items': len(job.source_ids), 'batch_size': job.batch_size, 'page_size': AI_BATCH_SIZE,
            'completed_batches': job.completed_batches, 'total_batches': total,
            'status': 'running' if claim_active(job) else 'completed' if job.completed_batches == total else 'ready',
            'count': sum(counts.values()), 'pending_count': counts.get('pending', 0),
            'applied_count': counts.get('applied', 0), 'dismissed_count': counts.get('dismissed', 0),
            'created_at': job.created_at.isoformat()}


def job_response(job, offset=0):
    result = summary(job)
    result.update({'configured': bool(provider_configuration()[0]), 'offset': offset,
                   'next_offset': offset + AI_BATCH_SIZE if result['count'] > offset + AI_BATCH_SIZE else None,
                   'results': [{**item.proposal, 'id': str(item.pk), 'status': item.status, 'decision': item.decision,
                                'source': item.context[item.proposal['source_sku']],
                                'candidates': [value for sku, value in item.context.items() if sku != item.proposal['source_sku']]}
                               for item in job.suggestions.all()[offset:offset + AI_BATCH_SIZE]]})
    result['category_groups'] = []
    if job.task == 'categories':
        result['category_groups'] = [
            {'category': row['proposal__category'], 'subcategory': row['proposal__subcategory'], 'count': row['count']}
            for row in job.suggestions.filter(status='pending', proposal__confidence__gte=.85)
                .exclude(proposal__category='').exclude(proposal__subcategory='')
                .order_by('proposal__category', 'proposal__subcategory')
                .values('proposal__category', 'proposal__subcategory').annotate(count=Count('id'))]
    return result


def classify(user, pk, index):
    key, model = provider_configuration()
    claim = uuid.uuid4()
    with transaction.atomic():
        job = owned_job(user, pk, lock=True)
        if not key and job.task != 'categories':
            raise ClassificationUnavailable()
        if index < job.completed_batches:
            return job_response(job, index * job.batch_size)
        if index != job.completed_batches or index >= math.ceil(len(job.source_ids) / job.batch_size):
            raise ValidationError('Continúa con el siguiente lote pendiente.')
        if claim_active(job):
            raise ClassificationConflict('Un lote de este análisis está en curso. Espera a que termine para reanudar.')
        selected = job.source_ids[index * job.batch_size:(index + 1) * job.batch_size]
        job.claim_id, job.claimed_at = claim, timezone.now()
        job.save(update_fields=['claim_id', 'claimed_at'])
    try:
        if job.task == 'categories':
            return classify_category_batch(user, job, claim, index, selected)
        # Retrieve candidates from the full active catalog, across batch and filter
        # boundaries. Only the 50 sources and bounded candidates reach Gemini.
        parts = list(Part.objects.filter(active=True, merged_into__isnull=True).prefetch_related('codes').order_by('sku'))
        rows = {part.sku: current_row(part) for part in parts}
        selected_ids = set(selected)
        selected_skus = [part.sku for part in parts if str(part.pk) in selected_ids]
        if len(selected_skus) != len(selected):
            raise ClassificationConflict('Algunos SKU se desactivaron o agruparon. Crea un análisis nuevo con el inventario actual.')
        groups = {sku: {'sku': sku, 'row': position + 1, 'fields': {field: row[field] for field in ['name', 'description', 'category', 'subcategory']},
                        'codes': {(code['brand'], code['code']): position + 1 for code in row['codes']}, 'reference_details': row['codes']}
                  for position, (sku, row) in enumerate(rows.items())}
        source, candidates, allowed = candidate_context(groups, selected_skus)
        rows.update({sku: row for sku, row in candidates.items() if row.get('proposed_parent')})
        # Existing candidate_context also reads retired entries by exact code;
        # the assistant offers only active canonical targets that were captured.
        allowed = {sku: [target for target in options if target in rows] for sku, options in allowed.items()}
        candidates = {sku: row for sku, row in candidates.items() if sku in rows}
        proposals = validate_suggestions(call_provider(source, candidates, allowed, instructions=job.instructions, feature='catalog_assistant'),
                                         source, candidates, allowed, preserve_unverified_groups=True)
        positions = {part_id: index * AI_BATCH_SIZE + position for position, part_id in enumerate(selected)}
        with transaction.atomic():
            job = owned_job(user, pk, lock=True)
            if job.claim_id != claim:
                raise ClassificationConflict('Este lote se reanudó desde otra sesión. Actualiza el análisis.')
            CatalogAssistantSuggestion.objects.bulk_create([
                CatalogAssistantSuggestion(job=job, source_id=rows[item['source_sku']]['id'],
                    position=positions[rows[item['source_sku']]['id']], proposal=item,
                    context={sku: rows[sku] for sku in [item['source_sku'], *allowed[item['source_sku']]]}) for item in proposals])
            job.completed_batches += 1
            job.claim_id, job.claimed_at = None, None
            job.model = model
            job.save(update_fields=['completed_batches', 'claim_id', 'claimed_at', 'model'])
            return job_response(job, index * AI_BATCH_SIZE)
    except Exception:
        CatalogAssistantJob.objects.filter(pk=pk, claim_id=claim).update(claim_id=None, claimed_at=None)
        raise


def classify_category_batch(user, job, claim, index, selected):
    # A category needs only its source row, never a scan of all catalog candidates.
    parts = list(Part.objects.filter(pk__in=selected, active=True, merged_into__isnull=True).prefetch_related('codes'))
    if len(parts) != len(selected):
        raise ClassificationConflict('Algunos SKU se desactivaron o agruparon. Crea un análisis nuevo.')
    by_id = {str(part.pk): current_row(part) for part in parts}
    rows = [by_id[part_id] for part_id in selected]
    proposals, metrics = classify_categories(rows, job.instructions)
    with transaction.atomic():
        saved = owned_job(user, job.pk, lock=True)
        if saved.claim_id != claim:
            raise ClassificationConflict('Este lote se reanudó desde otra sesión. Actualiza el análisis.')
        CatalogAssistantSuggestion.objects.bulk_create([
            CatalogAssistantSuggestion(job=saved, source_id=row['id'], position=index*saved.batch_size+i,
                proposal=proposal, context={row['sku']: row})
            for i, (row, proposal) in enumerate(zip(rows, proposals))])
        saved.metrics = {key: saved.metrics.get(key, 0)+metrics.get(key, 0) for key in saved.metrics.keys() | metrics.keys()}
        saved.completed_batches += 1
        saved.claim_id, saved.claimed_at = None, None
        saved.model = provider_configuration()[1]
        saved.save(update_fields=['metrics', 'completed_batches', 'claim_id', 'claimed_at', 'model'])
        return job_response(saved, index*saved.batch_size)


def unchanged(part, snapshot):
    actual = current_row(part)
    if actual['is_OEM'] != snapshot.get('is_OEM', False):
        raise ClassificationConflict(f'La identificación OEM del SKU {part.sku} cambió desde el análisis. Analízalo de nuevo.')
    if any(actual[field] != snapshot[field] for field in ['id', 'sku', 'name', 'description', 'active', 'merged_into']):
        raise ClassificationConflict(f'El SKU {snapshot["sku"]} cambió desde el análisis. Crea un análisis nuevo antes de aplicar su revisión.')
    actual_refs = {(item['brand'], item['code']): item for item in actual['codes']}
    for ref in snapshot['codes']:
        current = actual_refs.get((ref['brand'], ref['code']), {})
        if any(key in ref and ref[key] != current.get(key) for key in ['ref_type', 'reference_source']):
            raise ClassificationConflict(f'El tipo o la fuente de los alternos de {part.sku} cambió desde el análisis.')
    old_codes = {(item['brand'], item['code']) for item in snapshot['codes']}
    if not old_codes.issubset({(item['brand'], item['code']) for item in actual['codes']}):
        raise ClassificationConflict(f'Los alternos de {part.sku} cambiaron desde el análisis. Revisa el inventario actual.')


@transaction.atomic
def apply(user, pk, decisions, offset):
    job = owned_job(user, pk, lock=True)
    if claim_active(job):
        raise ClassificationConflict('Pausa la clasificación antes de aplicar propuestas.')
    ids = [str(item['id']) for item in decisions]
    if len(ids) != len(set(ids)):
        raise ValidationError('No repitas propuestas en la revisión.')
    proposals = {str(item.pk): item for item in job.suggestions.filter(pk__in=ids)}
    if len(proposals) != len(ids):
        raise ValidationError('Selecciona propuestas de este análisis.')
    normalized, requested_skus = [], set()
    for item in decisions:
        value = {field: item[field] for field in ['target_sku', 'category', 'subcategory']}
        suggestion = proposals[str(item['id'])]
        if suggestion.status == 'applied' and suggestion.decision == value:
            continue  # Replaying a lost successful response performs no writes.
        if suggestion.status != 'pending':
            raise ValidationError('Esta propuesta ya fue revisada. Actualiza el análisis.')
        source, target = suggestion.proposal['source_sku'], value['target_sku']
        if target not in suggestion.context:
            raise ValidationError('Elige el SKU de origen o uno de sus candidatos mostrados.')
        if target != source and len(source) > 120:
            raise ValidationError('El SKU de origen supera el tamaño de un código alterno; conserva ese SKU separado.')
        if value['subcategory'] and not value['category']:
            raise ValidationError('Indica un grupo para la subclasificación propuesta.')
        normalized.append((suggestion, value))
        requested_skus.update([source, target])
    parts = {part.sku: part for part in Part.objects.select_for_update().filter(sku__in=requested_skus).order_by('pk').prefetch_related('codes')}
    proposed_parents = {value['target_sku']: suggestion.context[value['target_sku']] for suggestion, value in normalized
                        if suggestion.context[value['target_sku']].get('proposed_parent')}
    if requested_skus - set(parts) - set(proposed_parents) or any(not part.active or part.merged_into_id for part in parts.values()):
        raise ClassificationConflict('Uno de los SKU ya no está activo. Revisa el inventario actual.')
    moving = {item.proposal['source_sku'] for item, value in normalized if item.proposal['source_sku'] != value['target_sku']}
    if any(value['target_sku'] in moving for _, value in normalized):
        raise ValidationError('Elige un SKU final por grupo; no uses cadenas ni ciclos de agrupación.')
    targets = {}
    for suggestion, value in normalized:
        source, target = suggestion.proposal['source_sku'], value['target_sku']
        unchanged(parts[source], suggestion.context[source])
        if target in proposed_parents:
            if target in parts:
                # A parent created by an earlier reviewed batch of this same
                # analysis is reusable; an unrelated concurrent creation is not.
                if not CatalogMerge.objects.filter(target=parts[target], summary__assistant_job=str(job.pk)).exists():
                    raise ClassificationConflict(f'El SKU {target} se creó después del análisis. Revisa su agrupación antes de continuar.')
                if any(getattr(parts[target], field) != suggestion.context[target][field] for field in ['name', 'description']):
                    raise ClassificationConflict(f'El SKU {target} cambió desde el análisis.')
        else:
            unchanged(parts[target], suggestion.context[target])
        classification = (value['category'], value['subcategory'])
        if target in targets and targets[target]['classification'] != classification:
            raise ValidationError('Las propuestas del mismo SKU de destino deben compartir grupo y subgrupo.')
        for field in ['category', 'subcategory']:
            current = getattr(parts[target], field) if target in parts else ''
            if value[field] and current != suggestion.context[target][field] and current != value[field]:
                raise ClassificationConflict(f'La clasificación de {target} cambió. Analiza el SKU otra vez para conservar la edición más reciente.')
        entry = targets.setdefault(target, {'sources': [], 'classification': classification})
        if source != target:
            entry['sources'].append(source)
    changed_parts = []
    for target, data in sorted(targets.items()):
        category, subcategory = data['classification']
        if data['sources']:
            result = merge_catalog_parts(actor=user, target_sku=target, source_skus=data['sources'], category=category, subcategory=subcategory,
                                         create_target=target in proposed_parents, assistant_job=job.pk)
            if target in proposed_parents and result['target']['description'] != proposed_parents[target]['description']:
                raise ClassificationConflict(f'Las descripciones de la familia {target} cambiaron. Crea un análisis nuevo.')
        else:
            part = parts[target]
            fields = []
            for field, value in [('category', category), ('subcategory', subcategory)]:
                if value and getattr(part, field) != value:
                    setattr(part, field, value)
                    fields.append(field)
            if fields:
                changed_parts.append(part)
    if changed_parts:
        bind_part_types(changed_parts)
        Part.objects.bulk_update(changed_parts, ['category', 'subcategory', 'part_type'])
    invalidate_keys = []
    for suggestion, value in normalized:
        if any(value[field] != suggestion.proposal[field] for field in ['category', 'subcategory']):
            invalidate_keys.append(suggestion.proposal.get('cache_key'))
        suggestion.status, suggestion.decision, suggestion.reviewed_at = 'applied', value, timezone.now()
    if normalized:
        CatalogAssistantSuggestion.objects.bulk_update([s for s, _ in normalized], ['status', 'decision', 'reviewed_at'])
    CategorySuggestionCache.objects.filter(pk__in=[k for k in invalidate_keys if k]).delete()
    return job_response(job, offset)


@transaction.atomic
def apply_category(user, pk, category, subcategory, offset):
    job = owned_job(user, pk, lock=True)
    if job.task != 'categories':
        raise ValidationError('La revisión por categoría solo está disponible para análisis de categorías.')
    # The reviewer explicitly chooses one category. Low-confidence results still
    # require individual review. Each retry consumes only pending proposals.
    suggestions = list(job.suggestions.filter(status='pending', proposal__category=category,
        proposal__subcategory=subcategory, proposal__confidence__gte=.85)[:CATEGORY_BATCH_SIZE])
    result = apply(user, pk, [{'id': s.pk, 'target_sku': s.proposal['source_sku'],
                             'category': category, 'subcategory': subcategory} for s in suggestions], offset)
    result['applied_batch'] = len(suggestions)
    return result


class CatalogAssistantList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_assistant_list', responses=OpenApiTypes.OBJECT)
    def get(self, request):
        scope, search = request.query_params.get('scope', 'unclassified'), request.query_params.get('search', '').strip()
        if scope not in ['unclassified', 'all'] or len(search) > 200:
            raise ValidationError('El filtro de inventario no es válido.')
        return Response({'configured': bool(provider_configuration()[0]), 'eligible_count': eligible_parts(scope, search).count(),
                         'jobs': [summary(job) for job in CatalogAssistantJob.objects.filter(owner=request.user)[:20]]})

    @extend_schema(operation_id='v1_management_catalog_assistant_create', request=AssistantStart, responses=OpenApiTypes.OBJECT)
    def post(self, request):
        serializer = AssistantStart(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        key, model = provider_configuration()
        if not key and data['task'] != 'categories':
            raise ClassificationUnavailable()
        existing = CatalogAssistantJob.objects.filter(pk=data['id']).first()
        if existing:
            if existing.owner_id != request.user.pk:
                raise NotFound('El análisis solicitado no existe.')
            if any(getattr(existing, field) != data[field] for field in ['scope', 'search', 'instructions', 'task']):
                raise ClassificationConflict('El identificador ya pertenece a otro análisis.')
            return Response(job_response(existing))
        sources = eligible_parts(data['scope'], data['search'])
        if data['task'] == 'categories':
            sources = sources.order_by('description', 'sku')
        source_ids = [str(value) for value in sources.values_list('pk', flat=True)]
        if not source_ids:
            raise ValidationError('No hay SKU activos con esos filtros para analizar.')
        try:
            with transaction.atomic():
                job = CatalogAssistantJob.objects.create(**data, owner=request.user, source_ids=source_ids, model=model,
                    batch_size=CATEGORY_BATCH_SIZE if data['task'] == 'categories' else AI_BATCH_SIZE)
        except IntegrityError:
            job = owned_job(request.user, data['id'])
            if any(getattr(job, field) != data[field] for field in ['scope', 'search', 'instructions', 'task']):
                raise ClassificationConflict('El identificador ya pertenece a otro análisis.')
        return Response(job_response(job), status=201)


class CatalogAssistantDetail(APIView):
    permission_classes = [IsSuperuser]

    def offset(self, request):
        try:
            value = int(request.query_params.get('offset', '0'))
        except (TypeError, ValueError):
            raise ValidationError('La página solicitada no es válida.')
        if value < 0 or value % AI_BATCH_SIZE:
            raise ValidationError('La página solicitada no es válida.')
        return value

    @extend_schema(operation_id='v1_management_catalog_assistant_retrieve', responses=OpenApiTypes.OBJECT)
    def get(self, request, pk):
        return Response(job_response(owned_job(request.user, pk), self.offset(request)))

    @extend_schema(operation_id='v1_management_catalog_assistant_action', request=AssistantAction, responses=OpenApiTypes.OBJECT)
    def post(self, request, pk):
        serializer = AssistantAction(data=request.data)
        serializer.is_valid(raise_exception=True)
        data, offset = serializer.validated_data, self.offset(request)
        if data['mode'] == 'classify':
            return Response(classify(request.user, pk, data['batch_index']))
        if data['mode'] == 'apply_category':
            try:
                return Response(apply_category(request.user, pk, data['category'], data['subcategory'], offset))
            except IntegrityError:
                raise ClassificationConflict('El catálogo cambió durante la revisión. Actualiza el análisis.')
        if data['mode'] == 'apply':
            try:
                return Response(apply(request.user, pk, data['decisions'], offset))
            except IntegrityError:
                raise ClassificationConflict('El catálogo cambió durante la revisión. Actualiza el análisis antes de reintentar.')
        with transaction.atomic():
            job = owned_job(request.user, pk, lock=True)
            if claim_active(job):
                raise ClassificationConflict('Pausa la clasificación antes de descartar propuestas.')
            selected = job.suggestions.filter(pk__in=data['ids'])
            if selected.count() != len(set(data['ids'])) or selected.filter(status='applied').exists():
                raise ValidationError('Selecciona propuestas pendientes de este análisis.')
            CategorySuggestionCache.objects.filter(pk__in=[s.proposal['cache_key'] for s in selected if s.proposal.get('cache_key')]).delete()
            selected.update(status='dismissed', reviewed_at=timezone.now())
            return Response(job_response(job, offset))
