"""Preview and import absolute supplier balances without altering stock identity."""
import hashlib
import json
import math
import re
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Account, Part, Reference, SupplierItem
from .serializers import InventorySerializer
from .services import ingest_inventory
from .stock_reservations import open_reservations, settle
from .supplier_import_models import SupplierInventoryImportBatch, SupplierInventoryImportJob
from .views import account_for
from .part_references import parse_reference_cell, format_references
from .workbook_reader import (XLSX_TYPE, batches, cell_original, cell_value, header, identifier_text, job_progress, reject_date_or_boolean,
                              reject_formula, reject_repeated, workbook_response, workbook_rows)

MAX_BYTES = 5 * 1024 * 1024
BATCH_SIZE = 500
MAX_QUANTITY = 2147483647
FIELDS = ['supplier_invent_id', 'codigo', 'brand', 'description', 'quantity', 'references']
COLUMNS = {'supplier_invent_id': 'ID_INVENTARIO_PROVEEDOR', 'codigo': 'CODIGO', 'brand': 'MARCA',
           'description': 'DESCRIPCION', 'quantity': 'EXISTENCIAS', 'references': 'REFERENCIAS'}
HEADERS = {
    'ID_INVENTARIO_PROVEEDOR': 'supplier_invent_id', 'SUPPLIER_INVENT_ID': 'supplier_invent_id',
    'SUPPLIERINVENTID': 'supplier_invent_id', 'INVENT_ID': 'supplier_invent_id',
    'INVENTID': 'supplier_invent_id', 'ID': 'supplier_invent_id',
    'CODIGO': 'codigo', 'CODE': 'codigo', 'CODIGO_PROVEEDOR': 'codigo', 'PART_NUMBER': 'codigo',
    'MARCA': 'brand', 'BRAND': 'brand', 'DESCRIPCION': 'description', 'DESCRIPTION': 'description',
    'EXISTENCIAS': 'quantity', 'CANTIDAD': 'quantity', 'QUANTITY': 'quantity', 'STOCK': 'quantity',
    'BALANCE': 'quantity', 'ERRORES': 'ignored', 'ERRORS': 'ignored',
    'REFERENCIAS': 'references', 'REFERENCES': 'references', 'ALTERNOS': 'references', 'CODIGOS_ALTERNOS': 'references',
    # Optional (rule 10, S3.2): what the supplier says about each article. Not in the template; a blank cell leaves the value as it was.
    'CALIDAD': 'calidad', 'QUALITY': 'calidad', 'CONDICION': 'condicion', 'CONDITION': 'condicion', 'GARANTIA': 'garantia',
    'WARRANTY': 'garantia', 'UNIDADES_POR_EMPAQUE': 'unidades_por_empaque', 'UNIDADES_EMPAQUE': 'unidades_por_empaque',
    'MATERIAL': 'material', 'ORIGEN': 'origen', 'PAIS_DE_ORIGEN': 'origen', 'PAIS_ORIGEN': 'origen',
}
OPTIONAL = {'calidad': 'CALIDAD', 'condicion': 'CONDICION', 'garantia': 'GARANTIA', 'unidades_por_empaque': 'UNIDADES_POR_EMPAQUE',
            'material': 'MATERIAL', 'origen': 'ORIGEN'}
COLUMNS.update(OPTIONAL)


class SupplierImportRequest(serializers.Serializer):
    file = serializers.FileField()


class SupplierBatchRequest(serializers.Serializer):
    batch_index = serializers.IntegerField(min_value=0)
    decisions = serializers.DictField(child=serializers.ChoiceField(choices=['net', 'gross']), required=False, default=dict,
                                      help_text='Por reserva de un pedido confirmado (su id): net, el saldo del archivo ya descuenta ese pedido (la reserva '
                                                'se liquida); gross, todavía lo incluye (la reserva sigue apartando sus unidades).')


SYNCED = ('Esta cuenta se sincroniza con apiag-cloud: sus existencias llegan de su sincronización, así que la carga Excel no está '
          'disponible.')
SYNCED_MANUAL = ('Esta cuenta se sincroniza con apiag-cloud: sus existencias llegan de su sincronización, así que no se actualizan a mano.')


