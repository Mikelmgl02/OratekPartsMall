"""Supplier price import from Excel: PRECIO / PRECIO_<LISTA> columns of an ERP export, read as-is, previewed, then committed in batches.

Same protocol as the stock import (preview, owner-bound job, fingerprinted 1,000-row batches with committed-index checkpoints), but
separate tables, so a running price import never marks the supplier busy for matching. Every write goes through set_prices():
SupplierItem is never locked or saved, prices never create items, and columns such as COSTO are ignored and never stored.
Cells: blank keeps the current value, BORRAR removes it, 0 in a list column is skipped with a warning. Amounts are parsed as Decimal
only; ambiguous separators and more than two decimals are rejected, never rounded.
"""
import hashlib
import json
import math
import re
from collections import Counter
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Account, SupplierItem
from .price_import_models import PRICE_IMPORT_STATUSES, PriceImportBatch, PriceImportJob
from .pricing_models import (CURRENCY_CHOICES, PRICE_CHANGE_KINDS, PRICE_LIST_CODE, PriceList, PriceListEntry, SupplierItemPricing, choice_values,
                             pricing_settings, record_pricing_event)
from .pricing_services import PriceConflict, amount, item_values, set_prices
from .pricing_views import RESERVED_LIST_CODES, configurer
from .supplier_import import HEADERS as STOCK_HEADERS
from .views import membership_for
from .workbook_reader import (XLSX_TYPE, batches, cell_original, cell_value, header, identifier_text, job_progress, reject_date_or_boolean,
                              reject_formula, reject_repeated, workbook_response, workbook_rows)

MAX_BYTES = 5 * 1024 * 1024
BATCH_SIZE = 1000
MAX_COLUMNS = 40
LIMIT = 100
CENT = Decimal('0.01')
DELETE = 'BORRAR'
ID_COLUMN = 'ID_INVENTARIO_PROVEEDOR'
DEFAULT_LIST_CODE = 'GENERAL'
# The ID and CODIGO columns accept the stock import's aliases; MARCA and DESCRIPCION are ignored but kept in the correction workbook.
COLUMN_FIELDS = {**{name: field for name, field in STOCK_HEADERS.items() if field in ('supplier_invent_id', 'codigo')},
                 'PRECIO': 'price', 'LINEA': 'discount_group', 'FAMILIA': 'discount_group', 'GRUPO_DESCUENTO': 'discount_group',
                 'PRECIO_MINIMO': 'floor_price', 'PRECIO_PISO': 'floor_price', 'ERRORES': 'skip', 'ERRORS': 'skip'}
CONTEXT_COLUMNS = {'MARCA', 'BRAND', 'DESCRIPCION', 'DESCRIPTION'}
# ERP exports often carry PRECIO_COSTO or PRECIO_COMPRA next to sale prices: supplier cost is ignored like COSTO, never a new list.
COST_CODE = re.compile(r'(COSTO?|COSTE|COMPRA)(_.*)?')
AMOUNT_PREFIX, AMOUNT_SUFFIX = re.compile(r'^(?:\$|B/\.|USD|PAB)\s*'), re.compile(r'\s*(?:USD|PAB)$')
INVALID_AMOUNT = 'Escribe un importe válido, por ejemplo 12.50.'
STALE = 'Los precios cambiaron desde la revisión; vuelve a subir el archivo.'
COUNTS = ('new_prices', 'increases', 'decreases', 'unchanged', 'removed')
APPLIED = ('created', 'updated', 'removed', 'unchanged', 'floor_updates', 'line_updates')


class StalePricePreview(APIException):
    status_code = 409
    default_detail = STALE


class ConfirmationRequired(APIException):
    status_code = 400
    default_detail = 'Confirma los cambios de la revisión antes de aplicarlos.'


def parse_amount(cell):
    """Decimal (two places), DELETE, or None for a blank cell (no change). Raises ValueError with a Spanish message."""
    reject_formula(cell)
    value = cell.value
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    reject_date_or_boolean(cell, 'Escribe el importe como número, sin fechas ni valores SI/NO.')
    if isinstance(value, str):
        text = AMOUNT_SUFFIX.sub('', AMOUNT_PREFIX.sub('', value.strip().upper(), count=1), count=1).strip()
        if text == DELETE:
            return DELETE
        number = amount_text(text)
    elif isinstance(value, (int, float)) and not (isinstance(value, float) and not math.isfinite(value)):
        # Excel keeps 15 significant digits: a pasted 11.499999999999998 is the 11.50 it shows, while a real third decimal stays and is rejected.
        number = Decimal(format(value, '.15g') if isinstance(value, float) else str(value))
        if number < 0:
            raise ValueError('El importe no puede ser negativo.')
    else:
        raise ValueError(INVALID_AMOUNT)
    if number >= Decimal('1e10'):
        raise ValueError('Usa como máximo 10 dígitos enteros.')
    if number != number.quantize(CENT):
        raise ValueError('Usa un máximo de dos decimales.')
    return number.quantize(CENT)


