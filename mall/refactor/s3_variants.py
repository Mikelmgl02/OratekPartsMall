"""Stage S3 of the inventory refactor: every supplier item on a line hangs from one Reference of that line (SupplierItem.reference), and
the codes the matching learned since S2b become sources.

    python -m mall.refactor.s3_variants --dry-run|--apply --user USERNAME|--rollback [--report PATH] [--json]

It runs before S3 lands (the code before S3 ignores the new column; the catalog freeze keeps it from moving items meanwhile) and again
after, as a catch-up. Re-running changes nothing new.

Which Reference (plan §3 S3; mall.variants.choose, the rules the S3 code attaches by): on the item's own line, the Reference of its key
(the clave of its code, its brand; the supplier's interno code before a public one, the main before the others), else for a brandless
item the branded Reference of its clave (rule 2), else for a branded item the brandless Reference of its clave. With nothing of its
clave on the line a declarado Reference is made, with a proveedor source: public when its code is the line's own clave under another
brand, or its clave once trailing auto-eligible TAGs are stripped is on the line (rule 3); otherwise, or when a public one would clash
with another line's key, interno for the item's supplier (B19). Items on a retired line attach the same way; items with no line stay
without a Reference (pending, OD21). An item on a unificada line is listed (I3: Agrupar or Separar decides) and left alone. An item
whose Reference sits on another line than its own (moved since the last run) is attached again.

Learned codes: each SupplierCodeMapping no proveedor source cites yet (written since S2b's backfill) becomes a ReferenceSource(kind
proveedor, citation 'Código aprendido · <proveedor> · <fecha>') on its line's Reference with that code, as S2b did, or an interno
Reference of its supplier when its line has none.

It writes in bulk (no signals, no save() shim) under the matching worker's lock, with the supplier Accounts and then the items locked,
in one transaction; on the live database --apply and --rollback need the S0.11 guard (mall.refactor.guard). Every row it changes or
creates goes, with its values before, to mall.refactor.output.manifest_path('s3') before the transaction commits.

The dry run also works before 0075-0077 are applied: without S2b's mains it simulates them (mall.refactor.s2b_main_refs's plan, its
compact-duplicate merges included: an item on a line that merge absorbs counts on its keeper) and without 0077 it reads every item as
unattached. A rehearsal copy gives the real numbers.

--rollback (plan S3: stock is untouched): back to the rows the code before S3 reads (undo()): a waiting code becomes the item's code,
every item loses its Reference (its line stays), and the References the backfill or the S3 code made (manifest, detail['s3']) are
deleted, or retired when something names them. Order (the CHECKs of 0078 forbid an item on a line without a Reference, and the code
before S3 writes exactly that): under the freeze, reverse 0078 (python manage.py migrate mall 0077_variant_reference, from the
worktree), run --rollback, revert the code, then reverse 0077 from the worktree.
"""
import argparse
import json
import os
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from types import SimpleNamespace

STAGE = 's3'
# The live dry run of 2026-10-14, after S2b's backfill (plan §3 S3 simulated 23,434 · 14,056 · 7,617 · 1,756 · 2 · 3 new on the data of
# 2026-10-13, before S2b; S2b's own rules then gave fewer branded mains). The DFSK code 1001D50F0716 that plan B19 counts as a new interno
# Reference is S2b's (its learned codes made it), so S3 hangs its item from it ('interna') and makes the other two, both public.
# nuevas = public + interno. The live counts move with the catalog.
EXPECTED = {'variantes': 23441, 'principal': 16581, 'marca_de_la_linea': 5136, 'exacta': 1719, 'sin_marca': 2, 'interna': 1,
            'nuevas': 2, 'nuevas_internas': 0, 'en_retiradas': 30, 'sin_linea': 4194, 'en_unificadas': 0, 'aprendidos': 0}
LIVE_LINE = ('referencia', 'preparacion', 'publicada', 'rechazada', 'retirada')
CHUNK = 2000
MAPPING_KIND = 'proveedor'
TIGHTEN = ('variant_line_follows_reference', 'variant_reference_iff_line', 'variant_unattached_is_pending')


def current_database():
    from django.db import connection
    return str(connection.settings_dict['NAME'])