def refuse_synced(supplier, message=SYNCED):
    """An account synced with apiag-cloud loads its stock from there only (no Excel import, no manual update): two sources would
    fight over the same counts."""
    from .apiag_models import ApiagConnection
    if ApiagConnection.objects.filter(account=supplier).exists():
        raise ValidationError({'detail': message})


class StaleSupplierPreview(APIException):
    status_code = 409
    default_detail = 'Las existencias o su vínculo cambiaron desde la vista previa. Revisa el archivo otra vez antes de importar.'


def _quantity(cell):
    value = cell.value
    if cell.is_date or isinstance(value, bool) or value is None or value == '':
        raise ValueError('Ingresa existencias como un número entero mayor o igual a cero. Una celda vacía no significa cero.')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('Ingresa un número entero válido para las existencias.')
    text = str(value).strip()
    if not re.fullmatch(r'\+?\d+(?:\.0+)?', text):
        raise ValueError('Las existencias deben ser unidades enteras, sin negativos, separadores ni decimales.')
    number = int(Decimal(text))
    if number > MAX_QUANTITY:
        raise ValueError('Las existencias superan el saldo máximo permitido.')
    return number


def read_supplier_excel(raw):
    with workbook_rows(raw, sheets=['EXISTENCIAS', 'INVENTARIO'], max_columns=20,
                       too_wide='Usa únicamente las columnas de la plantilla de existencias.') as rows:
        first = next(rows, None)
        if not first:
            raise ValidationError('El archivo no contiene encabezados de existencias.')
        columns = {}
        for index, cell in enumerate(first):
            if cell.value is None:
                continue
            key = HEADERS.get(header(cell.value))
            if not key or key in columns:
                raise ValidationError('Usa los encabezados de la plantilla, sin columnas desconocidas o repetidas.')
            columns[key] = index
        required = {'supplier_invent_id', 'codigo', 'quantity'}
        if not required.issubset(columns):
            raise ValidationError('Se requieren ID_INVENTARIO_PROVEEDOR, CODIGO y EXISTENCIAS en la primera fila.')
        records, warnings = [], []
        for row_number, cells in enumerate(rows, 2):
            if not any(cell.value is not None for cell in cells):
                continue
            record = {'row': row_number, 'values': dict.fromkeys(FIELDS, ''), 'original': {}, 'errors': [], 'data': {}}
            for field, index in columns.items():
                if field == 'ignored':
                    continue
                cell = cells[index]
                record['values'][field] = cell_value(cell)
                record['original'][field] = cell_original(cell)
                try:
                    reject_formula(cell)
                    if field in OPTIONAL and (cell.value is None or str(cell.value).strip() == ''):
                        continue  # a blank optional cell says nothing
                    if field == 'quantity':
                        value = _quantity(cell)
                    elif field == 'unidades_por_empaque':
                        reject_date_or_boolean(cell)
                        text = str(cell.value).strip()
                        if not re.fullmatch(r'\+?\d+(?:\.0+)?', text) or not 1 <= int(Decimal(text)) <= 32767:
                            raise ValueError('Las unidades por empaque son un número entero entre 1 y 32767.')
                        value = int(Decimal(text))
                    elif field in ['supplier_invent_id', 'codigo']:
                        if cell.value is None or cell.value == '':
                            raise ValueError('Ingresa el identificador de esta fila.')
                        value = identifier_text(cell).strip()
                        if field == 'codigo':
                            value = value.upper()
                        if not value or len(value) > 120:
                            raise ValueError('Ingresa un identificador de hasta 120 caracteres.')
                        if not isinstance(cell.value, str):
                            warnings.append({'row': row_number, 'column': COLUMNS[field], 'message': f'Identificador numérico convertido a texto: {value}.'})
                    else:
                        reject_date_or_boolean(cell)
                        value = str(cell.value if cell.value is not None else '').strip().upper()
                        if len(value) > (120 if field == 'brand' else 10000):
                            raise ValueError('El valor supera la longitud permitida.')
                        if field == 'references':
                            try:
                                value = parse_reference_cell(value)
                            except ValidationError as exc:
                                raise ValueError(' '.join(str(message) for message in exc.detail))
                    record['data'][field] = value
                    record['values'][field] = format_references(value) if field == 'references' else str(value)
                except ValueError as exc:
                    record['errors'].append({'row': row_number, 'column': COLUMNS[field], 'message': str(exc)})
            records.append(record)
        if not records:
            raise ValidationError('El archivo no contiene filas de existencias.')
        # Never add balances or choose one row arbitrarily, even when two copies look identical.
        reject_repeated(records, 'supplier_invent_id', COLUMNS['supplier_invent_id'],
                        'El ID {identifier} está repetido en el archivo. Conserva una sola fila por artículo del proveedor.')
        return records, warnings