def amount_text(text):
    """1234 · 12.50 · 12,50 (one separator, 1-2 decimals) · 1,234.50 · 1.234,50 · 1,234,567. A lone separator followed by three digits is
    ambiguous (12,500 could be 12500 or 12.50) and is rejected."""
    if text.startswith(('-', '(')) or text.endswith('-'):
        raise ValueError('El importe no puede ser negativo.')
    if re.fullmatch(r'\d+', text):
        return Decimal(text)
    if match := re.fullmatch(r'(\d+)[.,](\d+)', text):
        whole, fraction = match.groups()
        if len(fraction) == 3:
            raise ValueError(f'Formato ambiguo: escribe {whole}{fraction} o {whole}.{fraction[:2]}')
        if len(fraction) > 2:
            raise ValueError('Usa un máximo de dos decimales.')
        return Decimal(f'{whole}.{fraction}')
    for group, point in ((',', '.'), ('.', ',')):
        if match := re.fullmatch(rf'(\d{{1,3}}(?:{re.escape(group)}\d{{3}})+)(?:{re.escape(point)}(\d+))?', text):
            whole, fraction = match.group(1).replace(group, ''), match.group(2) or ''
            if len(fraction) > 2:
                raise ValueError('Usa un máximo de dos decimales.')
            return Decimal(f'{whole}.{fraction}' if fraction else whole)
    raise ValueError(INVALID_AMOUNT)


def parse_line(cell):
    """The supplier's own LINEA: '' (BORRAR) removes it, None keeps it."""
    reject_formula(cell)
    value = cell.value
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    reject_date_or_boolean(cell)
    text = (value if isinstance(value, str) else identifier_text(cell)).strip().upper()
    if text == DELETE:
        return ''
    if len(text) > 60:
        raise ValueError('Usa una línea de hasta 60 caracteres.')
    return text


def read_columns(first, existing=()):
    columns, ignored, seen = [], [], {}
    for index, cell in enumerate(first):
        if cell.value is None or not str(cell.value).strip():
            continue
        name = header(cell.value)
        field, code = COLUMN_FIELDS.get(name), None
        # A supplier list whose code looks like a cost column (COSTO, COMPRA_…) is still a list: its export must round-trip.
        if field is None and name.startswith('PRECIO_') and (name[7:] in existing or not COST_CODE.fullmatch(name[7:])):
            field, code = 'price', name[7:]
            if not re.fullmatch(PRICE_LIST_CODE, code) or code in RESERVED_LIST_CODES:
                raise ValidationError(f'El encabezado {name} no corresponde a una lista: después de PRECIO_ usa el código de la lista '
                                      '(de 1 a 30 letras sin tilde, números o guiones bajos).')
        elif field == 'price':
            code = ''
        if field == 'skip':
            continue
        if field is None:
            if name not in ignored:
                ignored.append(name)
            if name in CONTEXT_COLUMNS and name not in seen:
                seen[name] = index
                columns.append({'index': index, 'header': name, 'field': 'context', 'code': None})
            continue
        key = (field, code) if field == 'price' else field
        if key in seen:
            raise ValidationError(f'La columna {name} está repetida. Usa cada encabezado una sola vez.')
        seen[key] = index
        columns.append({'index': index, 'header': name, 'field': field, 'code': code})
    fields = {column['field'] for column in columns}
    if 'supplier_invent_id' not in fields:
        raise ValidationError(f'Se requiere {ID_COLUMN} en la primera fila.')
    if not fields & {'price', 'discount_group', 'floor_price'}:
        raise ValidationError('No encontramos columnas de precio. Usa PRECIO, PRECIO_<LISTA>, LINEA o PRECIO_MINIMO.')
    return columns, ignored


