"""Superuser API for the code suffix table: list with filters, one-click owner labels, aliases and scope overrides.

Every edit locks the row, checks the version the reviewer saw, bumps the table version and writes a CatalogCodeSuffixChange.
Repeating an edit that is already in place returns the row unchanged.
"""
import re

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, F, IntegerField, Max, Q
from django.db.models.fields.json import KT
from django.db.models.functions import Cast
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .catalog_identity import company_registry
from .catalog_suffix_models import (SUFFIX_CLASSES, SUFFIX_STATUSES, TAG_KINDS, VARIANT_KINDS, CatalogCodeSuffix, CatalogCodeSuffixChange,
                                    normalized_token, values)
from .catalog_suffixes import COMPANY_KINDS, FILLER, SIZE_RE, _guarded, description_head, suffix_table
from .management import IsSuperuser, UppercaseCharField

SUFFIX_LOCK = 724631111  # pg advisory lock key (matching_worker 724631109, quote assistant 724631110)
ROW_ACTIONS = ['confirm_tag', 'set_variant', 'set_unknown', 'note', 'add_alias', 'add_scope_override', 'remove_scope_override']
BULK_ACTIONS = ['confirm_tag', 'set_variant', 'set_unknown']
LABELS = {'confirm_tag': 'TAG', 'set_variant': 'VARIANT', 'set_unknown': 'UNKNOWN'}
SNAPSHOT = ('cls', 'tag_kind', 'variant_kind', 'attribution', 'creates_company_reference', 'confidence', 'status', 'owner_confirmed',
            'scope_overrides', 'notes')
# Only spellings the chain tokenizer can produce: a word, A/B compounds, two-word tokens (NEW ERA) or a decimal size.
ALIAS_RE = re.compile(r'^(?:[A-Z0-9]{1,30}|[A-Z]{1,16}(?:/[A-Z]{1,16}){1,3}|[A-Z]{1,16} [A-Z]{1,16}|\d\.\d{2})$')
LABEL_FIELDS = ('cls', 'tag_kind', 'variant_kind', 'attribution', 'creates_company_reference')
UNLOCK = KT('stats__run__unknown_unlock_if_labeled_TAG__unlock_alone_all')
MAX_SCOPES, MAX_BULK = 10, 100


class SuffixConflict(APIException):
    status_code = 409
    default_detail = 'El sufijo cambió. Actualiza la tabla antes de continuar.'


class SuffixTableUnavailable(APIException):
    status_code = 503
    default_detail = 'La tabla de sufijos aún no está disponible: faltan migraciones por aplicar.'


def require_table():
    if _guarded(lambda: CatalogCodeSuffix.objects.exists()) is None:
        raise SuffixTableUnavailable()


class ScopeOverride(serializers.Serializer):
    heads = serializers.ListField(child=serializers.CharField())
    cls = serializers.CharField()
    kind = serializers.CharField(allow_blank=True)
    attribution = serializers.CharField(allow_blank=True)
    confidence = serializers.CharField()


class SuffixRow(serializers.ModelSerializer):
    alias_of = serializers.SlugRelatedField(slug_field='token', read_only=True, allow_null=True)
    aliases = serializers.SlugRelatedField(slug_field='token', many=True, read_only=True)
    confirmed_by = serializers.SlugRelatedField(slug_field='username', read_only=True, allow_null=True)
    auto_eligible = serializers.BooleanField(read_only=True, help_text='TAG que un renombrado automático puede quitar.')
    scope_overrides = ScopeOverride(many=True, read_only=True)
    stats = serializers.DictField(read_only=True, help_text='Conteos, ejemplos y evidencia; run trae bloqueos y desbloqueos del último análisis OEM.')

    class Meta:
        model = CatalogCodeSuffix
        fields = ['token', 'match', 'alias_of', 'aliases', 'cls', 'tag_kind', 'variant_kind', 'attribution', 'creates_company_reference',
                  'scope_overrides', 'confidence', 'status', 'owner_confirmed', 'confirmed_by', 'confirmed_at', 'auto_eligible',
                  'proposed_label', 'legacy_company_registry', 'notes', 'stats', 'updated_at', 'version']
        read_only_fields = fields