def stock_plan(supplier, rows, *, lock=False):
    """Return the current plan and a snapshot of stock and relevant matching data. What each row does to its item is what the import
    will do (mall.reference_matching.plan_load, read-only: the commit runs the same rules), and the snapshot covers those outcomes, so
    a catalog change between the preview and a batch's commit is caught."""
    from . import reference_matching
    identifiers = sorted({row['data']['supplier_invent_id'] for row in rows})
    codes = sorted({row['data']['codigo'] for row in rows})
    items, parts = {}, {}
    for offset in range(0, len(identifiers), 2000):
        query = (SupplierItem.objects.filter(supplier=supplier, supplier_invent_id__in=identifiers[offset:offset + 2000])
                 .select_related('part', 'reference').order_by('pk'))
        items.update((item.supplier_invent_id, item) for item in query)
    # Every live Reference the rows' codes (and their bases once TAGs are stripped) may join, read at once, and their lines.
    lookups = reference_matching.Lookups().prefetch(codes)
    aliases = sorted({ref.pk: ref for rows_ in lookups.claves.values() for ref in rows_}.values(), key=lambda ref: ref.pk)
    for ref in aliases:
        parts[ref.part_id] = ref.part
    for item in items.values():
        if item.part_id:
            parts[item.part_id] = item.part
    if lock:
        # Reviewed SKU merging locks catalog parts, codes, then supplier rows.
        # Follow that same order, with globally sorted IDs across SQL chunks.
        part_ids = sorted(parts)
        for offset in range(0, len(part_ids), 2000):
            parts.update((part.pk, part) for part in Part.objects.select_for_update().filter(
                pk__in=part_ids[offset:offset + 2000]).order_by('pk'))
        alias_ids = sorted(alias.pk for alias in aliases)
        for offset in range(0, len(alias_ids), 2000):
            list(Reference.all_objects.select_for_update().filter(pk__in=alias_ids[offset:offset + 2000]).order_by('pk'))
        # Supplier rows in global pk order too, the order a deal confirmation locks them in. Callers hold the supplier's account lock,
        # which every item creator takes, so no row can appear between the read above and these locks.
        item_ids = sorted(item.pk for item in items.values())
        for offset in range(0, len(item_ids), 2000):
            items.update((item.supplier_invent_id, item) for item in SupplierItem.objects.select_for_update(of=('self',)).filter(
                pk__in=item_ids[offset:offset + 2000]).select_related('part', 'reference').order_by('pk'))
    held = open_reservations(item.pk for item in items.values())  # units confirmed deals still hold (mall.stock_reservations)
    outcomes = []
    summary = {'valid_rows': 0, 'created_items': 0, 'updated_items': 0, 'unchanged_items': 0, 'credit_units': 0, 'debit_units': 0, 'reserved_rows': 0}
    preview, errors, warnings, reservations = [], [], [], []
    for row in rows:
        data = dict(row['data'])
        item = items.get(data['supplier_invent_id'])
        if item and item.source != 'upload':
            errors.append({'row': row['row'], 'column': COLUMNS['supplier_invent_id'],
                           'message': 'Este ID se sincroniza con apiag-cloud. Sus existencias deben actualizarse desde el ERP.'})
            continue
        data.setdefault('brand', item.brand if item else '')
        data.setdefault('description', item.description if item else '')
        data.setdefault('references', item.references if item else [])
        validated = InventorySerializer(data={**data, 'source': 'upload', 'update_id': f'excel-preview-{row["row"]}'})
        if not validated.is_valid():
            errors.extend({'row': row['row'], 'column': COLUMNS.get(field, field), 'message': str(message)}
                          for field, messages in validated.errors.items() for message in messages)
            continue
        data = {field: value for field, value in validated.validated_data.items() if field not in ['source', 'update_id']}
        row['data'] = data
        current = item.reported_quantity if item else 0
        difference = data['quantity'] - current
        direction = 'credit' if difference > 0 else 'debit' if difference < 0 else 'none'
        # What the import will do (mall.reference_matching.plan_load, read-only): an item without a Reference joins its line's, gets
        # one, waits in a line case or becomes a new product; an attached item's code change that keeps it on its line applies on its
        # own, any other waits in codigo_pendiente, out of stock, while the code stays.
        if item is not None:
            item.supplier = supplier
        load = reference_matching.plan_load(item, data, lookups=lookups, supplier=supplier)
        plan, resolution = load.plan, load.resolution
        part_id, status = plan.part_id, plan.status
        action_taken = resolution.action if resolution is not None else plan.rule
        if resolution is not None and resolution.line is not None:
            parts.setdefault(resolution.line.pk, resolution.line)
        outcomes.append((row['row'], *outcome_key(action_taken, parts.get(part_id), status), plan.pending[0] or '', plan.pending[1] or ''))
        compared = ['codigo', 'brand', 'description', 'references', *(field for field in OPTIONAL if field in data)]
        action = 'create' if not item else 'update' if any(getattr(item, field) != data[field] for field in compared) or difference else 'unchanged'
        summary['valid_rows'] += 1
        summary[{'create': 'created_items', 'update': 'updated_items', 'unchanged': 'unchanged_items'}[action]] += 1
        if direction != 'none':
            summary[f'{direction}_units'] += abs(difference)
        part = parts.get(part_id)
        orders = held.get(item.pk, []) if item else []
        reserved = sum(reservation.quantity for reservation in orders)
        preview.append({'row': row['row'], **data, 'current_quantity': current, 'uploaded_quantity': data['quantity'],
                        'direction': direction, 'movement_quantity': abs(difference), 'new_quantity': data['quantity'],
                        'matching_status': status, 'part_id': str(part_id) if part_id else None,
                        'part_sku': part.sku if part else None, 'action': action, 'reserved': reserved,
                        'codigo_pendiente': plan.pending[0], 'brand_pendiente': plan.pending[1],
                        'resolucion': action_taken, 'regla': resolution.rule if resolution is not None else plan.rule})
        if reserved:
            # Confirmed deals hold units of this item: the supplier says whether the new count already discounts them.
            summary['reserved_rows'] += 1
            reservations.append({'row': row['row'], 'supplier_invent_id': data['supplier_invent_id'], 'codigo': data['codigo'],
                                 'description': data['description'], 'current_quantity': current, 'uploaded_quantity': data['quantity'],
                                 'reserved': reserved, 'reserved_quantity': item.reserved_quantity,
                                 'orders': [{'id': str(reservation.pk), 'reference': reservation.order.reference, 'client_name': reservation.order.client_name,
                                             'quantity': reservation.quantity, 'confirmed_at': reservation.created_at.isoformat(),
                                             # An order already sent (En camino) or delivered most likely left the file's count already: the
                                             # panel suggests "Ya lo descuenta".
                                             'in_transit_at': reservation.order.in_transit_at.isoformat() if reservation.order.in_transit_at else None,
                                             'delivered_at': reservation.order.delivered_at.isoformat() if reservation.order.delivered_at else None}
                                            for reservation in orders]})
        if plan.rule == 'pendiente':
            warnings.append({'row': row['row'], 'column': COLUMNS['codigo'], 'message':
                             f'El código cambió de {item.codigo} a {data["codigo"]}: el cambio queda pendiente de revisión y el artículo sale '
                             'del catálogo de clientes hasta que se apruebe.'})
        elif action_taken == 'no_line':
            warnings.append({'row': row['row'], 'column': COLUMNS['codigo'], 'message':
                             'Ningún código lo vincula a un SKU: pasa a Productos nuevos y aparecerá a los clientes cuando se publique.'})
        elif action_taken == 'case':
            warnings.append({'row': row['row'], 'column': COLUMNS['codigo'], 'message':
                             'El código está en varios SKU: queda en revisión hasta que se elija el SKU correcto.'})
        elif status == 'review':
            warnings.append({'row': row['row'], 'column': COLUMNS['codigo'], 'message':
                             'La descripción contradice la del SKU de su código: queda en revisión antes de aparecer en el catálogo de clientes.'})
        elif status != 'matched':
            warnings.append({'row': row['row'], 'column': COLUMNS['codigo'], 'message':
                             'Su SKU aún no está publicado: aparecerá en el catálogo de clientes cuando se publique.'})
    snapshot = {
        'items': sorted((str(item.pk), item.supplier_invent_id, item.codigo, item.brand, item.description,
                         item.source, item.reported_quantity, item.reserved_quantity, str(item.part_id),
                         item.matching_status, item.updated_at.isoformat(), item.references, item.reference_id,
                         item.codigo_pendiente, item.brand_pendiente) for item in items.values()),
        # S6: what the rows will do (each one's line and status) instead of the raw lines and codes they read: an earlier batch of the
        # same job creates new products and Rule 3 codes, which change those rows without changing any outcome that matters.
        'outcomes': sorted(outcomes),
        'reservations': sorted((str(reservation.pk), reservation.quantity) for orders in held.values() for reservation in orders),
    }
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return {'summary': summary, 'preview': preview, 'errors': errors, 'warnings': warnings, 'reservations': reservations}, fingerprint