def read_price_excel(raw, existing=()):
    """(columns, ignored column names, records, warnings). Records keep each cell's original value for the correction workbook.
    `existing`: the supplier's list codes, so a PRECIO_<CODE> column of an existing list is never taken for a cost column."""
    with workbook_rows(raw, sheets=['PRECIOS', 'LISTA_PRECIOS'], max_columns=MAX_COLUMNS,
                       too_wide=f'Usa como máximo {MAX_COLUMNS} columnas en el archivo de precios.') as rows:
        first = next(rows, None)
        if not first:
            raise ValidationError('El archivo no contiene encabezados de precios.')
        columns, ignored = read_columns(first, existing)
        records, warnings = [], []
        for row_number, cells in enumerate(rows, 2):
            if not any(cell.value is not None for cell in cells):
                continue
            record = {'row': row_number, 'values': {}, 'original': {}, 'errors': [], 'data': {'prices': {}}}
            for column in columns:
                cell, name, field = cells[column['index']], column['header'], column['field']
                record['values'][name], record['original'][name] = cell_value(cell), cell_original(cell)
                if field == 'context':
                    continue
                try:
                    if field == 'supplier_invent_id':
                        reject_formula(cell)
                        if cell.value is None or cell.value == '':
                            raise ValueError('Ingresa el identificador de esta fila.')
                        value = identifier_text(cell).strip()
                        if not value or len(value) > 120:
                            raise ValueError('Ingresa un identificador de hasta 120 caracteres.')
                        if not isinstance(cell.value, str):
                            warnings.append({'row': row_number, 'column': name, 'message': f'Identificador numérico convertido a texto: {value}.'})
                        record['data'][field] = record['values'][name] = value
                    elif field == 'codigo':
                        # Only a check: an unreadable or different code never blocks the row, which is keyed by its ID.
                        try:
                            reject_formula(cell)
                            value = identifier_text(cell).strip().upper() if cell.value not in (None, '') else ''
                        except ValueError:
                            warnings.append({'row': row_number, 'column': name, 'message': 'No pudimos leer el CODIGO; la fila se identifica por su ID.'})
                            value = ''
                        if value:
                            record['data'][field] = value
                    elif field == 'discount_group':
                        value = parse_line(cell)
                        if value is not None:
                            record['data'][field], record['values'][name] = value, value or DELETE
                    else:
                        value = parse_amount(cell)
                        if value is None:
                            continue
                        # The correction workbook keeps what was read (11.50), never a float artifact that would fail when uploaded again.
                        record['values'][name] = value if value == DELETE else f'{value:.2f}'
                        if field == 'price' and value == 0:
                            warnings.append({'row': row_number, 'column': name, 'message': 'Precio cero ignorado; deja la celda vacía o escribe BORRAR.'})
                            continue
                        stored = None if value == DELETE else f'{value:.2f}'
                        if field == 'price':
                            record['data']['prices'][name] = stored
                        else:
                            record['data'][field] = stored
                except ValueError as exc:
                    record['errors'].append({'row': row_number, 'column': name, 'message': str(exc)})
            records.append(record)
        if not records:
            raise ValidationError('El archivo no contiene filas de precios.')
        id_column = next(column['header'] for column in columns if column['field'] == 'supplier_invent_id')
        reject_repeated(records, 'supplier_invent_id', id_column, 'El ID {identifier} está repetido en el archivo. Conserva una sola fila por artículo.')
        return columns, ignored, records, warnings


def resolve_lists(supplier, columns):
    """Map each price column to a list code. PRECIO is the default list; PRECIO_<CODE> the list with that code, created on commit if
    it does not exist yet. Returns ({header: code}, [lists to create], warnings)."""
    lists = {row.code: row for row in PriceList.objects.filter(supplier=supplier)}
    default = next((row for row in lists.values() if row.is_default), None)
    currency, targets, used, warnings = pricing_settings(supplier).default_currency, {}, {}, []
    for column in columns:
        if column['field'] != 'price':
            continue
        code = column['code'] or (default.code if default else DEFAULT_LIST_CODE)
        if code in used:
            raise ValidationError(f'Las columnas {used[code]} y {column["header"]} corresponden a la misma lista {code}. Deja solo una.')
        targets[column['header']], used[code] = code, column['header']
        if code in lists and not lists[code].active:
            warnings.append({'row': 1, 'column': column['header'], 'message': f'La lista {code} está archivada: sus precios se guardan, pero no se sugieren en cotizaciones.'})
    # The first list a supplier creates is its default: PRECIO's list, otherwise the first new column of the file.
    creating = [{'code': code, 'name': code, 'currency': currency, 'is_default': False, 'header': name} for code, name in used.items() if code not in lists]
    return targets, creating, warnings


def keyed_by_code(records, targets):
    for record in records:
        record['data']['prices'] = {targets[name]: value for name, value in record['data']['prices'].items()}