class SuffixChangeRow(serializers.ModelSerializer):
    actor = serializers.SlugRelatedField(slug_field='username', read_only=True, allow_null=True)

    class Meta:
        model = CatalogCodeSuffixChange
        fields = ['action', 'actor', 'before', 'after', 'note', 'version', 'created_at']
        read_only_fields = fields


class SuffixDetailResponse(SuffixRow):
    changes = SuffixChangeRow(many=True, read_only=True, help_text='Últimos 20 cambios auditados.')

    class Meta(SuffixRow.Meta):
        fields = SuffixRow.Meta.fields + ['changes']
        read_only_fields = fields


class SuffixCounts(serializers.Serializer):
    cls = serializers.DictField(child=serializers.IntegerField())
    status = serializers.DictField(child=serializers.IntegerField())
    has_unlock = serializers.IntegerField(help_text='Sufijos cuyo etiquetado desbloquearía SKU según el último análisis OEM.')


class SuffixListResponse(serializers.Serializer):
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = SuffixRow(many=True)
    version = serializers.IntegerField(help_text='Versión de la tabla; sube con cada cambio.')
    counts = SuffixCounts()
    company_suffixes = serializers.DictField(child=serializers.CharField(), help_text='Sufijos que crean referencias de empresa.')


class SuffixEdit(serializers.Serializer):
    action = serializers.ChoiceField(choices=ROW_ACTIONS)
    version = serializers.IntegerField(min_value=1, help_text='Versión del sufijo que revisaste; si cambió, responde 409.')
    tag_kind = serializers.ChoiceField(choices=TAG_KINDS, required=False)
    variant_kind = serializers.ChoiceField(choices=VARIANT_KINDS, required=False)
    attribution = serializers.CharField(max_length=200, required=False, allow_blank=True)
    creates_company_reference = serializers.BooleanField(required=False)
    notes = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    alias = UppercaseCharField(max_length=60, required=False, help_text='add_alias: otra escritura del mismo sufijo.')
    heads = serializers.ListField(child=UppercaseCharField(max_length=60), required=False, allow_empty=False, max_length=20,
                                  help_text='add_scope_override: sustantivos iniciales de la descripción (BATERIA, BUJE BIELA).')
    cls = serializers.ChoiceField(choices=SUFFIX_CLASSES, required=False, help_text='add_scope_override: clase dentro del alcance.')
    kind = serializers.CharField(max_length=20, required=False, allow_blank=True, help_text='add_scope_override: tipo de etiqueta o de variante.')
    index = serializers.IntegerField(min_value=0, required=False, help_text='remove_scope_override: posición del alcance.')
    note = serializers.CharField(max_length=300, required=False, allow_blank=True, help_text='Comentario para la auditoría.')


class SuffixBulkRow(serializers.Serializer):
    token = serializers.CharField(max_length=60)
    version = serializers.IntegerField(min_value=1)


class SuffixBulkEdit(serializers.Serializer):
    action = serializers.ChoiceField(choices=BULK_ACTIONS)
    rows = SuffixBulkRow(many=True, allow_empty=False, max_length=MAX_BULK)
    tag_kind = serializers.ChoiceField(choices=TAG_KINDS, required=False)
    variant_kind = serializers.ChoiceField(choices=VARIANT_KINDS, required=False)
    note = serializers.CharField(max_length=300, required=False, allow_blank=True)


class SuffixBulkResponse(serializers.Serializer):
    results = SuffixRow(many=True)
    version = serializers.IntegerField()


class CompanySuffixesResponse(serializers.Serializer):
    suffixes = serializers.DictField(child=serializers.CharField(), help_text='{sufijo: empresa}: SKU-SUFIJO es un código de esa empresa.')
    registry = serializers.ChoiceField(choices=['settings', 'table', 'fallback'],
                                       help_text='settings (CATALOG_COMPANY_SUFFIXES), table (tabla de sufijos) o fallback (FEB/FEBEST mientras falta la migración).')


def snapshot(row):
    return {**{field: getattr(row, field) for field in SNAPSHOT}, 'alias_of': row.alias_of.token if row.alias_of_id else None}


def detail(row):
    return {**SuffixRow(row).data, 'changes': SuffixChangeRow(row.changes.select_related('actor')[:20], many=True).data}


def rows_query():
    return CatalogCodeSuffix.objects.select_related('alias_of', 'confirmed_by').prefetch_related('aliases')