def job_result(job):
    return {**job.preview, 'job_id': str(job.pk), 'supplier_id': str(job.supplier_id), 'filename': job.filename,
            'status': job.status, 'expires_at': job.expires_at.isoformat(), 'valid': job.total_rows > 0,
            'imported': job.status == 'completed', 'summary': job.summary, 'applied_summary': job.applied_summary,
            'progress': job_progress(job, BATCH_SIZE)}


def _workbook_response(rows, filename, *, errors=False):
    return workbook_response('EXISTENCIAS', [(field, COLUMNS[field]) for field in FIELDS], rows, filename, errors=errors,
                             numeric=['quantity'], widths=[30, 26, 22, 60, 18, 65, 80],
                             render=lambda field, value: format_references(value) if field == 'references' and isinstance(value, list) else value)


class SupplierInventoryImportTemplate(APIView):
    @extend_schema(responses={(200, XLSX_TYPE): bytes})
    def get(self, request, account_id):
        refuse_synced(account_for(request.user, account_id, 'supplier'))
        return _workbook_response([{'supplier_invent_id': '000123', 'codigo': '58411-1R000-KSM',
                                    'brand': 'KSM', 'description': 'REEMPLAZA ESTA FILA CON TUS DATOS', 'quantity': 5,
                                    'references': 'HYUNDAI:58411-1R000; 58411-1R000'}],
                                  'existencias-proveedor.xlsx')