def price_plan(supplier, rows, codes, headers, *, lock=False):
    """Preview of rows [{row, data}] against the current prices of `codes`, and a fingerprint of everything the plan relied on: item id
    and supplier ID, each list entry's revision (or null) and the item-pricing revision (or null). SupplierItem.updated_at is excluded,
    so stock syncs never make a price batch stale. With lock, the touched lists, entries and item-pricing rows are row-locked in the
    same order as set_prices() (the caller holds the supplier Account lock first)."""
    identifiers = sorted({row['data']['supplier_invent_id'] for row in rows})
    items = {}
    for offset in range(0, len(identifiers), 2000):
        items.update((item.supplier_invent_id, item) for item in SupplierItem.objects.filter(
            supplier=supplier, supplier_invent_id__in=identifiers[offset:offset + 2000]).only('id', 'supplier_invent_id', 'codigo', 'description'))
    lists = PriceList.objects.filter(supplier=supplier, code__in=list(codes)).order_by('pk')
    lists = {row.code: row for row in (lists.select_for_update() if lock else lists)}
    ids, entries, pricing = sorted(item.pk for item in items.values()), {}, {}
    for offset in range(0, len(ids), 2000):
        query = PriceListEntry.objects.filter(price_list__in=list(lists.values()), item_id__in=ids[offset:offset + 2000]).order_by('item_id', 'price_list_id')
        entries.update(((entry.price_list_id, entry.item_id), entry) for entry in (query.select_for_update() if lock else query))
    for offset in range(0, len(ids), 2000):
        query = SupplierItemPricing.objects.filter(supplier=supplier, item_id__in=ids[offset:offset + 2000]).order_by('item_id')
        pricing.update((row.item_id, row) for row in (query.select_for_update() if lock else query))
    summary, per_list = Counter(), {code: Counter() for code in codes}
    preview, errors, warnings, staged = [], [], [], []
    for row in rows:
        data, number = row['data'], row['row']
        item = items.get(data['supplier_invent_id'])
        if not item:
            errors.append({'row': number, 'column': headers['id'], 'message': 'Este ID no existe en tu inventario. Carga primero sus existencias.'})
            continue
        summary['valid_rows'] += 1
        if data.get('codigo') and data['codigo'] != item.codigo:
            warnings.append({'row': number, 'column': headers['codigo'],
                             'message': f'El CODIGO del archivo ({data["codigo"]}) no coincide con tu inventario ({item.codigo}); se usó el ID.'})
        base = {'row': number, 'supplier_invent_id': item.supplier_invent_id, 'codigo': item.codigo, 'description': item.description}
        changes = []
        for code, value in data['prices'].items():
            entry = entries.get((lists[code].pk, item.pk)) if code in lists else None
            current, new = entry.unit_price if entry else None, None if value is None else Decimal(value)
            if current == new:
                summary['unchanged'] += 1
                per_list[code]['unchanged'] += 1
                continue
            action = 'create' if current is None else 'delete' if new is None else 'update'
            kind = {'create': 'new_prices', 'delete': 'removed'}.get(action) or ('increases' if new > current else 'decreases')
            summary[kind] += 1
            per_list[code][kind] += 1
            percent = ((new - current) * 100 / current).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if action == 'update' else None
            big = action == 'update' and abs(new - current) * 2 > current
            if big:
                summary['big_changes'] += 1
                warnings.append({'row': number, 'column': headers[code], 'message': f'Variación mayor al 50 % ({percent:+} %): {amount(current)} → {amount(new)}.'})
            changes.append({**base, 'kind': 'list_price', 'list': code, 'current': amount(current), 'new': amount(new),
                            'change_percent': None if percent is None else str(percent), 'action': action, 'big_change': big})
        before = item_values(pricing.get(item.pk))
        for field, label in (('discount_group', 'line_updates'), ('floor_price', 'floor_updates')):
            if field not in data:
                continue
            new = data[field] if field == 'discount_group' else None if data[field] is None else Decimal(data[field])
            if new == before[field]:
                continue
            summary[label] += 1
            show = (lambda value: value) if field == 'discount_group' else amount
            changes.append({**base, 'kind': field, 'list': None, 'current': show(before[field]) or None, 'new': show(new) or None,
                            'change_percent': None, 'action': 'create' if before[field] in (None, '') else 'delete' if new in (None, '') else 'update',
                            'big_change': False})
        if changes:
            preview.extend(changes)
            staged.append({'row': number, 'data': data})
    snapshot = sorted((str(item.pk), item.supplier_invent_id, [entries[(lists[code].pk, item.pk)].revision if code in lists and (lists[code].pk, item.pk) in entries
                                                                else None for code in sorted(codes)],
                       pricing[item.pk].revision if item.pk in pricing else None) for item in items.values())
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    preview.sort(key=lambda change: (not change['big_change'], change['row']))
    totals = {key: summary[key] for key in ('valid_rows', *COUNTS, 'big_changes', 'line_updates', 'floor_updates')}
    return {'summary': {**totals, 'lists': {code: {key: counts[key] for key in COUNTS} for code, counts in per_list.items()}},
            'preview': preview, 'errors': errors, 'warnings': warnings, 'staged': staged, 'lists': lists, 'entries': entries, 'pricing': pricing,
            'items': items}, fingerprint