def table_version():
    return CatalogCodeSuffix.objects.aggregate(top=Max('version'))['top'] or 0


def lock_table():
    if connection.vendor == 'postgresql':
        # Serializes suffix edits so each save reads the committed max version (the readers' cache key).
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(%s)', [SUFFIX_LOCK])


def previous(row, cls):
    """The row's last audited label of that class, so a one-click relabel undoes a mistaken one (FEB back to the FEBEST company code)."""
    return next((c.before for c in row.changes.order_by('-created_at', '-id')[:50] if c.before.get('cls') == cls), {})


def label(row, action, data):
    """Field values of the owner's label. Kind and attribution default to what the row says, or said the last time it had that class."""
    cls = LABELS[action]
    keep = row.cls == cls
    prior = {} if keep or cls == 'UNKNOWN' else previous(row, cls)
    if row.token.startswith('ALT_TAIL:') and (cls != 'VARIANT' or data.get('variant_kind', 'alt_oem_tail') != 'alt_oem_tail'):
        raise ValidationError('Las colas de OEM alterno (ALT_TAIL) son siempre variantes de tipo OEM alterno.')
    if cls == 'TAG':
        kind = data.get('tag_kind') or (row.tag_kind if keep else prior.get('tag_kind') or 'brand')
        same = keep and row.tag_kind == kind or prior.get('tag_kind') == kind
        attribution = row.attribution if keep else prior.get('attribution', '') if same else ''
        default = attribution or (f'brand: {row.token}' if kind in COMPANY_KINDS else '')
        company = data.get('creates_company_reference', (row.creates_company_reference if keep else prior.get('creates_company_reference', kind in COMPANY_KINDS))
                           if same else kind in COMPANY_KINDS)
        if company and kind not in COMPANY_KINDS:
            raise ValidationError({'creates_company_reference': 'Solo una marca, marca propia o código de empresa crea referencias de empresa.'})
        if company and kind == 'company_code' and not data.get('attribution', default).strip():
            # The attribution names the brand of every company reference catalog_identity writes for SKU-<token>.
            raise ValidationError({'attribution': 'Indica la empresa del código (por ejemplo, brand: FEBEST).'})
        return {'cls': 'TAG', 'tag_kind': kind, 'variant_kind': '', 'attribution': data.get('attribution', default),
                'creates_company_reference': company, 'status': 'owner_confirmed', 'owner_confirmed': True}
    if data.get('creates_company_reference'):
        raise ValidationError({'creates_company_reference': 'Solo una etiqueta crea referencias de empresa.'})
    if cls == 'VARIANT':
        kind = data.get('variant_kind') or (row.variant_kind if keep else prior.get('variant_kind') or 'spec')
        attribution = row.attribution if keep else prior.get('attribution', '') if prior.get('variant_kind') == kind else ''
        return {'cls': 'VARIANT', 'tag_kind': '', 'variant_kind': kind, 'attribution': data.get('attribution', attribution),
                'creates_company_reference': False, 'status': 'owner_confirmed', 'owner_confirmed': True}
    return {'cls': 'UNKNOWN', 'tag_kind': '', 'variant_kind': '', 'attribution': data.get('attribution', row.attribution if keep else ''),
            'creates_company_reference': False, 'status': 'needs_owner_label', 'owner_confirmed': False}


def scope_head(text):
    """The head noun as classify() compares it: BOBINA -> COIL, FILTRO GASOLINA -> FILTRO GAS, BUJE BIELA stays."""
    head, qualifier = description_head(text)
    extra = [w for w in text.split()[1:] if w not in FILLER]
    if not head or len(extra) > 1 or (extra and not qualifier):
        raise ValidationError({'heads': f'{" ".join(text.split()) or "Un sustantivo"} no es un sustantivo inicial reconocido; usa el sustantivo '
                                        'y, si aplica, un calificador del tipo de pieza (BUJE BIELA, FILTRO ACEITE).'})
    return f'{head} {qualifier}'.strip()


