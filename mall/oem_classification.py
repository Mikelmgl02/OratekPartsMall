"""Reviewed OEM promotion: signed preview, then one atomic reference/main/merge operation."""
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import line_merge, lines, references
from .line_views import Conflict, ErrorBodies, serialize_line
from .management import IsSuperuser
from .models import OEMLookupCache, Part, Reference
from .oem_lookup import safe_url
from .oem_catalog_matches import resolve, equivalents

SALT = 'oem-classification-preview-v1'
RESEARCH_SALT = 'oem-classification-research-v1'


class OEMSelection(serializers.Serializer):
    code = serializers.CharField(max_length=120)
    brand = serializers.CharField(max_length=120)


class OEMPromotionRequest(serializers.Serializer):
    selections = OEMSelection(many=True, required=False, min_length=1, max_length=20)
    research_token = serializers.CharField(max_length=4000)
    code = serializers.CharField(max_length=120)
    brand = serializers.CharField(max_length=120)
    expected_version = serializers.IntegerField()
    preview_token = serializers.CharField(required=False, max_length=100000)
    confirmed = serializers.BooleanField(default=False)


def research_proposal(pk, data):
    try:
        evidence = signing.loads(data['research_token'], salt=RESEARCH_SALT, max_age=3600)
    except signing.BadSignature:
        raise Conflict('La investigación venció o no es válida. Vuelve a buscar el OEM.')
    if evidence['part'] != str(pk):
        raise ValidationError({'detail': 'La investigación corresponde a otra ficha.'})
    cached = OEMLookupCache.objects.filter(pk=evidence['key']).first()
    if cached is None:
        raise Conflict('La investigación ya no está disponible. Vuelve a buscar el OEM.')
    available = equivalents(cached.result.get('references', []))
    def identity(row):
        return references.clave(row['code']), references.brand_name(row['brand'])
    requested = data.get('selections') or [{'code': data['code'], 'brand': data['brand']}]
    selected = []
    for item in requested:
        row = next((row for row in available if identity(row) == identity(item)), None)
        if row is None:
            raise ValidationError({'detail': 'Selecciona solo equivalencias verificables de la misma pieza; no conjuntos ni propuestas inciertas.'})
        if identity(row) not in {identity(found) for found in selected}:
            selected.append(row)
    proposal = next((row for row in selected if identity(row) == identity(data)), None)
    if proposal is None:
        raise ValidationError({'detail': 'El OEM propuesto como principal debe estar entre las referencias seleccionadas.'})
    return evidence, proposal, available, selected


def snapshot(parts):
    ids = [part.pk for part in parts]
    family = Part.objects.filter(merged_into_id__in=ids)
    ids += list(family.values_list('pk', flat=True))
    return {
        'lines': list(Part.objects.filter(pk__in=ids).order_by('pk').values('id', 'sku', 'version', 'changed_at', 'estado', 'merged_into_id')),
        'references': list(Reference.all_objects.filter(part_id__in=ids).order_by('pk').values('id', 'version', 'tipo', 'estado', 'brand', 'code', 'supplier_id', 'is_main', 'part_id')),
    }


def json_snapshot(parts):
    # Signing's JSON encoder does not accept UUIDs or dates; use the same lossless text conversion on both reads.
    return line_merge.jsonable(snapshot(parts))