def set_price_changes(plan, codes):
    """set_prices() arguments for a locked plan whose fingerprint matched: each row keeps the revisions the preview relied on."""
    changes, items = [], []
    lists, entries, pricing = plan['lists'], plan['entries'], plan['pricing']
    for row in plan['staged']:
        data, item = row['data'], plan['items'][row['data']['supplier_invent_id']]
        reference = f'import:{plan["job"]}:{row["row"]}'
        for code, value in data['prices'].items():
            # A list that does not exist (not created: no new price in the file) only received BORRAR, which is a no-op.
            if code in codes:
                entry = entries.get((lists[code].pk, item.pk)) if code in lists else None
                changes.append({'list_id': codes[code], 'supplier_item_id': item.pk, 'unit_price': None if value is None else Decimal(value),
                                'expected_revision': entry.revision if entry else None, 'reference': reference})
        fields = {field: (data[field] if field == 'discount_group' or data[field] is None else Decimal(data[field]))
                  for field in ('discount_group', 'floor_price') if field in data}
        if fields:
            current = pricing.get(item.pk)
            items.append({'supplier_item_id': item.pk, **fields, 'expected_revision': current.revision if current else None, 'reference': reference})
    return changes, items


def job_codes(job):
    return [column['code'] for column in job.preview.get('lists', [])]


def job_headers(job):
    return {'id': job.preview.get('id_column', ID_COLUMN), 'codigo': job.preview.get('code_column', 'CODIGO'),
            **{column['code']: column['header'] for column in job.preview.get('lists', [])}}


def job_result(job):
    return {**job.preview, 'job_id': str(job.pk), 'supplier_id': str(job.supplier_id), 'filename': job.filename, 'status': job.status,
            'expires_at': job.expires_at.isoformat(), 'valid': job.total_rows > 0, 'imported': job.status == 'completed',
            'summary': job.summary, 'applied_summary': job.applied_summary, 'lists_to_create': job.lists_to_create,
            'requires': {'acknowledge_big_changes': bool(job.summary.get('big_changes')) and not job.big_change_acknowledged,
                         'confirm_new_lists': bool(job.lists_to_create) and job.completed_batches == 0},
            'progress': job_progress(job, BATCH_SIZE)}


# --- OpenAPI shapes (the views return job_result() dictionaries with exactly these keys) -------------------------------------

class PriceImportRequest(serializers.Serializer):
    file = serializers.FileField()


class PriceImportBatchRequest(serializers.Serializer):
    batch_index = serializers.IntegerField(min_value=0)
    acknowledge_big_changes = serializers.BooleanField(required=False, default=False, help_text='Revisé los cambios mayores al 50 %.')
    confirm_new_lists = serializers.BooleanField(required=False, default=False, help_text='Crear las listas nuevas del archivo (lote 0).')


class PriceImportIssue(serializers.Serializer):
    row = serializers.IntegerField()
    column = serializers.CharField()
    message = serializers.CharField()


class PriceImportChange(serializers.Serializer):
    row = serializers.IntegerField()
    supplier_invent_id = serializers.CharField()
    codigo = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    kind = serializers.ChoiceField(choices=choice_values(PRICE_CHANGE_KINDS))
    list = serializers.CharField(allow_null=True, help_text='Código de la lista; null para línea o precio mínimo.')
    current = serializers.CharField(allow_null=True)
    new = serializers.CharField(allow_null=True)
    change_percent = serializers.CharField(allow_null=True)
    action = serializers.ChoiceField(choices=['create', 'update', 'delete'])
    big_change = serializers.BooleanField()


class PriceImportCounts(serializers.Serializer):
    new_prices = serializers.IntegerField()
    increases = serializers.IntegerField()
    decreases = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    removed = serializers.IntegerField()


class PriceImportSummary(PriceImportCounts):
    source_rows = serializers.IntegerField()
    valid_rows = serializers.IntegerField()
    rejected_rows = serializers.IntegerField()
    big_changes = serializers.IntegerField()
    line_updates = serializers.IntegerField()
    floor_updates = serializers.IntegerField()
    lists = serializers.DictField(child=PriceImportCounts(), help_text='Desglose por código de lista.')