def scope_override(row, data):
    if row.match != 'exact':
        raise ValidationError('Los alcances por tipo de pieza solo aplican a sufijos exactos.')
    heads = [scope_head(h) for h in data.get('heads') or []]
    if not heads or len(set(heads)) != len(heads):
        raise ValidationError({'heads': 'Indica sustantivos iniciales distintos, por ejemplo BATERIA o BUJE BIELA.'})
    taken = {h for o in row.scope_overrides for h in o['heads']} & set(heads)
    if taken:
        raise ValidationError({'heads': f'Ya existe un alcance para {", ".join(sorted(taken))}.'})
    if len(row.scope_overrides) >= MAX_SCOPES:
        raise ValidationError(f'Usa hasta {MAX_SCOPES} alcances por sufijo.')
    cls, kind = data.get('cls'), data.get('kind') or ''
    allowed = {'TAG': values(TAG_KINDS), 'VARIANT': values(VARIANT_KINDS), 'UNKNOWN': ['']}
    if cls not in allowed or kind not in allowed[cls]:
        raise ValidationError({'kind': 'Elige la clase y un tipo válido para esa clase.'})
    return row.scope_overrides + [{'heads': heads, 'cls': cls, 'kind': kind, 'attribution': data.get('attribution', ''), 'confidence': 'high'}]


def planned(row, action, data):
    if action in LABELS:
        return label(row, action, data)
    if action == 'note':
        if 'notes' not in data:
            raise ValidationError({'notes': 'Escribe las notas del sufijo.'})
        return {'notes': data['notes']}
    if action == 'add_scope_override':
        return {'scope_overrides': scope_override(row, data)}
    index = data.get('index')
    if index is None or index >= len(row.scope_overrides):
        raise ValidationError({'index': 'Ese alcance no existe.'})
    return {'scope_overrides': [o for i, o in enumerate(row.scope_overrides) if i != index]}


def unchanged(row, fields):
    return all(getattr(row, key) == value for key, value in fields.items())


def save(row, fields, action, user, note=''):
    """Apply fields to a locked row and audit it; a no-op edit returns False without saving."""
    before = snapshot(row)
    if unchanged(row, fields):
        return False
    for key, value in fields.items():
        setattr(row, key, value)
    if 'owner_confirmed' in fields:
        row.confirmed_by, row.confirmed_at = (user, timezone.now()) if row.owner_confirmed else (None, None)
    row.save()
    CatalogCodeSuffixChange.objects.create(suffix=row, token=row.token, action=action, actor=user, before=before, after=snapshot(row),
                                           note=note, version=row.version)
    return True


def alias_label(root, certain=True):
    """An alias carries its root's label and is exactly as trusted as the root; an uncertain spelling (still awaiting a label,
    e.g. HEML 'HELM?') never becomes auto-eligible through its root, as in the v2 owner-confirms scenario."""
    trusted = certain and root.cls != 'UNKNOWN' and (root.owner_confirmed or (root.status == 'seed_confirmed' and root.confidence in ('high', 'medium')))
    status = 'owner_confirmed' if trusted else 'needs_owner_label' if not certain or root.cls == 'UNKNOWN' else root.status
    return {**{key: getattr(root, key) for key in LABEL_FIELDS}, 'confidence': root.confidence, 'status': status, 'owner_confirmed': trusted}


def relabel(row, fields, action, user, note=''):
    """An owner label on a canonical row also reaches its aliases (other spellings of the same suffix), audited per row:
    a demoted TAG never leaves an alias behind as a strippable TAG."""
    if not save(row, fields, action, user, note=note):
        return False
    if action in LABELS and not row.alias_of_id:
        for alias in CatalogCodeSuffix.objects.select_for_update().filter(alias_of=row).order_by('token'):
            certain = alias.status != 'needs_owner_label'
            if certain or alias.cls != row.cls:
                save(alias, alias_label(row, certain), action, user, note=f'Alias de {row.token}')
    return True


def locked_row(token):
    row = CatalogCodeSuffix.objects.select_for_update().filter(token=normalized_token(token)).first()
    if not row:
        raise NotFound('Ese sufijo no está en la tabla.')
    return row