def output_dir():
    from .output import main_checkout
    return main_checkout() / 'output'


def manifest_path():
    from .output import manifest_path as stage_manifest_path
    return stage_manifest_path(STAGE)


def manifest_lines():
    path = manifest_path()
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def manifest_problem():
    """Why the manifest cannot take this run's lines, or None (the output folder is missing, or it holds another database's lines)."""
    if not os.environ.get('REFACTOR_OUTPUT', '').strip() and not output_dir().is_dir():
        return f'Falta la carpeta {output_dir()} para el manifiesto; ejecútalo dentro del contenedor de la app.'
    others = sorted({str(line.get('database')) for line in manifest_lines()} - {current_database()})
    if others:
        return (f"El manifiesto {manifest_path()} tiene líneas de otra base ({', '.join(others)}): muévelo antes de aplicar o deshacer "
                'en esta.')
    return None


def refusal():
    from .guard import refusal as shared
    return shared(True)


def write_manifest(lines):
    """Add the lines through a flushed temporary copy renamed over the manifest (all or none), inside the transaction."""
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        with open(temporary, 'w', encoding='utf-8') as handle:
            if path.exists():
                with open(path, encoding='utf-8') as old:
                    shutil.copyfileobj(old, handle)
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def recorded(values):
    return json.loads(json.dumps(values, default=str))


def schema():
    """{'reference': 0077 applied (SupplierItem.reference), 'mains': S2b's mains exist (0075/0076 and its backfill), 'tipo': 0075}."""
    from django.db import connection
    with connection.cursor() as cursor:
        items = {c.name for c in connection.introspection.get_table_description(cursor, 'mall_supplieritem')}
        parts = {c.name for c in connection.introspection.get_table_description(cursor, 'mall_part')}
        refs = {c.name for c in connection.introspection.get_table_description(cursor, 'mall_partcode')}
        mains = False
        if 'is_main' in refs:
            cursor.execute('SELECT EXISTS (SELECT 1 FROM mall_partcode WHERE is_main)')
            mains = bool(cursor.fetchone()[0])
    return {'reference': 'reference_id' in items, 'mains': mains, 'tipo': 'tipo' in parts, 'references': 'clave' in refs}


def tightened():
    from django.db import connection
    with connection.cursor() as cursor:
        present = connection.introspection.get_constraints(cursor, 'mall_supplieritem')
    return any(name in present for name in TIGHTEN)


# --- reading --------------------------------------------------------------------------------------------------------------------------

def line_rows():
    from ..models import Part
    return {row['pk']: row for row in Part._base_manager.order_by('pk').values('pk', 'sku', 'estado', 'merged_into_id').iterator(chunk_size=CHUNK)}


def live_line(line):
    return line is not None and not line['merged_into_id'] and line['estado'] in LIVE_LINE


def reference_rows():
    """The Reference rows, as the S3 code reads them."""
    from ..models import Reference
    fields = ('pk', 'part_id', 'code', 'brand', 'clave', 'supplier_id', 'is_main', 'estado', 'tipo', 'printed_forms')
    yield from Reference._base_manager.order_by('pk').values(*fields).iterator(chunk_size=CHUNK)