class SupplierInventoryImport(APIView):
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(request=SupplierImportRequest, responses=OpenApiTypes.OBJECT)
    def post(self, request, account_id):
        supplier = account_for(request.user, account_id, 'supplier')
        refuse_synced(supplier)
        serializer = SupplierImportRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        file = serializer.validated_data['file']
        if not file.name.lower().endswith('.xlsx') or file.size > MAX_BYTES:
            raise ValidationError('Selecciona un archivo .xlsx de hasta 5 MB.')
        records, parse_warnings = read_supplier_excel(file.read())
        rows = [record for record in records if not record['errors']]
        plan, initial_fingerprint = stock_plan(supplier, rows)
        extra_errors = {}
        for error in plan['errors']:
            extra_errors.setdefault(error['row'], []).append(error)
        for record in records:
            record['errors'].extend(extra_errors.get(record['row'], []))
        rejected = [record for record in records if record['errors']]
        accepted = [record for record in records if not record['errors']]
        errors = [error for record in rejected for error in record['errors']]
        warnings = parse_warnings + plan['warnings']
        summary = {**plan['summary'], 'source_rows': len(records), 'rejected_rows': len(rejected)}
        preview = {'preview': plan['preview'][:100], 'preview_count': len(plan['preview']), 'errors': errors[:100],
                   'error_count': len(errors), 'warnings': warnings[:100], 'warning_count': len(warnings),
                   'reservations': plan['reservations']}  # every row with units held by confirmed deals, not only the first 100
        with transaction.atomic():
            Account.objects.select_for_update().get(pk=supplier.pk)
            _, locked_fingerprint = stock_plan(supplier, rows, lock=True)
            if locked_fingerprint != initial_fingerprint:
                raise StaleSupplierPreview()
            job = SupplierInventoryImportJob.objects.create(
                owner=request.user, supplier=supplier, filename=file.name[:255],
                status='ready' if accepted else 'review', expires_at=timezone.now() + timedelta(days=7),
                summary=summary, preview=preview,
                rejected_rows=[{key: record[key] for key in ['row', 'values', 'original', 'errors']} for record in rejected],
                total_rows=len(accepted), total_batches=(len(accepted) + BATCH_SIZE - 1) // BATCH_SIZE,
                applied_summary={**{key: 0 for key in plan['summary']}, 'settled_units': 0, 'held_units': 0})
            staged = []
            for index, chunk in batches(accepted, BATCH_SIZE):
                data = [{'row': record['row'], 'data': record['data']} for record in chunk]
                batch_plan, fingerprint = stock_plan(supplier, data)
                if batch_plan['errors']:
                    raise StaleSupplierPreview()
                staged.append(SupplierInventoryImportBatch(job=job, index=index, data=data, fingerprint=fingerprint))
            SupplierInventoryImportBatch.objects.bulk_create(staged, batch_size=500)
            # New code aliases can appear between independent reads. Do not
            # stage a newer snapshot behind an older displayed preview.
            if stock_plan(supplier, rows)[1] != initial_fingerprint:
                raise StaleSupplierPreview()
        return Response(job_result(job))