def add_alias(row, data, user):
    alias = normalized_token(data.get('alias'))
    if not ALIAS_RE.match(alias):
        raise ValidationError({'alias': 'Escribe el alias como un solo sufijo del SKU: letras y números, A/B o dos palabras (NEW ERA).'})
    if row.match != 'exact':
        raise ValidationError('Solo los sufijos exactos admiten alias.')
    root = locked_row(row.alias_of.token) if row.alias_of_id else row
    if alias == root.token:
        raise ValidationError({'alias': 'El alias debe ser una escritura distinta del sufijo.'})
    if root.cls != 'VARIANT' and (SIZE_RE.match(alias) or any(c.isdigit() for c in alias)):
        # A digit tail is a size or part of a part number: as an exact TAG it would be stripped from the OEM base.
        raise ValidationError({'alias': 'Un alias con números solo puede pertenecer a una variante (por ejemplo, una medida).'})
    existing = CatalogCodeSuffix.objects.select_for_update().filter(token=alias).first()
    if existing and (existing.aliases.exists() or existing.match != 'exact'):
        raise ValidationError({'alias': f'{alias} ya agrupa otros alias; edítalo directamente.'})
    if existing and existing.alias_of_id != root.pk and existing.cls not in ('UNKNOWN', root.cls):
        raise ValidationError({'alias': f'{alias} ya está clasificado como {existing.get_cls_display().lower()}; cámbialo primero desde su fila.'})
    fields = {'alias_of': root, **alias_label(root)}
    note = data.get('note') or f'Alias de {root.token}'
    if existing:
        if not save(existing, fields, 'set_alias', user, note=note):
            return False
        target = existing
    else:
        target = CatalogCodeSuffix(token=alias, match='exact', notes=f'Alias de {root.token} agregado por el propietario.', **fields)
        if target.owner_confirmed:
            target.confirmed_by, target.confirmed_at = user, timezone.now()
        target.save()
        CatalogCodeSuffixChange.objects.create(suffix=target, token=alias, action='set_alias', actor=user, before={}, after=snapshot(target),
                                               note=note, version=target.version)
    CatalogCodeSuffixChange.objects.create(suffix=root, token=root.token, action='add_alias', actor=user, before={},
                                           after={'alias': alias}, note=data.get('note', ''), version=target.version)
    return True


class SuffixList(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_suffixes_list', responses=SuffixListResponse, parameters=[
        OpenApiParameter('class', OpenApiTypes.STR, enum=values(SUFFIX_CLASSES)),
        OpenApiParameter('status', OpenApiTypes.STR, enum=values(SUFFIX_STATUSES)),
        OpenApiParameter('kind', OpenApiTypes.STR, enum=values(TAG_KINDS) + values(VARIANT_KINDS)),
        OpenApiParameter('has_unlock', OpenApiTypes.BOOL, description='true: solo sufijos cuyo etiquetado desbloquearía SKU.'),
        OpenApiParameter('search', OpenApiTypes.STR, description='Sufijo, atribución o propuesta.'),
        OpenApiParameter('page', OpenApiTypes.INT)],
        description='Tabla de sufijos de códigos con filtros. Ordena por SKU que llevan el sufijo (o por desbloqueos con has_unlock).')
    def get(self, request):
        require_table()
        params, rows = request.query_params, rows_query()
        for key, field, choices in (('class', 'cls', SUFFIX_CLASSES), ('status', 'status', SUFFIX_STATUSES)):
            if params.get(key):
                if params[key] not in values(choices):
                    raise ValidationError({key: 'El filtro no es válido.'})
                rows = rows.filter(**{field: params[key]})
        if params.get('kind'):
            if params['kind'] not in values(TAG_KINDS) + values(VARIANT_KINDS):
                raise ValidationError({'kind': 'El filtro no es válido.'})
            rows = rows.filter(Q(tag_kind=params['kind']) | Q(variant_kind=params['kind']))
        search = normalized_token(params.get('search'))
        if search:
            rows = rows.filter(Q(token__icontains=search) | Q(attribution__icontains=search) | Q(proposed_label__icontains=search))
        unlock = params.get('has_unlock', '').lower()
        if unlock not in ('', 'true', 'false'):
            raise ValidationError({'has_unlock': 'Usa true o false.'})
        rows = rows.annotate(unlock=Cast(UNLOCK, IntegerField()), seen=Cast(KT('stats__count_all'), IntegerField()))
        if unlock == 'true':
            rows = rows.filter(unlock__gt=0).order_by('-unlock', 'token')
        else:
            rows = (rows.filter(Q(unlock__isnull=True) | Q(unlock__lte=0)) if unlock == 'false' else rows).order_by(F('seen').desc(nulls_last=True), 'token')
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        every = CatalogCodeSuffix.objects.order_by()
        counts = {'cls': dict(every.values('cls').annotate(n=Count('pk')).values_list('cls', 'n')),
                  'status': dict(every.values('status').annotate(n=Count('pk')).values_list('status', 'n')),
                  'has_unlock': every.annotate(unlock=Cast(UNLOCK, IntegerField())).filter(unlock__gt=0).count()}
        return Response({'count': paginator.page.paginator.count, 'next': paginator.get_next_link(), 'previous': paginator.get_previous_link(),
                         'results': SuffixRow(page, many=True).data, 'version': table_version(), 'counts': counts,
                         'company_suffixes': {key: value.strip().upper() for key, value in company_registry().items()}})

    @extend_schema(operation_id='v1_management_catalog_suffixes_bulk_update', request=SuffixBulkEdit, responses={
        200: SuffixBulkResponse, 409: OpenApiResponse(description='Algún sufijo cambió desde que lo revisaste; no se aplicó ninguno.')},
        description='Aplica la misma etiqueta del propietario (TAG, VARIANTE o desconocido) a hasta 100 sufijos, con un registro de auditoría por sufijo.')
    def patch(self, request):
        require_table()
        data = SuffixBulkEdit(data=request.data)
        data.is_valid(raise_exception=True)
        data = data.validated_data
        if len({normalized_token(r['token']) for r in data['rows']}) != len(data['rows']):
            raise ValidationError({'rows': 'Cada sufijo debe aparecer una sola vez.'})
        with transaction.atomic():
            lock_table()
            touched = []
            for item in data['rows']:
                row = locked_row(item['token'])
                fields = label(row, data['action'], data)
                if row.version != item['version'] and not unchanged(row, fields):
                    raise SuffixConflict(f'{row.token} cambió. Actualiza la tabla antes de continuar.')
                relabel(row, fields, data['action'], request.user, note=data.get('note', ''))
                touched.append(row.pk)
        return Response({'results': SuffixRow(rows_query().filter(pk__in=touched).order_by('token'), many=True).data, 'version': table_version()})