class OEMClassification(ErrorBodies, APIView):
    permission_classes = [IsSuperuser]

    @extend_schema(request=OEMPromotionRequest, responses={200: {'type': 'object', 'additionalProperties': True}},
                   description='Previsualiza la promoción OEM; con preview_token y confirmed aplica atómicamente la operación revisada.')
    def post(self, request, pk):
        serializer = OEMPromotionRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        evidence, proposal, proposals, selected = research_proposal(pk, data)
        try:
            with transaction.atomic():
                source = Part.objects.filter(pk=pk).first()
                if source is None:
                    raise NotFound('No existe esa ficha.')
                target, matches = resolve(source, proposals)
                locked = line_merge.lock_lines([source.pk, target.pk])
                source, target = locked[source.pk], locked[target.pk]
                references.lock(*sorted({references.clave(source.sku), *(references.clave(row['code']) for row in proposals)}))
                resolved, matches = resolve(source, proposals)
                if resolved.pk != target.pk:
                    raise Conflict('El propietario del OEM cambió. Vuelve a revisar la operación.')
                if source.estado != 'publicada' or target.estado not in ('publicada', 'referencia'):
                    raise Conflict('La ficha ya no está publicada o el destino no admite esta operación. Revisa el inventario.')
                if source.version != data['expected_version'] or source.version != evidence['version']:
                    raise Conflict('La ficha cambió desde la investigación. Vuelve a buscar y revisar el OEM.')
                old = references.main_of(source, lock=True)
                if old is None or old.supplier_id or old.tipo == 'interno' or old.estado == 'disputa':
                    raise Conflict('La referencia principal requiere revisión manual; no se modificará su ámbito de proveedor ni una referencia en disputa.')
                if old.tipo == 'oem' or any(old.clave == references.clave(row['code']) for row in selected):
                    raise Conflict('El código actual ya es OEM o coincide con el elegido. Este flujo conserva un código distinto confirmado como aftermarket; revisa la referencia directamente.')
                target_main = references.main_of(target, lock=True)
                preserve_main = bool(target_main and target_main.tipo == 'oem' and target_main.supplier_id is None
                                     and target_main.estado in references.EXCLUSIVE)
                existing_choice = Reference.objects.filter(part=target, clave=references.clave(proposal['code']), supplier__isnull=True).filter(
                    references.brand_q(references.brand_name(proposal['brand']))).first()
                final_code = target_main.code if preserve_main else existing_choice.code if existing_choice else proposal['code']
                final_brand = target_main.brand if preserve_main else references.brand_name(proposal['brand'])
                current = {'selected': selected, 'equivalences': matches, 'preserve_main': preserve_main, 'source': str(source.pk), 'target': str(target.pk), 'proposal': proposal,
                           'snapshot': json_snapshot([source, target])}
                token = data.get('preview_token')
                if not token:
                    return Response({'preview_token': signing.dumps(current, salt=SALT, compress=True),
                                     'merge': source.pk != target.pk,
                                     'source': {'id': str(source.pk), 'sku': source.sku, 'description': source.description,
                                                'items': source.supplier_items.count(), 'images': source.images.count()},
                                     'target': {'id': str(target.pk), 'sku': target.sku, 'description': target.description,
                                                'items': target.supplier_items.count(), 'images': target.images.count()},
                                     'oem': final_code, 'brand': final_brand, 'selected_oem': proposal['code'], 'selected_brand': proposal['brand'],
                                     'preserve_main': preserve_main, 'matched_equivalences': matches, 'previous_code': old.code,
                                     'selected_oems': [{**row, 'already_registered': any(owner['id'] == str(target.pk) for match in matches
                                         if references.clave(match['code']) == references.clave(row['code']) and references.brand_name(match['brand']) == references.brand_name(row['brand'])
                                         for owner in match['catalog_matches'])} for row in selected]})
                if not data['confirmed']:
                    raise ValidationError({'detail': 'Confirma la misma pieza y que el código anterior es aftermarket antes de aplicar.'})
                try:
                    reviewed = signing.loads(token, salt=SALT, max_age=900)
                except signing.BadSignature:
                    raise Conflict('La vista previa venció. Revisa de nuevo la operación.')
                if reviewed != current:
                    raise Conflict('Las referencias o fichas cambiaron desde la vista previa. Revisa de nuevo; no se aplicó ningún cambio.')
                # Retype before merging so CatalogMerge records the reviewed aftermarket identity in its history.
                references.update(old, tipo='aftermarket', estado='verificado', actor=request.user,
                                  reference_source=proposal['source_url'],
                                  detail={**old.detail, 'oem_classification': {'previous_tipo': old.tipo, 'oem': final_code, 'selected_oems': [{'code': row['code'], 'brand': row['brand'], 'source': row['source_url']} for row in selected], 'source': proposal['source_url'], 'actor': request.user.pk, 'at': timezone.now().isoformat()}},
                                  notes=(old.notes + '\nClasificación revisada: código no OEM; el OEM principal se confirmó por el administrador.').strip())
                merge = None
                if target.pk != source.pk:
                    merge = line_merge.merge_lines(target, [source], actor=request.user, reason='clasificacion_oem')
                    target.refresh_from_db()
                chosen = None
                for row in selected:
                    ref, _ = references.add(target, row['code'], row['brand'], tipo='oem', estado='verificado',
                                            source=row['source_url'], actor=request.user, absorb_library=False)
                    if ref.supplier_id or ref.estado == 'disputa':
                        raise Conflict('Una referencia OEM requiere resolver su ámbito o disputa. No se aplicó ningún cambio.')
                    ref = references.update(ref, tipo='oem', estado='verificado', actor=request.user, reference_source=row['source_url'])
                    if row == proposal:
                        chosen = ref
                if not preserve_main:
                    references.set_main(target, chosen, actor=request.user, reason='Clasificación OEM revisada con fuentes')
                # A spelling-equivalent existing Reference may have been reused or absorbed by a merge.
                old.refresh_from_db()
                previous = Reference.objects.filter(pk=old.reemplazado_por_id or old.pk, part_id=target.pk).first()
                if previous is None or previous.pk == chosen.pk:
                    raise Conflict('No se pudo conservar el código anterior como referencia hija. No se aplicó ningún cambio.')
                if previous.tipo != 'aftermarket':
                    references.update(previous, tipo='aftermarket', estado='verificado', actor=request.user)
                Part.objects.filter(pk=target.pk).update(version=F('version') + 1)
                lines.touch_visible(target)
                target.refresh_from_db()
                return Response({'applied': True, 'line': serialize_line(target, request), 'previous_code': old.code,
                                 'merge_id': str(merge.pk) if merge else None})
        except IntegrityError:
            raise Conflict('Otro proceso registró ese código. Vuelve a revisar; no se aplicó ningún cambio.')