def simulated_references(lines):
    """(rows, into, note): the References as S2b's backfill would leave them (its mains reused or created, the codes it disputes),
    and {absorbed line: keeper} for the compact duplicates its merge hook would merge (their non-main codes move to the keeper; the
    absorbed main joins the keeper's, which has its key)."""
    from . import s2b_main_refs
    state = s2b_main_refs.schema()
    planned = s2b_main_refs.plan(state, hook=s2b_main_refs.merge_hook())
    rows = {row['pk']: dict(row) for row in s2b_main_refs.reference_rows(state)}
    for row in rows.values():
        row.setdefault('printed_forms', [])
    for pk, entry in planned['disputed'].items():
        if pk in rows:
            rows[pk]['estado'] = 'disputa'
    synthetic = 0
    for line_id, main in planned['mains'].items():
        if main['action'] == 'reuse':
            rows[main['ref']].update(is_main=True, code=main['code'], brand=main['brand'], tipo=main['tipo'], estado=main['estado'])
        elif main['action'] == 'create':
            synthetic -= 1
            rows[synthetic] = {'pk': synthetic, 'part_id': line_id, 'code': main['code'], 'brand': main['brand'], 'clave': main['clave'],
                               'supplier_id': None, 'is_main': True, 'estado': main['estado'], 'tipo': main['tipo'], 'printed_forms': []}
    by_text = {str(pk): pk for pk in lines}
    # The interno References S2b's backfill makes from learned codes whose line has no Reference with that code (its write()).
    from .. import references
    held = {(row['clave'], row['brand'], row['supplier_id']) for row in rows.values() if row.get('supplier_id') and row['estado'] != 'retirada'}
    for mapping in planned['mappings']:
        if mapping['reference'] is not None or mapping['main_of'] is not None or mapping['line'] not in by_text:
            continue
        code, brand, supplier = references.code_key(mapping['code']), references.brand_key(mapping['brand']), uuid.UUID(mapping['supplier'])
        key = (references.clave(code), brand, supplier)
        if key in held:
            continue
        held.add(key)
        synthetic -= 1
        rows[synthetic] = {'pk': synthetic, 'part_id': by_text[mapping['line']], 'code': code, 'brand': brand, 'clave': key[0],
                           'supplier_id': supplier, 'is_main': False, 'estado': 'declarado', 'tipo': 'interno', 'printed_forms': []}
    into = {by_text[pk]: by_text[keeper] for pk, keeper in (planned.get('hook_plan') or {}).get('into', {}).items()
            if pk in by_text and keeper in by_text}
    for row in rows.values():
        if row['part_id'] in into and row['estado'] != 'retirada':
            if row['is_main']:
                row['estado'] = 'retirada'  # absorbed into the keeper's main (the same key)
            else:
                row['part_id'] = into[row['part_id']]
    return list(rows.values()), into, planned['final']


def item_rows(state):
    from ..models import SupplierItem
    fields = ['pk', 'supplier_id', 'supplier__name', 'supplier_invent_id', 'part_id', 'codigo', 'brand', 'matching_status']
    if state['reference']:
        fields.append('reference_id')
    for row in SupplierItem.objects.order_by('pk').values(*fields).iterator(chunk_size=CHUNK):
        row.setdefault('reference_id', None)
        yield row


# --- plan -----------------------------------------------------------------------------------------------------------------------------

def plan(state=None):
    """Everything the apply writes, read-only: {'attach': [{item, line, ref, how, before}], 'new': {key: new Reference},
    'mappings', 'final', 'listed', 'simulated'}."""
    from .. import references, variants
    from .. import catalog_suffixes
    state = state or schema()
    lines = line_rows()
    simulated, into, s2b_final = None, {}, None
    if state['mains']:
        refs = list(reference_rows())
    else:
        refs, into, s2b_final = simulated_references(lines)
        simulated = {'mains_simulated': True, 'absorbed_lines': len(into)}
    by_line, by_id = defaultdict(list), {}
    for row in refs:
        if row['estado'] != 'retirada':
            row = SimpleNamespace(**row)
            by_line[row.part_id].append(row)
            by_id[row.pk] = row
    held = defaultdict(list)  # clave -> exclusive public rows, for the brandless reservation of a new public Reference
    for rows in by_line.values():
        for row in rows:
            if row.estado in references.EXCLUSIVE and row.supplier_id is None:
                held[row.clave].append(row)
    table = catalog_suffixes.suffix_table()
    final, listed = Counter(), defaultdict(list)
    attach, new = [], {}
    for item in item_rows(state):
        if item['part_id'] is None:
            final['sin_linea'] += 1
            continue
        line_id = into.get(item['part_id'], item['part_id'])
        line = lines.get(line_id)
        if not live_line(line):
            final['en_unificadas'] += 1
            listed['en_unificadas'].append(f"{item['codigo']} ({item['supplier_invent_id']}) en {line['sku'] if line else line_id}")
            continue
        current = by_id.get(item['reference_id']) if item['reference_id'] else None
        if current is not None and current.part_id == line_id and line_id == item['part_id']:
            final['ya_tenia'] += 1
            continue
        final['variantes'] += 1
        final['en_retiradas'] += line['estado'] == 'retirada'
        row, how = variants.choose(by_line[line_id], line['sku'], item['codigo'], item['brand'], item['supplier_id'], table=table)
        entry = {'item': item['pk'], 'line': line_id, 'supplier': item['supplier_id'], 'codigo': item['codigo'], 'brand': item['brand'],
                 'before': {'reference_id': item['reference_id'], 'part_id': item['part_id']}, 'how': how}
        if row is not None:
            entry['ref'] = row.pk
            final[how] += 1
        else:
            entry['new'] = new_key = new_reference(new, held, line, item, how)
            if new[new_key]['tipo'] == 'interno':
                final['nuevas_internas'] += new[new_key]['first'] == item['pk']
            final['nuevas'] += new[new_key]['first'] == item['pk']
            final[f"a_{new[new_key]['how']}"] += 1
            listed['nuevas'].append(f"{item['codigo']}{' (' + item['brand'] + ')' if item['brand'] else ''} en {line['sku']}: "
                                    f"{'interna de ' + (item['supplier__name'] or '') if new[new_key]['tipo'] == 'interno' else 'pública declarada'}")
        attach.append(entry)
    mappings = learned_since(lines, by_line, state) if state['mains'] else []
    final['aprendidos'] = len(mappings)
    return {'attach': attach, 'new': new, 'mappings': mappings, 'final': final, 'listed': dict(listed), 'schema': state,
            'simulated': simulated, 's2b': dict(s2b_final) if s2b_final else None}