class PriceImportApplied(serializers.Serializer):
    created = serializers.IntegerField()
    updated = serializers.IntegerField()
    removed = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    floor_updates = serializers.IntegerField()
    line_updates = serializers.IntegerField()


class PriceImportList(serializers.Serializer):
    header = serializers.CharField()
    code = serializers.CharField()
    new = serializers.BooleanField(help_text='La lista se creará al aplicar.')
    archived = serializers.BooleanField()
    skipped = serializers.BooleanField(help_text='La lista no existe y la columna no trae precios nuevos: no se creará.')


class PriceImportNewList(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()
    currency = serializers.ChoiceField(choices=choice_values(CURRENCY_CHOICES))
    is_default = serializers.BooleanField()
    header = serializers.CharField()


class PriceImportRequirements(serializers.Serializer):
    acknowledge_big_changes = serializers.BooleanField()
    confirm_new_lists = serializers.BooleanField()


class PriceImportProgress(serializers.Serializer):
    batch_size = serializers.IntegerField()
    total_batches = serializers.IntegerField()
    completed_batches = serializers.IntegerField()
    total_rows = serializers.IntegerField()
    processed_rows = serializers.IntegerField()
    next_batch = serializers.IntegerField(allow_null=True)


class PriceImportResult(serializers.Serializer):
    job_id = serializers.UUIDField()
    supplier_id = serializers.UUIDField()
    filename = serializers.CharField()
    status = serializers.ChoiceField(choices=choice_values(PRICE_IMPORT_STATUSES))
    expires_at = serializers.DateTimeField()
    valid = serializers.BooleanField(help_text='Hay cambios de precio para aplicar.')
    imported = serializers.BooleanField()
    summary = PriceImportSummary()
    applied_summary = PriceImportApplied()
    id_column = serializers.CharField()
    code_column = serializers.CharField()
    columns = serializers.ListField(child=serializers.CharField(), help_text='Columnas del archivo que conserva el libro de correcciones.')
    lists = PriceImportList(many=True)
    lists_to_create = PriceImportNewList(many=True)
    ignored_columns = serializers.ListField(child=serializers.CharField())
    preview = PriceImportChange(many=True)
    preview_count = serializers.IntegerField()
    errors = PriceImportIssue(many=True)
    error_count = serializers.IntegerField()
    warnings = PriceImportIssue(many=True)
    warning_count = serializers.IntegerField()
    requires = PriceImportRequirements()
    progress = PriceImportProgress()


# --- Views ---------------------------------------------------------------------------------------------------------------------

TEMPLATE_HINT = 'REEMPLAZA ESTA FILA CON TUS DATOS'


class PriceImportTemplate(APIView):
    @extend_schema(operation_id='v1_accounts_prices_import_template', responses={(200, XLSX_TYPE): bytes},
                   description='Plantilla de precios con una fila de ejemplo: PRECIO es tu lista predeterminada y PRECIO_<CÓDIGO> las demás.')
    def get(self, request, account_id):
        account, _ = membership_for(request.user, account_id, 'supplier')
        others = [row.code for row in PriceList.objects.filter(supplier=account, active=True, is_default=False).order_by('code')]
        columns = [(name, name) for name in (ID_COLUMN, 'CODIGO', 'MARCA', 'DESCRIPCION', 'LINEA', 'PRECIO_MINIMO', 'PRECIO', *[f'PRECIO_{code}' for code in others])]
        sample = {ID_COLUMN: '000123', 'CODIGO': '58411-1R000-KSM', 'MARCA': 'KSM', 'DESCRIPCION': TEMPLATE_HINT, 'LINEA': 'FRENOS',
                  'PRECIO_MINIMO': Decimal('12.50'), 'PRECIO': Decimal('15.75')}
        return workbook_response('PRECIOS', columns, [sample], 'plantilla-precios.xlsx', widths=[30, 26, 22, 60, 20] + [16] * (len(columns) - 5),
                                 numeric=[name for name, _ in columns if name.startswith('PRECIO')],
                                 render=lambda field, value: None if value == '' and field.startswith('PRECIO') else value)


class PriceImport(APIView):
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(operation_id='v1_accounts_prices_import_create', request=PriceImportRequest, responses={200: PriceImportResult},
                   description='Sube un .xlsx de precios y devuelve la vista previa. No cambia precios hasta aplicar sus lotes.')
    def post(self, request, account_id):
        supplier, _ = configurer(request.user, account_id)
        serializer = PriceImportRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        file = serializer.validated_data['file']
        if not file.name.lower().endswith('.xlsx') or file.size > MAX_BYTES:
            raise ValidationError('Selecciona un archivo .xlsx de hasta 5 MB.')
        columns, ignored, records, parse_warnings = read_price_excel(file.read(), set(PriceList.objects.filter(supplier=supplier).values_list('code', flat=True)))
        targets, creating, list_warnings = resolve_lists(supplier, columns)
        keyed_by_code(records, targets)
        codes = list(targets.values())
        headers = {'id': next(column['header'] for column in columns if column['field'] == 'supplier_invent_id'),
                   'codigo': next((column['header'] for column in columns if column['field'] == 'codigo'), 'CODIGO'),
                   **{code: name for name, code in targets.items()}}
        rows = [record for record in records if not record['errors']]
        plan, initial_fingerprint = price_plan(supplier, rows, codes, headers)
        extra_errors = {}
        for error in plan['errors']:
            extra_errors.setdefault(error['row'], []).append(error)
        for record in records:
            record['errors'].extend(extra_errors.get(record['row'], []))
        rejected = [record for record in records if record['errors']]
        errors = [error for record in rejected for error in record['errors']]
        warnings = list_warnings + parse_warnings + plan['warnings']
        # Only lists that receive a price are created; the first one a supplier gets becomes its default (PRECIO's list when present).
        lists_to_create = [spec for spec in creating if plan['summary']['lists'][spec['code']]['new_prices']]
        if lists_to_create and not PriceList.objects.filter(supplier=supplier).exists():
            default = next((spec for spec in lists_to_create if spec['header'] == 'PRECIO'), lists_to_create[0])
            default['is_default'] = True
        new_codes = {spec['code'] for spec in lists_to_create}
        existing = dict(PriceList.objects.filter(supplier=supplier, code__in=codes).values_list('code', 'active'))
        summary = {**plan['summary'], 'source_rows': len(records), 'rejected_rows': len(rejected)}
        preview = {'id_column': headers['id'], 'code_column': headers['codigo'],
                   'columns': [column['header'] for column in columns], 'ignored_columns': ignored,
                   'lists': [{'header': name, 'code': code, 'new': code in new_codes, 'archived': existing.get(code) is False,
                              'skipped': code not in existing and code not in new_codes} for name, code in targets.items()],
                   'preview': plan['preview'][:LIMIT], 'preview_count': len(plan['preview']), 'errors': errors[:LIMIT], 'error_count': len(errors),
                   'warnings': warnings[:LIMIT], 'warning_count': len(warnings)}
        staged = plan['staged']
        with transaction.atomic():
            # The same writer order as set_prices(), the stock import and ingest_inventory: the supplier Account first.
            Account.objects.select_for_update().get(pk=supplier.pk)
            if price_plan(supplier, rows, codes, headers, lock=True)[1] != initial_fingerprint:
                raise StalePricePreview('Los precios cambiaron mientras revisábamos el archivo. Súbelo otra vez.')
            job = PriceImportJob.objects.create(
                owner=request.user, supplier=supplier, filename=file.name[:255], status='ready' if staged else 'review',
                expires_at=timezone.now() + timedelta(days=7), summary=summary, preview=preview, lists_to_create=lists_to_create,
                rejected_rows=[{key: record[key] for key in ['row', 'values', 'original', 'errors']} for record in rejected],
                total_rows=len(staged), total_batches=(len(staged) + BATCH_SIZE - 1) // BATCH_SIZE, applied_summary=dict.fromkeys(APPLIED, 0))
            batch_rows = []
            for index, chunk in batches(staged, BATCH_SIZE):
                batch_plan, fingerprint = price_plan(supplier, chunk, codes, headers)
                if batch_plan['errors'] or len(batch_plan['staged']) != len(chunk):
                    raise StalePricePreview('Los precios cambiaron mientras revisábamos el archivo. Súbelo otra vez.')
                batch_rows.append(PriceImportBatch(job=job, index=index, data=chunk, fingerprint=fingerprint))
            PriceImportBatch.objects.bulk_create(batch_rows, batch_size=200)
        return Response(job_result(job))


def owned_job(supplier, user, pk, *, lock=False):
    """Jobs are bound to the member who uploaded them and to the supplier: anyone else gets 404."""
    query = PriceImportJob.objects.filter(owner=user, supplier=supplier)
    return get_object_or_404(query.select_for_update() if lock else query, pk=pk)


class PriceImportJobView(APIView):
    @extend_schema(operation_id='v1_accounts_prices_import_job_retrieve', responses={200: PriceImportResult},
                   description='Progreso de tu importación de precios. Solo la ve quien la subió.')
    def get(self, request, account_id, pk):
        return Response(job_result(owned_job(configurer(request.user, account_id)[0], request.user, pk)))

    @extend_schema(operation_id='v1_accounts_prices_import_job_commit', request=PriceImportBatchRequest, responses={200: PriceImportResult},
                   description='Aplica el siguiente lote. Repetir un lote ya guardado devuelve el progreso; un precio cambiado desde la vista previa devuelve 409.')
    def post(self, request, account_id, pk):
        serializer = PriceImportBatchRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        index = data['batch_index']
        supplier, _ = configurer(request.user, account_id)
        try:
            with transaction.atomic():
                # Account, then the job, then the price rows: the order of every supplier writer.
                Account.objects.select_for_update().get(pk=supplier.pk)
                job = owned_job(supplier, request.user, pk, lock=True)
                if index < job.completed_batches:
                    return Response(job_result(job))
                if job.expires_at < timezone.now():
                    raise StalePricePreview('Esta revisión venció. Sus errores siguen disponibles para descargar; vuelve a subir el archivo.')
                if index != job.completed_batches or index >= job.total_batches:
                    raise StalePricePreview('El número de lote cambió. Recupera el progreso antes de continuar.')
                if job.summary.get('big_changes') and not (job.big_change_acknowledged or data['acknowledge_big_changes']):
                    raise ConfirmationRequired('Confirma que revisaste los cambios mayores al 50 %.')
                if index == 0 and job.lists_to_create and not data['confirm_new_lists']:
                    raise ConfirmationRequired(f'Confirma la creación de las listas nuevas: {", ".join(spec["code"] for spec in job.lists_to_create)}.')
                codes, batch = job_codes(job), job.batches.get(index=index)
                plan, fingerprint = price_plan(supplier, batch.data, codes, job_headers(job), lock=True)
                if fingerprint != batch.fingerprint or plan['errors']:
                    raise StalePricePreview()
                if index == 0:
                    create_lists(supplier, request.user, job)
                plan['job'] = job.pk
                changes, items = set_price_changes(plan, dict(PriceList.objects.filter(supplier=supplier, code__in=codes).values_list('code', 'pk')))
                if changes or items:
                    result = set_prices(supplier, request.user, changes=changes, items=items, source='import', reference=f'import:{job.pk}',
                                        audit_kind='price_import_applied', audit={'object_id': str(job.pk), 'payload': {'batch': index, 'filename': job.filename}})
                    for key in APPLIED:
                        job.applied_summary[key] = job.applied_summary.get(key, 0) + result['summary'][key]
                job.big_change_acknowledged = job.big_change_acknowledged or data['acknowledge_big_changes']
                job.completed_batches += 1
                job.processed_rows += len(batch.data)
                job.status = 'completed' if job.completed_batches == job.total_batches else 'importing'
                job.save(update_fields=['applied_summary', 'big_change_acknowledged', 'completed_batches', 'processed_rows', 'status'])
                return Response(job_result(job))
        except (IntegrityError, PriceConflict):
            raise StalePricePreview('No se guardó este lote porque los precios cambiaron. Los lotes anteriores siguen guardados; vuelve a subir el archivo.')


def create_lists(supplier, actor, job):
    """Lists new to the supplier, created once: inside batch 0's transaction, so a failed or retried batch 0 never duplicates them."""
    current = PriceList.objects.filter(supplier=supplier)
    for spec in job.lists_to_create:
        if current.filter(code=spec['code']).exists() or (spec['is_default'] and current.filter(is_default=True).exists()):
            raise StalePricePreview()
        row = PriceList.objects.create(supplier=supplier, code=spec['code'], name=spec['name'], currency=spec['currency'], is_default=spec['is_default'],
                                       created_by=actor)
        record_pricing_event(supplier, actor, 'price_list_changed', object_id=str(row.pk), payload={
            'action': 'created', 'code': row.code, 'source': 'import', 'job': str(job.pk),
            'new': {'name': row.name, 'currency': row.currency, 'is_default': row.is_default}})


class PriceImportErrors(PriceImportJobView):
    http_method_names = ['get', 'head', 'options']

    @extend_schema(operation_id='v1_accounts_prices_import_errors', responses={(200, XLSX_TYPE): bytes},
                   description='Libro PRECIOS con las filas rechazadas y su columna ERRORES, todo como texto, listo para corregir y volver a subir.')
    def get(self, request, account_id, pk):
        job = owned_job(configurer(request.user, account_id)[0], request.user, pk)
        names = job.preview.get('columns') or [ID_COLUMN]
        return workbook_response('PRECIOS', [(name, name) for name in names], job.rejected_rows, 'errores-precios.xlsx', errors=True,
                                 widths=[30] + [20] * (len(names) - 1) + [80])