class SuffixDetail(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_suffix_detail', responses=SuffixDetailResponse,
                   description='Un sufijo con sus alias, alcances y sus últimos cambios auditados.')
    def get(self, request, token):
        require_table()
        row = rows_query().filter(token=normalized_token(token)).first()
        if not row:
            raise NotFound('Ese sufijo no está en la tabla.')
        return Response(detail(row))

    @extend_schema(operation_id='v1_management_catalog_suffix_update', request=SuffixEdit, responses={
        200: SuffixDetailResponse, 409: OpenApiResponse(description='El sufijo cambió desde que lo revisaste.')},
        description='confirm_tag, set_variant y set_unknown etiquetan el sufijo (tipo y atribución opcionales); note, add_alias, '
                    'add_scope_override y remove_scope_override completan la fila. Cada cambio sube la versión de la tabla y queda auditado.')
    def patch(self, request, token):
        require_table()
        data = SuffixEdit(data=request.data)
        data.is_valid(raise_exception=True)
        data, action = data.validated_data, data.validated_data['action']
        with transaction.atomic():
            lock_table()
            row = locked_row(token)
            if action == 'add_alias':
                if row.version != data['version'] and not CatalogCodeSuffix.objects.filter(
                        token=normalized_token(data.get('alias')), alias_of=row.alias_of or row).exists():
                    raise SuffixConflict()
                add_alias(row, data, request.user)
            else:
                fields = planned(row, action, data)
                if row.version != data['version'] and not unchanged(row, fields):
                    raise SuffixConflict()
                relabel(row, fields, action, request.user, note=data.get('note', ''))
        return Response(detail(rows_query().get(pk=row.pk)))


class CompanySuffixes(APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(operation_id='v1_management_catalog_company_suffixes', responses=CompanySuffixesResponse,
                   description='Sufijos que identifican un código de empresa (SKU-FEB → FEBEST), los mismos que usa la biblioteca de referencias.')
    def get(self, request):
        registry = 'settings' if hasattr(settings, 'CATALOG_COMPANY_SUFFIXES') else 'table' if suffix_table().source == 'db' else 'fallback'
        return Response({'suffixes': {key: value.strip().upper() for key, value in company_registry().items()}, 'registry': registry})