def new_reference(new, held, line, item, how):
    """The key of the Reference made for an item nothing on its line takes: public (the brandless reservation checked against every
    other line's exclusive keys and the ones this run makes) or interno for its supplier; one per key and line."""
    from .. import references, variants
    code, brand = references.code_key(item['codigo']), references.brand_key(item['brand'])
    key = references.clave(code)
    if how == 'nueva':
        clash = any(row.part_id != line['pk'] and (not brand or not row.brand or row.brand == brand) for row in held.get(key, ()))
        clash = clash or any(other['line'] != line['pk'] and other['tipo'] != 'interno' and other['clave'] == key
                             and (not brand or not other['brand'] or other['brand'] == brand) for other in new.values())
        if clash:
            how = 'nueva_interna'
    supplier = item['supplier_id'] if how == 'nueva_interna' else None
    found = (line['pk'], key, brand, supplier)
    if found not in new:
        new[found] = {'line': line['pk'], 'code': code, 'brand': brand, 'clave': key, 'supplier': supplier, 'how': how,
                      'tipo': 'interno' if supplier else variants.new_reference_tipo(brand), 'first': item['pk'],
                      'source': f"Declarado por {item['supplier__name'] or ''} · artículo {item['supplier_invent_id']}"[:500]}
    return found


def learned_since(lines, by_line, state):
    """S2b's learned codes (mall.refactor.s2b_main_refs.learned_codes) for the SupplierCodeMapping rows no proveedor source cites."""
    from ..models import ReferenceSource
    from . import s2b_main_refs
    cited = {source for source in ReferenceSource.objects.filter(kind=MAPPING_KIND, detail__has_key='mapping')
             .values_list('detail__mapping', flat=True)}
    rows = {pk: [vars(row) for row in found] for pk, found in by_line.items()}
    return [mapping for mapping in s2b_main_refs.learned_codes(lines, {}, rows) if mapping['id'] not in cited]


# --- apply ----------------------------------------------------------------------------------------------------------------------------

