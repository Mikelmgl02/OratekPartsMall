"""Preview and atomically import internal SKUs and their alterno codes."""
import hashlib
import io
import json
from datetime import timedelta
from zipfile import BadZipFile, ZipFile
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from django.core import signing
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.utils import extend_schema

from .management import IsSuperuser
from .models import CatalogClassificationState, CatalogImportBatch, CatalogImportIssue, CatalogImportJob, Part, PartCode
from .catalog_recovery import date_identifier, date_recovery, description_text, recovery_evidence
from .workbook_reader import header, identifier_text

MAX_BYTES = 5 * 1024 * 1024
BATCH_SIZE = 500
SALT = 'motionpartes.catalog-excel'
HEADERS = {'SKU': 'sku', 'SKU_INTERNO': 'sku', 'NOMBRE': 'name',
           'DESCRIPCION': 'description', 'CODIGO_ALTERNO': 'code',
           'MARCA_ALTERNO': 'brand', 'ACTIVO': 'active',
           'CATEGORIA': 'category', 'SUBCATEGORIA': 'subcategory'}


class ImportRequest(serializers.Serializer):
    file = serializers.FileField()
    mode = serializers.ChoiceField(choices=['preview', 'import'], default='preview')
    preview_token = serializers.CharField(required=False)
    corrections = serializers.JSONField(required=False, default=list)
    skip_rows = serializers.JSONField(required=False, default=list)
    job_id = serializers.UUIDField(required=False)

    def validate_corrections(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('Las correcciones deben ser una lista.')
        seen = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {'row', 'column', 'value'}:
                raise serializers.ValidationError('Indica la fila, columna y código de cada corrección.')
            row, column, code = item['row'], item['column'], item['value']
            if type(row) is not int or row < 2 or column not in ('SKU', 'CODIGO_ALTERNO'):
                raise serializers.ValidationError('La fila o columna de la corrección no es válida.')
            if not isinstance(code, str) or not code.strip() or len(code.strip()) > (200 if column == 'SKU' else 120):
                raise serializers.ValidationError('Ingresa un código de texto dentro de la longitud permitida.')
            if (row, column) in seen:
                raise serializers.ValidationError('No repitas una corrección para la misma celda.')
            seen.add((row, column))
        return value

    def validate_skip_rows(self, value):
        if not isinstance(value, list) or any(type(row) is not int or row < 2 for row in value) or len(set(value)) != len(value):
            raise serializers.ValidationError('Las filas excluidas deben ser una lista de números de fila únicos.')
        return value


class ImportErrorRow(serializers.Serializer):
    row = serializers.IntegerField()
    column = serializers.CharField()
    message = serializers.CharField()


class ImportPreviewRow(serializers.Serializer):
    sku = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField()
    category = serializers.CharField()
    subcategory = serializers.CharField()
    active = serializers.BooleanField()
    codes = serializers.ListField(child=serializers.DictField())
    action = serializers.CharField()
    added_codes = serializers.IntegerField()


class ImportSummary(serializers.Serializer):
    rows = serializers.IntegerField()
    created_skus = serializers.IntegerField()
    updated_skus = serializers.IntegerField()
    unchanged_skus = serializers.IntegerField()
    added_codes = serializers.IntegerField()
    skipped_rows = serializers.IntegerField(required=False)
    rejected_rows = serializers.IntegerField(required=False)


class ImportResponse(serializers.Serializer):
    valid = serializers.BooleanField()
    imported = serializers.BooleanField()
    summary = ImportSummary()
    errors = ImportErrorRow(many=True)
    error_count = serializers.IntegerField()
    warnings = ImportErrorRow(many=True)
    warning_count = serializers.IntegerField()
    preview = ImportPreviewRow(many=True)
    preview_count = serializers.IntegerField()
    preview_token = serializers.CharField(required=False)
    job_id = serializers.UUIDField(required=False)
    status = serializers.CharField(required=False)
    filename = serializers.CharField(required=False)
    progress = serializers.DictField(required=False)
    applied_summary = ImportSummary(required=False)
    recovery_rows = serializers.ListField(child=serializers.DictField(), required=False)
    recovery_count = serializers.IntegerField(required=False)
    source_rows = serializers.IntegerField(required=False)
    skipped_rows = serializers.ListField(child=serializers.IntegerField(), required=False)
    skipped_row_count = serializers.IntegerField(required=False)
    numeric_warning_count = serializers.IntegerField(required=False)
    date_recovery_count = serializers.IntegerField(required=False)
    rejected_row_count = serializers.IntegerField(required=False)
    pending_error_count = serializers.IntegerField(required=False)


class StalePreview(APIException):
    status_code = 409
    default_detail = 'El archivo o el catálogo cambió desde la vista previa. Revisa el archivo otra vez antes de importar.'


def read_excel(raw, *, include_warnings=False, include_recovery=False, include_records=False, corrections=None, skip_rows=None):
    errors = []
    warnings = []
    groups = {}
    row_count = 0
    recoveries = []
    corrections = {(item['row'], HEADERS[item['column']]): item['value'].strip().upper() for item in (corrections or [])}
    skip_rows = set(skip_rows or [])
    numeric_warning_count = 0
    records = {}
    def error(row, column, message):
        errors.append({'row': row, 'column': column, 'message': message})
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 30 * 1024 * 1024:
                raise ValidationError('El contenido del archivo Excel es demasiado grande.')
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
    except (BadZipFile, ValueError, KeyError, OSError, ParseError, DefusedXmlException, InvalidFileException):
        raise ValidationError('No pudimos leer el archivo. Usa un libro Excel .xlsx válido.')
    try:
        sheet = next((s for s in workbook.worksheets if header(s.title) == 'INVENTARIO'), workbook.worksheets[0])
        if (sheet.max_column or 0) > 20:
            raise ValidationError('Usa únicamente las columnas de la plantilla.')
        # Some spreadsheet exporters report incomplete dimensions. Stream actual
        # rows so no populated row disappears because of that metadata.
        sheet.reset_dimensions()
        rows = sheet.iter_rows(max_col=20)
        first = next(rows)
        columns = {}
        for index, cell in enumerate(first):
            if cell.value is None:
                continue
            key = HEADERS.get(header(cell.value))
            if not key or key in columns:
                raise ValidationError('Usa los encabezados de la plantilla, sin columnas desconocidas o repetidas.')
            columns[key] = index
        if 'sku' not in columns:
            raise ValidationError('Falta la columna SKU en la primera fila.')
        evidence = recovery_evidence(sheet, columns)
        date_cells = evidence[2]
        date_rows = {number for number, _ in date_cells}
        if any(key not in date_cells for key in corrections) or not skip_rows.issubset(date_rows):
            raise ValidationError('Solo puedes corregir o excluir filas con códigos convertidos en fechas por Excel.')
        for number, cells in enumerate(rows, 2):
            if not any(cell.value is not None for cell in cells):
                continue
            row_count += 1
            record = {'row': number, 'values': {key: '' for key in set(HEADERS.values())}, 'original': {}, 'recovery': [], 'sku': None}
            if include_records:
                records[number] = record
            values = {}
            bad = False
            description = description_text(cells, columns)
            resolved_sku = None
            for key, index in columns.items():
                cell = cells[index]
                value = cell.value
                if value is None or value == '':
                    continue
                if include_records:
                    kind = 'formula' if cell.data_type == 'f' else 'error' if cell.data_type == 'e' else 'date' if cell.is_date else 'text' if isinstance(value, str) else 'boolean' if isinstance(value, bool) else 'number'
                    original_value = value.isoformat() if hasattr(value, 'isoformat') else str(value)
                    record['original'][key] = {'value': original_value, 'kind': kind, 'number_format': cell.number_format}
                    record['values'][key] = str(value).strip().upper()
                column = next(name for name, field in HEADERS.items() if field == key)
                if cell.data_type in ['f', 'e']:
                    error(number, column, 'Usa valores de texto, sin fórmulas ni errores de Excel.')
                    bad = True
                    continue
                if key in ['sku', 'code']:
                    if date_identifier(cell):
                        recovery = date_recovery(cell, row=number, column=column, field=key, description=description, evidence=evidence)
                        if (number, key) in corrections:
                            reviewed_code = corrections[number, key]
                            if not recovery['automatic'] or reviewed_code != recovery['suggested_code']:
                                recovery.update(automatic=False, reason='Código revisado en la vista previa de importación.')
                            recovery.update(value=reviewed_code, resolved=True)
                        if number in skip_rows:
                            recovery.update(skipped=True, resolved=False, value='')
                        recoveries.append(recovery)
                        record['recovery'].append(recovery)
                        record['values'][key] = recovery['value'] or recovery['suggested_code']
                        if recovery['skipped']:
                            continue
                        if not recovery['resolved']:
                            error(number, column, 'Excel convirtió el código en una fecha. Revisa la sugerencia o excluye esta fila en la vista previa.')
                            bad = True
                            continue
                        value = recovery['value']
                        if key == 'sku':
                            resolved_sku = value
                        warnings.append({'row': number, 'column': column, 'message': f'Código reconstruido desde una fecha de Excel: {value}. {recovery["reason"]}'})
                    else:
                        try:
                            value = identifier_text(cell)
                        except ValueError as exception:
                            error(number, column, str(exception))
                            bad = True
                            continue
                        if not isinstance(cell.value, str):
                            warnings.append({'row': number, 'column': column, 'message': f'Código numérico convertido a texto: {value}.'})
                            numeric_warning_count += 1
                # Some exports copy the SKU into NOMBRE. Recover its paired
                # date after all columns are read, regardless of header order.
                if key == 'name' and date_identifier(cell) and cell.value == cells[columns['sku']].value:
                    record['values']['name'] = ''
                    continue
                text = str(value).strip().upper()
                if not text:
                    continue
                if key == 'active':
                    flag = header(text)
                    if flag not in ['SI', 'NO', 'TRUE', 'FALSE', '1', '0']:
                        error(number, column, 'Usa SI o NO para indicar si el SKU está activo.')
                        bad = True
                    else:
                        values[key] = flag in ['SI', 'TRUE', '1']
                elif len(text) > {'sku': 200, 'name': 200, 'description': 10000, 'brand': 120, 'code': 120, 'category': 120, 'subcategory': 120}[key]:
                    error(number, column, 'El valor supera la longitud permitida.')
                    bad = True
                else:
                    values[key] = text
            record['sku'] = values.get('sku')
            for key, value in values.items():
                record['values'][key] = ('SI' if value else 'NO') if key == 'active' else value
            if number in skip_rows:
                continue
            if resolved_sku and 'name' in columns and date_identifier(cells[columns['name']]) and cells[columns['name']].value == cells[columns['sku']].value:
                values['name'] = resolved_sku
            if not values.get('sku'):
                if not bad:
                    error(number, 'SKU', 'Ingresa el SKU interno en esta fila.')
                continue
            if bad:
                continue
            sku = values['sku']
            group = groups.setdefault(sku, {'sku': sku, 'row': number, 'fields': {}, 'codes': {}})
            for key in ['name', 'description', 'active', 'category', 'subcategory']:
                if key in values:
                    if key in group['fields'] and group['fields'][key] != values[key]:
                        column = next(name for name, field in HEADERS.items() if field == key)
                        error(number, column, f'Las filas del SKU {sku} tienen datos diferentes en esta columna.')
                    else:
                        group['fields'][key] = values[key]
            if values.get('brand') and not values.get('code'):
                error(number, 'CODIGO_ALTERNO', 'Ingresa el código alterno para esta marca.')
            if values.get('code'):
                group['codes'].setdefault((values.get('brand', ''), values['code']), number)
        if not row_count:
            raise ValidationError('El archivo no contiene filas de inventario.')
        if not groups and not errors and not include_records:
            raise ValidationError('No quedan filas de inventario para importar. Revisa los códigos o selecciona otro archivo.')
        result = (groups, errors, row_count - len(skip_rows))
        if include_recovery or include_records:
            metadata = {'recovery_rows': recoveries, 'recovery_count': len(recoveries), 'source_rows': row_count,
                        'skipped_rows': sorted(skip_rows), 'skipped_row_count': len(skip_rows),
                        'numeric_warning_count': numeric_warning_count,
                        'date_recovery_count': sum(item['resolved'] and not item['skipped'] for item in recoveries)}
            if include_records:
                return (*result, warnings, metadata, records)
            return (*result, warnings, metadata)
        return (*result, warnings) if include_warnings else result
    except (ValueError, KeyError, OSError, IndexError, ParseError, DefusedXmlException, BadZipFile):
        raise ValidationError('No pudimos leer las filas del archivo Excel.')
    finally:
        workbook.close()


def build_plan(groups, errors, row_count, *, lock=False, warnings=None, error_sink=None):
    errors = list(errors)
    candidates = set(groups) | {code for group in groups.values() for _, code in group['codes']}
    parts, codes = {}, []
    # Bound SQL parameter lists even when a workbook contains tens of thousands
    # of rows. An individual SKU may itself have many alterno codes.
    ordered_candidates = sorted(candidates)
    for offset in range(0, len(ordered_candidates), 2000):
        chunk = ordered_candidates[offset:offset + 2000]
        parts_query = Part.objects.filter(sku__in=chunk).select_related('merged_into').order_by('pk')
        codes_query = PartCode.objects.filter(code__in=chunk).select_related('part').order_by('pk')
        if lock:
            parts_query = parts_query.select_for_update(of=('self',))
            codes_query = codes_query.select_for_update(of=('self',))
        parts.update((part.sku, part) for part in parts_query)
        codes.extend(codes_query)
    owners = {sku: [('', part.merged_into.sku if part.merged_into_id else sku)] for sku, part in parts.items()}
    for entry in codes:
        owners.setdefault(entry.code, []).append((entry.brand, entry.part.sku))
    for group in groups.values():
        for brand, code in group['codes']:
            owners.setdefault(code, []).append((brand, group['sku']))
    existing_pairs = {(entry.part.sku, entry.brand, entry.code) for entry in codes}
    summary = {'rows': row_count, 'created_skus': 0, 'updated_skus': 0, 'unchanged_skus': 0, 'added_codes': 0}
    preview = []
    for sku, group in groups.items():
        if any(owner != sku for _, owner in owners.get(sku, [])):
            errors.append({'row': group['row'], 'column': 'SKU', 'message': f'El SKU {sku} ya se usa como alterno de otro SKU.'})
        for (brand, code), number in group['codes'].items():
            conflicts = code in groups and code != sku
            conflicts |= any(owner != sku and (not brand or not other_brand or brand == other_brand) for other_brand, owner in owners.get(code, []))
            if conflicts:
                errors.append({'row': number, 'column': 'CODIGO_ALTERNO', 'message': f'El código {code} identifica otro SKU. Revisa su asignación.'})
        existing = parts.get(sku)
        if existing and existing.merged_into_id:
            errors.append({'row': group['row'], 'column': 'SKU', 'message': f'El SKU {sku} ya se agrupó bajo {existing.merged_into.sku}. Usa el SKU interno de destino y conserva este código como alterno.'})
        added = [(brand, code) for brand, code in group['codes'] if (sku, brand, code) not in existing_pairs]
        group['existing'] = existing
        group['added'] = added
        changed = existing and any(getattr(existing, field) != value for field, value in group['fields'].items())
        action = 'create' if not existing else 'update' if changed or added else 'unchanged'
        group['action'] = action
        summary[{'create': 'created_skus', 'update': 'updated_skus', 'unchanged': 'unchanged_skus'}[action]] += 1
        summary['added_codes'] += len(added)
        preview.append({'sku': sku, 'name': group['fields'].get('name', existing.name if existing else ''),
                        'description': group['fields'].get('description', existing.description if existing else ''),
                        'category': group['fields'].get('category', existing.category if existing else ''),
                        'subcategory': group['fields'].get('subcategory', existing.subcategory if existing else ''),
                        'active': group['fields'].get('active', existing.active if existing else True),
                        'codes': [{'brand': brand, 'code': code} for brand, code in added[:20]],
                        'action': action, 'added_codes': len(added)})
    relevant_pairs = {}
    for group in groups.values():
        relevant_pairs.setdefault(group['sku'], set()).add('')
        for brand, code in group['codes']:
            relevant_pairs.setdefault(code, set()).add(brand)
    relevant_codes = [entry for entry in codes if any(not brand or not entry.brand or brand == entry.brand for brand in relevant_pairs.get(entry.code, []))]
    snapshot = {
        'parts': sorted((str(part.pk), part.sku, part.name, part.description, part.active, part.category, part.subcategory, str(part.merged_into_id or '')) for part in parts.values()),
        'codes': sorted((entry.pk, str(entry.part_id), entry.part.sku, entry.brand, entry.code) for entry in relevant_codes),
    }
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    warnings = warnings or []
    result = {'valid': not errors, 'imported': False, 'summary': summary, 'errors': errors[:100], 'error_count': len(errors), 'preview': preview[:100], 'preview_count': len(preview), 'warnings': warnings[:100], 'warning_count': len(warnings)}
    if error_sink is not None:
        error_sink.extend(errors)
    return result, fingerprint


def split_import_plan(groups, errors, records, metadata, warnings):
    """Hold out erroneous SKU groups; unrelated valid rows can proceed."""
    all_errors = []
    build_plan(groups, errors, metadata['source_rows'], error_sink=all_errors)
    rejected = {item['row'] for item in all_errors} | set(metadata.get('skipped_rows', []))
    bad_skus = {records[row]['sku'] for row in rejected if records[row]['sku']}
    rejected.update(row for row, record in records.items() if record['sku'] in bad_skus)
    accepted = {sku: group for sku, group in groups.items() if sku not in bad_skus}
    issues = []
    errors_by_row = {}
    for item in all_errors:
        errors_by_row.setdefault(item['row'], []).append(item)
    for row in sorted(rejected):
        record = records[row]
        row_errors = errors_by_row.get(row) or [{'row': row, 'column': 'SKU', 'message': (
            'Fila excluida de esta importación. Puedes corregirla después en Errores de importación.'
            if row in metadata.get('skipped_rows', []) else
            'Otra fila de este SKU tiene errores. El SKU completo queda pendiente para conservar datos consistentes.'
        )}]
        issues.append({'row': row, 'values': record['values'], 'original': record['original'], 'recovery': record['recovery'], 'errors': row_errors})
    # A held-out group cannot contribute aliases or metadata to the accepted
    # plan. Revalidate the remaining groups independently before staging them.
    accepted_rows = len(records) - len(rejected)
    result, _ = build_plan(accepted, [], accepted_rows, warnings=warnings)
    if not result['valid']:
        raise ValidationError('No pudimos separar las filas válidas. Revisa los conflictos del archivo.')
    diagnostics = [item for issue in issues for item in issue['errors']]
    metadata.update({'errors': diagnostics[:100], 'error_count': len(diagnostics),
                     'rejected_row_count': len(issues), 'pending_error_count': len(issues)})
    if issues:
        result['summary']['rejected_rows'] = len(issues)
    result.update(metadata)
    result['valid'] = bool(accepted)
    return accepted, issues, result, metadata


def save_plan(groups):
    new_parts, changed_parts, new_codes = [], [], []
    for sku, group in groups.items():
        part = group['existing'] or Part(sku=sku)
        for field, value in group['fields'].items():
            setattr(part, field, value)
        if group['existing'] is None:
            new_parts.append(part)
        elif group['action'] == 'update':
            changed_parts.append(part)
        new_codes.extend(PartCode(part=part, brand=brand, code=code) for brand, code in group['added'])
    from .technical_data import bind_part_types
    bind_part_types(new_parts + changed_parts)
    Part.objects.bulk_create(new_parts, batch_size=500)
    Part.objects.bulk_update(changed_parts, ['name', 'description', 'active', 'category', 'subcategory', 'part_type'], batch_size=500)
    PartCode.objects.bulk_create(new_codes, batch_size=500)


def encode_groups(groups):
    return [{'sku': group['sku'], 'row': group['row'], 'fields': group['fields'],
             'codes': [{'brand': brand, 'code': code, 'row': row} for (brand, code), row in group['codes'].items()]}
            for group in groups.values()]


def decode_groups(data):
    return {item['sku']: {'sku': item['sku'], 'row': item['row'], 'fields': dict(item['fields']),
                          'codes': {(code['brand'], code['code']): code['row'] for code in item['codes']}}
            for item in data}


def load_job_groups(job):
    groups = {}
    for data in job.batches.order_by('index').values_list('data', flat=True).iterator():
        groups.update(decode_groups(data))
    return groups


def job_result(job):
    result = dict(job.preview)
    result.pop('_file_hash', None)
    result.setdefault('warnings', [])
    result.setdefault('warning_count', 0)
    result.update({'job_id': str(job.pk), 'status': job.status, 'filename': job.filename,
                   'imported': job.status == 'completed', 'summary': job.summary,
                   'applied_summary': job.applied_summary,
                   'progress': {'total_batches': job.total_batches, 'completed_batches': job.completed_batches,
                                'total_skus': job.total_skus, 'processed_skus': job.processed_skus,
                                'batch_size': BATCH_SIZE}})
    result['pending_error_count'] = job.issues.filter(status='pending').count()
    return result


@transaction.atomic
def replace_job_groups(job, groups, *, row_count=None, warnings=None, review_metadata=None):
    if job.completed_batches or job.status == 'completed':
        raise ValidationError('La importación ya comenzó. Crea una nueva vista previa para cambiar sus datos.')
    row_count = row_count if row_count is not None else job.summary.get('rows', len(groups))
    result, _ = build_plan(groups, [], row_count, warnings=warnings if warnings is not None else job.preview.get('warnings', []))
    if not result['valid']:
        raise ValidationError({'detail': 'Las asignaciones tienen conflictos. Corrígelas antes de continuar.', 'errors': result['errors']})
    if warnings is None:
        result['warning_count'] = job.preview.get('warning_count', result['warning_count'])
    metadata_keys = ('recovery_rows', 'recovery_count', 'source_rows', 'skipped_rows', 'skipped_row_count', 'numeric_warning_count', 'date_recovery_count', 'errors', 'error_count', 'rejected_row_count', 'pending_error_count', '_file_hash')
    metadata = review_metadata if review_metadata is not None else {key: job.preview[key] for key in metadata_keys if key in job.preview}
    result.update(metadata)
    if metadata.get('skipped_row_count'):
        result['summary']['skipped_rows'] = metadata['skipped_row_count']
    if metadata.get('rejected_row_count'):
        result['summary']['rejected_rows'] = metadata['rejected_row_count']
    batches = []
    items = list(groups.items())
    for offset in range(0, len(items), BATCH_SIZE):
        chunk = dict(items[offset:offset + BATCH_SIZE])
        _, fingerprint = build_plan(chunk, [], len(chunk))
        batches.append(CatalogImportBatch(job=job, index=len(batches), data=encode_groups(chunk), fingerprint=fingerprint))
    job.batches.all().delete()
    CatalogImportBatch.objects.bulk_create(batches, batch_size=100)
    job.summary = result['summary']
    job.applied_summary = {key: 0 for key in job.summary}
    if metadata.get('skipped_row_count'):
        job.applied_summary['skipped_rows'] = metadata['skipped_row_count']
    if metadata.get('rejected_row_count'):
        job.applied_summary['rejected_rows'] = metadata['rejected_row_count']
    job.total_skus = len(groups)
    job.total_batches = len(batches)
    job.processed_skus = job.completed_batches = 0
    job.status = 'ready' if groups else 'review'
    result['valid'] = bool(groups)
    job.preview = result
    job.save()
    return job_result(job)


class BatchRequest(serializers.Serializer):
    batch_index = serializers.IntegerField(min_value=0)


class CatalogImportJobView(APIView):
    permission_classes = [IsSuperuser]

    def get_job(self, request, pk, *, lock=False):
        jobs = CatalogImportJob.objects.filter(owner=request.user)
        if lock:
            jobs = jobs.select_for_update()
        job = get_object_or_404(jobs, pk=pk)
        if job.expires_at < timezone.now() and job.status != 'review':
            raise StalePreview('Esta importación venció. Sube el archivo y revisa una nueva vista previa.')
        return job

    @extend_schema(responses=ImportResponse)
    def get(self, request, pk):
        return Response(job_result(self.get_job(request, pk)))

    @extend_schema(request=BatchRequest, responses=ImportResponse)
    def post(self, request, pk):
        serializer = BatchRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        index = serializer.validated_data['batch_index']
        return self.commit(request, pk, index)

    def commit(self, request, pk, index):
        try:
            with transaction.atomic():
                job = self.get_job(request, pk, lock=True)
                if index < job.completed_batches:
                    # A lost response can safely be retried without committing
                    # the following batch by accident.
                    return Response(job_result(job))
                if index != job.completed_batches or index >= job.total_batches:
                    raise StalePreview('El número de lote cambió. Recupera el progreso antes de continuar.')
                if CatalogClassificationState.objects.filter(job=job, claim_id__isnull=False, claimed_at__gt=timezone.now() - timedelta(seconds=120)).exists():
                    raise StalePreview('Hay un lote de IA en curso. Espera a que termine y revisa sus propuestas antes de importar.')
                batch = job.batches.get(index=index)
                groups = decode_groups(batch.data)
                result, fingerprint = build_plan(groups, [], len(groups), lock=True)
                if fingerprint != batch.fingerprint or not result['valid']:
                    raise StalePreview('El catálogo cambió para este lote. Los lotes anteriores siguen guardados; revisa una nueva vista previa.')
                save_plan(groups)
                for key, value in result['summary'].items():
                    job.applied_summary[key] = job.applied_summary.get(key, 0) + value
                job.completed_batches += 1
                job.processed_skus += len(groups)
                job.status = 'completed' if job.completed_batches == job.total_batches else 'importing'
                if job.status == 'completed':
                    job.applied_summary['rows'] = job.summary['rows']
                    from .matching_queue import enqueue_matching
                    enqueue_matching()
                job.save(update_fields=['applied_summary', 'completed_batches', 'processed_skus', 'status'])
                return Response(job_result(job))
        except IntegrityError:
            raise StalePreview('No se guardó este lote porque hubo un cambio en el catálogo. Los lotes anteriores siguen guardados.')


class CatalogImport(APIView):
    permission_classes = [IsSuperuser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(request=ImportRequest, responses=ImportResponse)
    def post(self, request):
        serializer = ImportRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        file = data['file']
        if not file.name.lower().endswith('.xlsx') or file.size > MAX_BYTES:
            raise ValidationError('Selecciona un archivo .xlsx de hasta 5 MB.')
        raw = file.read()
        digest = hashlib.sha256(raw).hexdigest()
        if data['mode'] == 'preview':
            groups, errors, row_count, warnings, metadata, records = read_excel(raw, include_records=True, corrections=data['corrections'], skip_rows=data['skip_rows'])
            groups, issues, result, metadata = split_import_plan(groups, errors, records, metadata, warnings)
            metadata['_file_hash'] = digest
            with transaction.atomic():
                # Unresolved rows outlive the ordinary seven-day draft window.
                CatalogImportJob.objects.filter(owner=request.user, expires_at__lt=timezone.now()).exclude(issues__status='pending').exclude(pk=data.get('job_id')).delete()
                if data.get('job_id'):
                    job = get_object_or_404(CatalogImportJob.objects.select_for_update().filter(owner=request.user), pk=data['job_id'])
                    if job.completed_batches or job.status == 'completed' or job.issues.filter(status='resolved').exists() or job.preview.get('_file_hash') != digest:
                        raise StalePreview('Esta importación ya cambió o comenzó. Selecciona otro archivo para crear una nueva vista previa.')
                    CatalogClassificationState.objects.filter(job=job).delete()
                    job.classification_suggestions.all().delete()
                    job.issues.all().delete()
                    job.expires_at = timezone.now() + timedelta(days=7)
                else:
                    job = CatalogImportJob.objects.create(owner=request.user, filename=file.name[:255], expires_at=timezone.now() + timedelta(days=7))
                replace_job_groups(job, groups, row_count=result['summary']['rows'], warnings=warnings, review_metadata=metadata)
                CatalogImportIssue.objects.bulk_create([CatalogImportIssue(job=job, **issue) for issue in issues], batch_size=500)
            result.update(job_result(job))
            if result['valid']:
                result['preview_token'] = signing.dumps({'file': digest, 'job': str(job.pk), 'user': request.user.pk}, salt=SALT, compress=True)
            return Response(result)
        try:
            approved = signing.loads(data.get('preview_token', ''), salt=SALT, max_age=900)
        except signing.BadSignature:
            raise StalePreview()
        if approved.get('file') != digest or approved.get('user') != request.user.pk:
            raise StalePreview()
        if not approved.get('job'):
            raise StalePreview()
        # Older clients may still upload the file again to confirm it. Keep
        # confirmation compatible, but it only commits the first 500-SKU batch.
        # All remaining work uses the durable job endpoint.
        return CatalogImportJobView().commit(request, approved['job'], 0)