def outcome_key(action, line, status):
    """A row's outcome as the snapshot compares it: its line and status, a new product as one token: no line yet, a line in
    preparación (an earlier row or batch of the same upload may have just made it) or a line in referencia (an earlier batch's item
    may have just moved it to preparación: rule 7)."""
    if action == 'no_line' or (line is not None and line.estado in ('preparacion', 'referencia') and status == 'pending'):
        return 'nuevo', 'pending'
    return (str(line.pk) if line is not None else ''), status


def same_outcome(item, expected):
    """Whether the committed item ended as its preview row said. A new product (no_line) gets its line in Productos nuevos only at the
    commit, and a second row with the same code joins that line: either is a line in preparación with the item pending (or no line
    yet, when place() failed and the next matching pass makes it)."""
    if expected.get('resolucion') == 'no_line':
        return item.matching_status == 'pending' and (item.part_id is None or item.part.estado == 'preparacion')
    return (str(item.part_id) if item.part_id else None) == expected['part_id'] and item.matching_status == expected['matching_status']


class SupplierInventoryImportJobView(APIView):
    def get_job(self, request, account_id, pk, *, lock=False):
        supplier = account_for(request.user, account_id, 'supplier')
        query = SupplierInventoryImportJob.objects.filter(owner=request.user, supplier=supplier)
        if lock:
            query = query.select_for_update()
        return get_object_or_404(query, pk=pk)

    @extend_schema(responses=OpenApiTypes.OBJECT)
    def get(self, request, account_id, pk):
        return Response(job_result(self.get_job(request, account_id, pk)))

    @extend_schema(request=SupplierBatchRequest, responses=OpenApiTypes.OBJECT)
    def post(self, request, account_id, pk):
        serializer = SupplierBatchRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        index = serializer.validated_data['batch_index']
        supplier = account_for(request.user, account_id, 'supplier')
        try:
            with transaction.atomic():
                # Stock ingestion also takes this account lock first. Taking
                # it before the job/item locks gives every writer one order.
                Account.objects.select_for_update().get(pk=supplier.pk)
                refuse_synced(supplier)  # also a job staged before the account was connected
                job = self.get_job(request, account_id, pk, lock=True)
                if index < job.completed_batches:
                    return Response(job_result(job))
                if job.expires_at < timezone.now():
                    raise StaleSupplierPreview('Esta vista previa venció. Sus errores siguen disponibles para descargar; revisa el archivo otra vez.')
                if index != job.completed_batches or index >= job.total_batches:
                    raise StaleSupplierPreview('El número de lote cambió. Recupera el progreso antes de continuar.')
                batch = job.batches.get(index=index)
                plan, fingerprint = stock_plan(supplier, batch.data, lock=True)
                if fingerprint != batch.fingerprint or plan['errors']:
                    raise StaleSupplierPreview('Las existencias, su origen o su vínculo cambiaron para este lote. Los lotes anteriores siguen guardados; revisa una nueva vista previa.')
                expected_matches = {row['row']: row for row in plan['preview']}
                # Every confirmed order still holding units of a row needs the supplier's answer: does the file's count discount it?
                decisions = serializer.validated_data['decisions']
                held = {entry['row']: entry['orders'] for entry in plan['reservations']}
                undecided = [row for row, orders in held.items() if any(order['id'] not in decisions for order in orders)]
                if undecided:
                    raise ValidationError({'detail': 'Indica, para cada pedido confirmado de las filas '
                                                     f'{", ".join(map(str, sorted(undecided)[:10]))}, si el saldo de tu archivo ya lo descuenta.'})
                settled = kept = 0
                from .inventory_history import Recorder
                recorder = Recorder(supplier, 'excel', f'excel-{job.pk}', actor=request.user, import_job=job, filename=job.filename)
                for row in batch.data:
                    item = ingest_inventory(supplier=supplier, actor=request.user, recorder=recorder,
                                            data={**row['data'], 'source': 'upload', 'update_id': f'excel-{job.pk}-{row["row"]}'})
                    expected = expected_matches[row['row']]
                    if not same_outcome(item, expected):
                        # A previously absent SKU/code cannot be row-locked.
                        # If it appeared after validation, matching may have
                        # changed during ingestion; roll back the whole batch.
                        raise StaleSupplierPreview('Los códigos del catálogo cambiaron durante este lote. Revisa una nueva vista previa antes de continuar.')
                    orders = held.get(row['row'])
                    if orders:
                        settled += settle(item, [order['id'] for order in orders if decisions[order['id']] == 'net'], request.user,
                                          f'excel-{job.pk}-{row["row"]}')
                        kept += sum(order['quantity'] for order in orders if decisions[order['id']] == 'gross')
                # What the supplier answered stays with the job, so a resumed import shows it instead of asking again.
                for entry in job.preview.get('reservations', []):
                    if entry['row'] in held:
                        entry['applied'] = True
                        for order in entry['orders']:
                            order['decision'] = decisions.get(order['id'], '')
                job.applied_summary['settled_units'] = job.applied_summary.get('settled_units', 0) + settled
                job.applied_summary['held_units'] = job.applied_summary.get('held_units', 0) + kept
                for key, value in plan['summary'].items():
                    job.applied_summary[key] = job.applied_summary.get(key, 0) + value
                job.completed_batches += 1
                job.processed_rows += len(batch.data)
                job.status = 'completed' if job.completed_batches == job.total_batches else 'importing'
                job.save(update_fields=['applied_summary', 'preview', 'completed_batches', 'processed_rows', 'status'])
                recorder.finish(done=job.status == 'completed')
                if job.status == 'completed':
                    from .matching_queue import enqueue_matching
                    enqueue_matching()
                return Response(job_result(job))
        except IntegrityError:
            raise StaleSupplierPreview('No se guardó este lote porque las existencias cambiaron. Los lotes anteriores siguen guardados.')


class SupplierInventoryImportErrors(SupplierInventoryImportJobView):
    @extend_schema(responses={(200, XLSX_TYPE): bytes})
    def get(self, request, account_id, pk):
        job = self.get_job(request, account_id, pk)
        return _workbook_response(job.rejected_rows, 'errores-existencias.xlsx', errors=True)

    http_method_names = ['get', 'head', 'options']