def write(result, log, run, user):
    """Write the plan in bulk. Returns the counts written."""
    from .. import references, variants
    from ..models import Reference, ReferenceSource, SupplierItem
    counts = Counter()
    built = []
    for key, spec in result['new'].items():
        ref = references.build(spec['line'], spec['code'], spec['brand'], tipo=spec['tipo'], estado='declarado', supplier=spec['supplier'],
                               source=spec['source'], actor=user, detail={variants.TAG: {**variants.stamp('creada'), 'por': 's3_variants'}})
        built.append((key, ref))
    references.bulk_create([ref for _, ref in built], actor=user, serialized=True)  # under the freeze and the matching lock
    made = {}
    for key, ref in built:
        made[key] = ref.pk
        log.append({**run, 'model': 'mall.Reference', 'id': ref.pk, 'action': 'create', 'line': str(ref.part_id),
                    'after': recorded({'code': ref.code, 'brand': ref.brand, 'tipo': ref.tipo, 'supplier': ref.supplier_id, 'estado': ref.estado})})
    counts['referencias'] = len(built)
    items = {item.pk: item for item in SupplierItem.objects.filter(pk__in=[entry['item'] for entry in result['attach']])}
    changed = []
    for entry in result['attach']:
        item = items.get(entry['item'])
        ref_id = entry.get('ref') if 'ref' in entry else made[entry['new']]
        if item is None or ref_id is None or ref_id < 0:
            continue
        item.reference_id, item.part_id = ref_id, entry['line']
        changed.append(item)
        log.append({**run, 'model': 'mall.SupplierItem', 'id': str(item.pk), 'action': 'update', 'how': entry['how'],
                    'before': recorded(entry['before']), 'after': recorded({'reference_id': ref_id, 'part_id': entry['line']})})
    SupplierItem.objects.bulk_update(changed, ['reference', 'part'], batch_size=1000)  # no save(): the shim has nothing to add
    counts['variantes'] = len(changed)
    sources, internos = [], []
    held = set(Reference.all_objects.filter(tipo='interno', estado__in=references.EXCLUSIVE).values_list('clave', 'brand', 'supplier_id'))
    for mapping in result['mappings']:
        citation = f"Código aprendido · {mapping['supplier_name']} · {mapping['date']}"
        detail = {'legacy': 'SupplierCodeMapping', 'mapping': mapping['id'], 'supplier': mapping['supplier'], 'code': mapping['code'],
                  'brand': mapping['brand']}
        if mapping['reference'] is None:
            if mapping['line'] is None:
                continue
            key = (references.clave(mapping['code']), references.brand_key(mapping['brand']), uuid.UUID(mapping['supplier']))
            if key in held:
                continue
            held.add(key)
            ref = references.build(uuid.UUID(mapping['line']), mapping['code'], mapping['brand'], tipo='interno', supplier=mapping['supplier'],
                                   actor=user, detail={variants.TAG: {**variants.stamp('creada'), 'por': 's3_variants'}})
            internos.append((ref, citation, detail))
            continue
        if not ReferenceSource.objects.filter(reference_id=mapping['reference'], kind=MAPPING_KIND, citation=citation).exists():
            sources.append(ReferenceSource(reference_id=mapping['reference'], kind=MAPPING_KIND, citation=citation, detail=detail, created_by=user))
    references.bulk_create([ref for ref, _, _ in internos], actor=user, serialized=True)
    for ref, citation, detail in internos:
        log.append({**run, 'model': 'mall.Reference', 'id': ref.pk, 'action': 'create', 'role': 'interno', 'line': str(ref.part_id),
                    'after': recorded({'code': ref.code, 'brand': ref.brand, 'tipo': 'interno', 'supplier': ref.supplier_id})})
        sources.append(ReferenceSource(reference_id=ref.pk, kind=MAPPING_KIND, citation=citation, detail=detail, created_by=user))
    ReferenceSource.objects.bulk_create(sources, batch_size=500)
    for source in sources:
        log.append({**run, 'model': 'mall.ReferenceSource', 'id': source.pk, 'action': 'create',
                    'after': recorded({'reference_id': source.reference_id, 'kind': source.kind, 'citation': source.citation})})
    counts['fuentes'], counts['internas_aprendidas'] = len(sources), len(internos)
    return dict(counts)


# --- rollback -------------------------------------------------------------------------------------------------------------------------

def applied_lines():
    """{(model, id): [lines]} of what the applies on this database did and no rollback has put back, oldest first."""
    rows, database = defaultdict(list), current_database()
    for line in manifest_lines():
        if line.get('stage') != STAGE or line.get('database') != database or not line.get('model'):
            continue
        key, action = (line['model'], str(line['id'])), line.get('action')
        if action in ('update', 'create'):
            rows[key].append(line)
        elif action in ('rollback', 'delete', 'retire'):
            rows.pop(key, None)
    return rows


def undo(log, run):
    """Put S3's rows back to the state the code before S3 reads: {'restored', 'deleted', 'retired', 'pending_applied', 'changed': [...]}.

    - A code change still waiting for review (codigo_pendiente/brand_pendiente) becomes the item's code, as the code before S3 wrote
      every load's code, its status kept (review: the old matching looks again); 0077 reversed would drop the waiting code otherwise.
    - Every item, attached by the backfill or by the S3 code since, loses its Reference; its line stays (before S3 an item hangs from
      its line). The values before go to the manifest, so the backfill re-run puts them back.
    - The References the backfill created (its manifest) and those the S3 code created or brought back (detail['s3']: Vincular, a
      linked or approved code, the save() shim, a load's new item or same-key brand) are deleted when nothing points at them, else
      retired (one another Reference names as its replacement, or one brought back from retirada) and listed; a line's main or a
      Reference that moved line is left and listed. The code before S3 would show an interno code as a plain alterno to every
      supplier and client.
    - The sources the backfill created are deleted."""
    from django.db.models import Q
    from django.utils import timezone
    from .. import references, variants
    from ..models import Reference, ReferenceSource, SupplierItem
    by_model = defaultdict(dict)
    for (model, pk), chain in applied_lines().items():
        by_model[model][pk] = chain
    restored, deleted, retired, applied, changed = 0, 0, 0, 0, []
    now = timezone.now()
    waiting = SupplierItem.objects.filter(Q(codigo_pendiente__isnull=False) | Q(brand_pendiente__isnull=False)).order_by('pk')
    for item in waiting.only('pk', 'codigo', 'brand', 'codigo_pendiente', 'brand_pendiente', 'matching_status'):
        before = {'codigo': item.codigo, 'brand': item.brand, 'codigo_pendiente': item.codigo_pendiente,
                  'brand_pendiente': item.brand_pendiente, 'matching_status': item.matching_status}
        variants.apply_pending(item)
        after = {'codigo': item.codigo, 'brand': item.brand, 'codigo_pendiente': None, 'brand_pendiente': None}
        SupplierItem.objects.filter(pk=item.pk).update(**after, updated_at=now)
        applied += 1
        log.append({**run, 'model': 'mall.SupplierItem', 'id': str(item.pk), 'action': 'rollback', 'role': 'pendiente',
                    'before': recorded(before), 'after': recorded(after)})
    hung = list(SupplierItem.objects.filter(reference__isnull=False).order_by('pk').values_list('pk', 'reference_id', 'part_id'))
    for start in range(0, len(hung), CHUNK):
        SupplierItem.objects.filter(pk__in=[pk for pk, _, _ in hung[start:start + CHUNK]]).update(reference=None)  # the line stays
    for pk, ref_id, part_id in hung:
        log.append({**run, 'model': 'mall.SupplierItem', 'id': str(pk), 'action': 'rollback',
                    'before': recorded({'reference_id': ref_id, 'part_id': part_id}), 'after': {'reference_id': None}})
    restored += len(hung)
    for pk, chain in by_model.get('mall.ReferenceSource', {}).items():
        source = ReferenceSource.objects.filter(pk=pk).first()
        if source is not None:
            source.delete()
            deleted += 1
            log.append({**run, 'model': 'mall.ReferenceSource', 'id': int(pk), 'action': 'delete', 'before': chain[-1]['after']})
    made = {int(pk): chain[0]['after'] for pk, chain in by_model.get('mall.Reference', {}).items() if chain[0]['action'] == 'create'}
    stamped = {row['pk']: row for row in Reference._base_manager.filter(detail__has_key=variants.TAG)
               .values('pk', 'part_id', 'code', 'brand', 'tipo', 'supplier_id', 'estado', 'is_main', 'detail')}
    created, revived = set(made), set()
    for pk, row in stamped.items():
        how = (row['detail'].get(variants.TAG) or {}).get('como')
        if how == 'creada':
            created.add(pk)
        elif how == 'revivida':
            revived.add(pk)
        elif how == 'movida':
            changed.append(f"referencia {pk} ({row['code']}): S3 la pasó de línea con su artículo, se deja en su línea")
    rows = {row['pk']: row for row in Reference._base_manager.filter(pk__in=created | revived)
            .values('pk', 'code', 'brand', 'tipo', 'supplier_id', 'part_id', 'estado', 'is_main')}
    mains = {pk for pk, row in rows.items() if row['is_main']}
    for pk in sorted(mains):
        changed.append(f"referencia {pk} ({rows[pk]['code']}): es la principal de su SKU, se deja")
    named = set(Reference._base_manager.filter(reemplazado_por__in=list(created - mains)).values_list('reemplazado_por', flat=True))
    free = sorted(pk for pk in created - mains - named if pk in rows)
    deleted += references.purge(free)
    for pk in free:
        if not Reference._base_manager.filter(pk=pk).exists():
            log.append({**run, 'model': 'mall.Reference', 'id': pk, 'action': 'delete', 'before': recorded(made.get(pk) or rows[pk])})
    doomed = sorted(pk for pk in (named | revived) - mains if pk in rows and rows[pk]['estado'] != 'retirada')
    for pk in sorted(named - mains):
        changed.append(f"referencia {pk} ({rows[pk]['code']}): otra la nombra como reemplazo, se retira en vez de borrarse")
    if doomed:
        retired = references.retire(Reference._base_manager.filter(pk__in=doomed), reason='Rollback de S3', allow_variants=True)
        for pk in doomed:
            log.append({**run, 'model': 'mall.Reference', 'id': pk, 'action': 'retire', 'before': recorded({'estado': rows[pk]['estado']}),
                        'after': {'estado': 'retirada'}})
    return {'restored': restored, 'deleted': deleted, 'retired': retired, 'pending_applied': applied, 'changed': changed}


# --- run ------------------------------------------------------------------------------------------------------------------------------

ROWS = (('variantes', 'artículos por colgar'), ('principal', 'en su código principal'), ('marca_de_la_linea', 'sin marca, en la de marca'),
        ('exacta', 'en otra referencia exacta'), ('sin_marca', 'en la referencia sin marca'), ('interna', 'en un código interno'),
        ('nuevas', 'referencias nuevas'), ('nuevas_internas', '  internas'), ('en_retiradas', 'en líneas retiradas'),
        ('sin_linea', 'sin línea (pendientes)'), ('en_unificadas', 'en líneas unificadas'), ('aprendidos', 'códigos aprendidos'))


def print_summary(stdout, mode, result, writes=None):
    title = {'apply': 'Aplicado', 'dry_run': 'Simulación'}[mode]
    final = result['final']
    stdout.write(f'{title} · S3 artículos colgados de referencias\n')
    if result['simulated']:
        stdout.write('  (sin las principales de S2b en esta base: se simulan con el plan de s2b_main_refs, sus unificaciones '
                     f"compact_duplicate incluidas ({result['simulated']['absorbed_lines']} líneas absorbidas); sin 0077 todos los "
                     'artículos cuentan sin referencia; los códigos aprendidos los convierte S2b)\n')
    stdout.write(f"  {'':30} {'esperado':>9} {'resultado':>10}\n")
    for key, label in ROWS:
        stdout.write(f"  {label:30} {EXPECTED[key]:>9,} {final.get(key, 0):>10,}\n")
    if final.get('ya_tenia'):
        stdout.write(f"  artículos que ya tenían referencia: {final['ya_tenia']:,}\n")
    made = {key: final.get(f'a_{key}', 0) for key in ('nueva', 'nueva_interna')}
    if any(made.values()):
        stdout.write(f"  artículos en referencias nuevas: públicas {made['nueva']:,} · internas {made['nueva_interna']:,}\n")
    for label, key in (('Referencias nuevas', 'nuevas'), ('En líneas unificadas (I3, se dejan)', 'en_unificadas')):
        rows = result['listed'].get(key, [])
        if rows:
            stdout.write(f'  {label}: {len(rows)}\n')
            for row in rows[:20]:
                stdout.write(f'    {row}\n')
    if writes:
        stdout.write('  Escrito: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(writes.items())) + '\n')


def report(result):
    return {'expected': EXPECTED, 'result': dict(result['final']), 'listed': result['listed'], 'simulated': result['simulated'],
            'new': [{**spec, 'line': str(spec['line']), 'supplier': str(spec['supplier']) if spec['supplier'] else None}
                    for spec in result['new'].values()],
            'mappings': result['mappings'], 's2b': result['s2b']}


def run(*, apply=False, rollback=False, username=None, report_path=None, stdout=None):
    """Plan, and with apply write it, or with rollback undo the applies on this database. None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from ..matching_queue import matching_lock, matching_work
    from ..models import Account, SupplierItem
    stdout = stdout or sys.stdout
    user = None
    if apply or rollback:
        reason = refusal() or manifest_problem()
        if reason:
            stdout.write(reason + '\n')
            return None
        user = get_user_model().objects.filter(username=username or '').first()
        if user is None and (apply or username):
            stdout.write('Indica con --user el usuario que figura como autor del cambio.\n' if not username else f'No existe el usuario {username}.\n')
            return None
        state = schema()
        if not state['reference']:
            stdout.write('Falta la migración 0077_variant_reference en esta base: aplícala primero (la simulación funciona sin ella).\n')
            return None
        if apply and not state['mains']:
            stdout.write('Las líneas no tienen todavía su referencia principal: ejecuta antes python -m mall.refactor.s2b_main_refs --apply.\n')
            return None
        if rollback and tightened():
            stdout.write('0078_variant_reference_checks está aplicada: reviértela primero (python manage.py migrate mall 0077_variant_reference).\n')
            return None
    run_line = {'stage': STAGE, 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}

    def go():
        log = []
        with transaction.atomic():
            if rollback:
                result = {'mode': 'rollback', **undo(log, run_line)}
            else:
                if apply:  # the lock order of every writer of items: supplier Accounts, then the items, so the plan sees what it writes
                    suppliers = sorted(set(SupplierItem.objects.values_list('supplier_id', flat=True)))
                    list(Account.objects.select_for_update().filter(pk__in=suppliers).order_by('pk').values_list('pk', flat=True))
                    list(SupplierItem.objects.select_for_update().filter(part__isnull=False).order_by('pk').values_list('pk', flat=True))
                planned = plan()
                result = {'mode': 'apply' if apply else 'dry_run', **report(planned)}
                writes = None
                if apply:
                    writes = result['written'] = write(planned, log, run_line, user)
                print_summary(stdout, result['mode'], planned, writes)
                if report_path:
                    with open(report_path, 'w', encoding='utf-8') as handle:
                        json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
                    stdout.write(f'Informe: {report_path}\n')
            if log:
                result['manifest'] = str(write_manifest([{**run_line, 'action': 'run', 'mode': result['mode'], 'rows': len(log)}, *log]))
        result['rows_logged'] = len(log)
        if rollback:
            stdout.write(f"Rollback · S3: {result['restored']} artículos sin referencia · {result['pending_applied']} códigos pendientes "
                         f"aplicados · {result['deleted']} filas borradas · {result['retired']} referencias retiradas · "
                         f"{len(result['changed'])} se dejan\n")
            for line in result['changed'][:40]:
                stdout.write(f'  {line}\n')
        if result.get('manifest'):
            stdout.write(f"Manifiesto: {result['manifest']}\n")
        return result

    if not (apply or rollback):
        return go()
    with matching_lock() as acquired, matching_work():
        if not acquired:
            stdout.write('El análisis de coincidencias está en curso; vuelve a intentarlo cuando termine.\n')
            return None
        try:
            return go()
        except OSError as error:
            stdout.write(f'No se pudo escribir el manifiesto ({error}): no se cambia nada.\n')
            return None


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='S3: cada artículo de proveedor cuelga de una referencia de su línea.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los cambios sin escribir nada (también antes de 0075-0077: simulación).')
    mode.add_argument('--apply', action='store_true', help='Escribe todo en una transacción (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base (requiere el congelamiento en vivo).')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply; opcional con --rollback).')
    parser.add_argument('--report', help='Escribe en esta ruta (JSON) las referencias nuevas, los códigos aprendidos y las listas.')
    parser.add_argument('--json', action='store_true', help='Imprime también el resultado en JSON.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .guard import require_freeze
    require_freeze(options.apply or options.rollback)
    stdout = stdout or sys.stdout
    result = run(apply=options.apply, rollback=options.rollback, username=options.user, report_path=options.report, stdout=stdout)
    if result is not None and options.json:
        stdout.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
    return 0 if result is not None else 1


if __name__ == '__main__':
    sys.exit(main())
