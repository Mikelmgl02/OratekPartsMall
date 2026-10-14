"""Stage S2b of the inventory refactor: every live line gets its main Reference (the Reference whose code is its codigo), the line's
tipo mirrors it, the codes two lines hold become merge groups (part B decides them), the OEM numbers two catalog lines carry become
cases for later review, and the codes the matching learned from suppliers become sources.

    python -m mall.refactor.s2b_main_refs --dry-run|--apply --user USERNAME|--rollback [--report PATH] [--json]

It runs only after S2b lands (before that the mains would show in the old code's code lists); until it finishes the code reads a line
without a main through its codigo, and writes are frozen. Re-running changes nothing new.

Mains (plan §3 S2b, step 1), for every live line (any estado but unificada) that has none yet:
- Reuse a live Reference of the line whose clave is the codigo's when its tipo fits the line (an oem row for an OEM line, otherwise a
  non-oem row): the exact spelling first, then the one with the rule's brand, then the surer estado, then the lower id. Another
  spelling is respelled to the codigo and kept in printed_forms (I2: the main's code is the codigo). A row of the same key that does not
  fit is reused all the same (two rows cannot hold one key) and takes the rule's tipo. A reused row keeps its brand when it has one.
- Otherwise create one. Its brand and tipo come from the first rule that applies:
    1  is_OEM with its own branded OEM code (a live oem Reference with the codigo's key)   that brand          oem
    2  its code is exactly one library number (one maker, not only in dispute)            the maker           oem
    3  is_OEM with 2+ makers or none                                                       ''                  oem
    4  the outermost brand/house_brand TAG of its codigo names one identified brand        that brand          aftermarket
    5  that TAG is compound or names no identified brand (listed for Marcas)              ''                  aftermarket
    6  anything else                                                                       ''                  otro
  The suffix table is read with D4 applied in memory until S0.1 is (as S1 did), and a TAG name is a brand when Brand has it (S1 seeded
  only the identified ones).
- A main whose key another line holds (a compact duplicate: two codigos with one key, or a codigo that is another line's code; a
  brandless key clashes with every brand) joins a merge group. The keeper is the line whose own codigo it is, by rule 3g: publicada
  before retirada, then more stock, more order lines, more References, then the spelling without a trailing - or _. The other mains
  and the other lines' codes in the group become disputa ("Código duplicado de <keeper>: caso de unificación"). The groups go to the
  merge hook (part B: mall.line_merge.s2b_compact_duplicates, OD4: those that pass the noun and spec checks merge on their own, with a
  digest and Separar; the rest become compact_duplicate cases); it plans in the dry run too, and the report lists both (the digest the
  owner sees before the window). Without the hook they are listed and wait in disputa.
Shared OEM numbers (step 3, OD16): a library number that two or more catalog lines (publicada or retirada, not merged) carry, through
their codigo, a code (an OEM one only under its maker) or a catalog or sku_name link, groups those lines (numbers sharing a line join
one group). Each group gets one legacy catalog case (method shared_oem_number) proposing the line whose own codigo is a number of the
group (else the rule 3g keeper); approving it merges the group, dismissing it keeps them apart. Groups a merge group already holds are
left to it. They never merge on their own, and S4 waits for them (OD16 may waive that).
Learned codes (step 4): each SupplierCodeMapping becomes a ReferenceSource(kind proveedor, citation 'Código aprendido · <proveedor> ·
<fecha>') on its line's Reference with that code (its main included), by compact code, its brand or none; a mapping with no such
Reference becomes an interno Reference of its supplier.
Line tipo (step 5): Part.tipo follows the main (oem OEM, aftermarket AFTERMARKET, interno and otro UNKNOWN) and is_OEM stays in step.

It writes in bulk (no signals: a main is no evidence for the OEM library) under the matching worker's lock, in one transaction; on the
live database --apply and --rollback need the S0.11 guard (mall.refactor.guard). Every row it changes or creates goes, with its values
before, to mall.refactor.output.manifest_path('s2b') before the transaction commits. The dry run also works before 0073 and 0075 are
applied (References read as 0073 leaves them, lines as UNKNOWN): a simulation; a rehearsal copy gives the real numbers.

--rollback (plan S2b): the merge hook's rollback first (Separar, newest first; a merge whose lines went on into another line stays,
listed, with what it recorded), then the mains and interno References it created are deleted (nothing hangs from them before S3), its
sources and cases deleted, the reused rows, the codes it put in disputa and the lines' tipo and is_OEM put back, each unless it changed
since (listed); last, the mains the S2b code made after landing (--landed-at, else the first apply's start) on a line's codigo are
deleted and every other main left is unmarked, so the code before S2b lists none of them as an alterno. Order (rollback rungs 2-3 of
plan §1.3): reverse 0076 (it refuses while 0076 is applied), --rollback, revert the code, restart the workers; reverse 0075 last, if
at all. Never revert the code alone once the backfill ran: the code before S2b moves a source's main onto its merge target (two mains)
and writes is_OEM without tipo (0076's line_oem_iff_tipo). Until the code is reverted, mall.references.ensure_mains stays idle (more
than BACKFILL_PENDING_LINES lines without a main), so the worker does not make them again.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter, defaultdict

STAGE = 's2b'
# Plan §3 S2b (2026-10-13 simulation; the live counts move with the catalog and OD18's separations).
EXPECTED = {'lineas': 31758, 'reusar': 736, 'crear': 31022, 'oem': 679, 'aftermarket': 9206, 'aftermarket_marca': 8309, 'otro': 21873,
            'grupos_duplicados': 69, 'lineas_duplicadas': 139, 'grupos_compartidos': 103, 'numeros_compartidos': 301, 'aprendidos': 125,
            'internos': 2, 'is_oem_cambia': 2}
MERGE_HOOK = 'mall.line_merge.s2b_compact_duplicates'
MERGE_ROLLBACK_HOOK = 'mall.line_merge.s2b_rollback'
REF_FIELDS = ('is_main', 'brand', 'tipo', 'ref_type', 'code', 'printed_forms', 'estado', 'dispute_note')
PART_FIELDS = ('tipo', 'is_OEM')
LINE_TIPO = {'oem': 'OEM', 'aftermarket': 'AFTERMARKET', 'interno': 'UNKNOWN', 'otro': 'UNKNOWN'}
REF_TYPE_OF = {'oem': 'oem', 'aftermarket': 'company', 'interno': 'unknown', 'otro': 'unknown'}
TIPO_OF = {'oem': 'oem', 'company': 'aftermarket', 'unknown': 'otro'}
EXCLUSIVE = ('verificado', 'declarado', 'inferido')
ESTADO_RANK = {'verificado': 0, 'declarado': 1, 'inferido': 2, 'disputa': 3}
TAG_ROLES = ('brand', 'house_brand')
CHUNK = 2000
SHARED_METHOD, MAPPING_KIND = 'shared_oem_number', 'proveedor'


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
    """{'references': 0073 applied (is_main, estado, clave...), 'tipo': 0075 applied (Part.tipo)}."""
    from django.db import connection
    with connection.cursor() as cursor:
        references = {c.name for c in connection.introspection.get_table_description(cursor, 'mall_partcode')}
        parts = {c.name for c in connection.introspection.get_table_description(cursor, 'mall_part')}
    return {'references': {'is_main', 'estado', 'clave', 'tipo', 'supplier_id'} <= references, 'tipo': 'tipo' in parts}


def tightened():
    from django.db import connection
    with connection.cursor() as cursor:
        return 'reference_one_main_per_line' in connection.introspection.get_constraints(cursor, 'mall_partcode')


# --- reading --------------------------------------------------------------------------------------------------------------------------

def compact(code):
    return re.sub(r'[^A-Z0-9]', '', (code or '').upper())


def live(line):
    return not line['merged_into_id'] and line['estado'] != 'unificada'


def line_rows(state):
    """{id: line} of every line: sku, estado, active, merged_into_id, is_OEM, tipo (UNKNOWN before 0075), name."""
    from ..models import Part
    fields = ['pk', 'sku', 'estado', 'active', 'merged_into_id', 'is_OEM', 'name'] + (['tipo'] if state['tipo'] else [])
    rows = {}
    for row in Part._base_manager.order_by('pk').values(*fields).iterator(chunk_size=CHUNK):
        row.setdefault('tipo', 'UNKNOWN')
        rows[row['pk']] = row
    return rows


def reference_rows(state):
    """Every Reference's values as the plan reads them; before 0073 as it leaves them (tipo from ref_type, declarado, no main)."""
    from .. import references
    from ..models import Reference
    base = ['pk', 'part_id', 'code', 'brand', 'ref_type', 'reference_source']
    if state['references']:
        yield from Reference._base_manager.order_by('pk').values(*base, 'tipo', 'estado', 'supplier_id', 'is_main', 'clave', 'printed_forms',
                                                                   'dispute_note').iterator(chunk_size=CHUNK)
        return
    for row in Reference._base_manager.order_by('pk').values(*base).iterator(chunk_size=CHUNK):
        yield {**row, 'tipo': TIPO_OF.get(row['ref_type'], 'otro'), 'estado': 'declarado', 'supplier_id': None, 'is_main': False,
               'clave': references.clave(row['code']), 'printed_forms': [], 'dispute_note': ''}


def library_makers():
    """{compact number: {maker}} of the OEM library, leaving out the makers that file a number only in dispute."""
    from ..oem_reference_models import OEMReference
    makers = defaultdict(set)
    for code, manufacturer, status in OEMReference.objects.values_list('code', 'manufacturer', 'status').iterator(chunk_size=CHUNK):
        if status != 'disputed':
            makers[code].add(manufacturer)
    return makers


class TagBrands:
    """The brand a codigo's outermost brand or house_brand TAG names: (Brand nombre, 'tag'), (None, 'unidentified') for a compound or
    unknown name, (None, '') for no such TAG. The suffix table with D4 applied in memory until S0.1 is (mall.refactor.s1_lifecycle)."""

    def __init__(self):
        from .. import catalog_suffixes as cs
        from .s1_lifecycle import suffix_rows
        rows = suffix_rows()
        self.table = cs.SuffixTable(rows, source='db') if rows else cs.suffix_table()
        self.names = {}

    def brand(self, name):
        from ..brands import lookup, normalize
        name = normalize(name)
        if name not in self.names:
            found = None if '/' in name or not name else lookup(name)
            self.names[name] = found.nombre if found else None
        return self.names[name]

    def __call__(self, sku):
        from .. import catalog_suffixes as cs
        _, chain, klass = cs.strip_tags(sku, table=self.table)
        if klass != 'tag':
            return None, ''
        entries = [item['entry'] for item in chain if item['entry'].cls == 'TAG' and item['entry'].role in TAG_ROLES]
        if not entries:
            return None, ''
        nombre = self.brand(entries[-1].brand_name())
        return (nombre, 'tag') if nombre else (None, 'unidentified')


def line_stats(ids):
    """{line id: (stock, order lines)} for the keeper's rule 3g order."""
    from django.db.models import Count, Sum
    from ..models import SupplierItem
    from ..request_models import SupplierRequestLine
    stock = dict(SupplierItem.objects.filter(part_id__in=ids).values('part_id').annotate(n=Sum('reported_quantity')).values_list('part_id', 'n'))
    orders = dict(SupplierRequestLine.objects.filter(part_id__in=ids).values('part_id').annotate(n=Count('pk')).values_list('part_id', 'n'))
    return {pk: (stock.get(pk) or 0, orders.get(pk) or 0) for pk in ids}


# --- plan -----------------------------------------------------------------------------------------------------------------------------

def rule_of(line, own, makers, tags):
    """(brand, tipo, rule) of a new main (the table above). own: the line's live References with the codigo's key."""
    from .. import references
    key = references.clave(line['sku'])
    number = compact(line['sku'])
    oem_like = len(number) >= 8 and len(re.sub(r'[^0-9]', '', line['sku'])) >= 3  # the clave's OEM-like rule
    found = makers.get(number, set()) if oem_like else set()
    branded = sorted((row for row in own if row['tipo'] == 'oem' and row['brand']), key=lambda row: (row['code'] != line['sku'], row['pk']))
    if line['is_OEM'] and branded:
        return branded[0]['brand'], 'oem', 1
    if len(found) == 1:
        return next(iter(found)), 'oem', 2
    if line['is_OEM']:
        return '', 'oem', 3
    brand, reason = tags(line['sku'])
    if brand:
        return brand, 'aftermarket', 4
    if reason == 'unidentified':
        return '', 'aftermarket', 5
    return '', 'otro', 6


def keeper_key(line, stats, ref_count):
    stock, orders = stats.get(line['pk'], (0, 0))
    return (line['estado'] != 'publicada', -stock, -orders, -ref_count.get(line['pk'], 0), bool(re.search(r'[-_]$', line['sku'])),
            line['sku'], str(line['pk']))


def clashes(brand, other_brand):
    """Whether two exclusive keys of one clave clash: the same brand, or either brandless (a brandless code reserves every brand)."""
    return not brand or not other_brand or brand == other_brand


def plan(state=None, *, hook=None, actor=''):
    """Everything the apply writes, read-only: {'lines', 'mains': {line id: main}, 'groups', 'disputed': {ref id: note}, 'tipos',
    'shared', 'mappings', 'final', 'listed'}."""
    from .. import references
    state = state or schema()
    lines = line_rows(state)
    refs = list(reference_rows(state))
    live_refs = [row for row in refs if row['estado'] != 'retirada']
    by_line = defaultdict(list)
    for row in live_refs:
        by_line[row['part_id']].append(row)
    makers, tags = library_makers(), TagBrands()
    mains, final, listed = {}, Counter(), defaultdict(list)
    for pk, line in lines.items():
        if not live(line):
            continue
        current = [row for row in by_line[pk] if row['is_main']]
        if current:
            mains[pk] = {'line': pk, 'action': 'keep', 'ref': current[0]['pk'], 'code': current[0]['code'], 'brand': current[0]['brand'],
                         'tipo': current[0]['tipo'], 'estado': current[0]['estado'], 'clave': current[0]['clave'], 'rule': 0}
            final['ya_tenia'] += 1
            continue
        key = references.clave(line['sku'])
        own = [row for row in by_line[pk] if row['clave'] == key and row['supplier_id'] is None]
        brand, tipo, rule = rule_of(line, own, makers, tags)
        fits = [row for row in own if (row['tipo'] == 'oem') == (tipo == 'oem')]
        same_key = [row for row in own if row['brand'] == brand]
        ranked = sorted(fits or same_key, key=lambda row: (row['code'] != line['sku'], row['brand'] != brand,
                                                          ESTADO_RANK.get(row['estado'], 9), row['pk']))
        main = {'line': pk, 'code': line['sku'], 'clave': key, 'rule': rule, 'estado': 'declarado'}
        if ranked:
            row = ranked[0]
            keep_brand = bool(row['brand'])
            main.update(action='reuse', ref=row['pk'], brand=row['brand'] if keep_brand else brand,
                        tipo=(row['tipo'] if keep_brand and row['tipo'] != 'otro' else tipo) if fits else tipo,
                        estado=row['estado'], before={field: row.get(field) for field in REF_FIELDS})
            if main['brand'] != row['brand'] and any(other['brand'] == main['brand'] for other in own if other['pk'] != row['pk']):
                main['brand'] = row['brand']  # the line already holds the rule's brand under this key on another row
            final['reusar'] += 1
        else:
            main.update(action='create', ref=None, brand=brand, tipo=tipo)
            final['crear'] += 1
        if rule == 5:
            listed['marca_sin_identificar'].append(line['sku'])
        if rule == 3:
            listed['oem_sin_fabricante'].append(line['sku'])
        mains[pk] = main
    groups, disputed = merge_groups(lines, mains, live_refs, by_line)
    planned = hook(groups, apply=False, actor=None, run=None, log=None) if hook is not None and groups else {}
    into = {str(pk): str(keeper) for pk, keeper in (planned.get('into') or {}).items()}
    into.update({str(pk): None for pk in planned.get('absorbed') or [] if str(pk) not in into})
    absorbed = set(into)
    tipos = {}
    for pk, main in mains.items():
        line, tipo = lines[pk], LINE_TIPO.get(main['tipo'], 'UNKNOWN')
        if (line['tipo'], line['is_OEM']) != (tipo, tipo == 'OEM'):
            tipos[pk] = {'before': {'tipo': line['tipo'], 'is_OEM': line['is_OEM']}, 'after': {'tipo': tipo, 'is_OEM': tipo == 'OEM'}}
        if main['action'] != 'keep':
            final[main['tipo']] += 1
            final['aftermarket_marca'] += main['tipo'] == 'aftermarket' and bool(main['brand'])
            final[f"regla_{main['rule']}"] += 1
    final['lineas'] = sum(1 for line in lines.values() if live(line))
    final['is_oem_cambia'] = sum(1 for change in tipos.values() if change['before']['is_OEM'] != change['after']['is_OEM'])
    final['grupos_duplicados'], final['lineas_duplicadas'] = len(groups), len({pk for group in groups for pk in group['lines']})
    shared = shared_numbers(lines, mains, live_refs, groups, into)
    final['grupos_compartidos'], final['numeros_compartidos'] = len(shared), len({n for group in shared for n in group['numbers']})
    mappings = learned_codes(lines, mains, by_line)
    final['aprendidos'] = sum(1 for m in mappings if m['reference'] is not None or m['main_of'] is not None)
    final['internos'] = sum(1 for m in mappings if m['reference'] is None and m['main_of'] is None)
    return {'lines': lines, 'mains': mains, 'groups': groups, 'disputed': disputed, 'tipos': tipos, 'shared': shared, 'mappings': mappings,
            'final': final, 'listed': dict(listed), 'absorbed': sorted(map(str, absorbed)), 'schema': state, 'hook_plan': planned}


def merge_groups(lines, mains, live_refs, by_line):
    """([group], {reference id: note}): the keys a new main clashes on with another line, one group per clave, the rule 3g keeper
    among the lines whose own codigo it is, and the disputa notes of the other mains (by line, 'main:<id>') and codes."""
    held = defaultdict(list)  # clave -> exclusive rows that are not one of the planned mains
    planned = {main.get('ref') for main in mains.values() if main.get('ref')}
    for row in live_refs:
        if row['estado'] in EXCLUSIVE and row['supplier_id'] is None and row['pk'] not in planned:
            held[row['clave']].append(row)
    by_clave = defaultdict(list)
    for main in mains.values():
        by_clave[main['clave']].append(main)
    ids = set()
    candidates = {}
    for key, group_mains in by_clave.items():
        fresh = [main for main in group_mains if main['action'] != 'keep']
        if not fresh:
            continue
        members = {}
        for main in fresh:
            for other in group_mains:
                if other['line'] != main['line'] and clashes(main['brand'], other['brand']):
                    members.setdefault(('main', main['line']), main)
                    members.setdefault(('main', other['line']), other)
            for row in held.get(key, ()):
                if row['part_id'] != main['line'] and clashes(main['brand'], row['brand']):
                    members.setdefault(('main', main['line']), main)
                    members.setdefault(('ref', row['pk']), row)
        if members:
            candidates[key] = members
            ids |= {member['line'] if kind == 'main' else member['part_id'] for (kind, _), member in members.items()}
    stats = line_stats(sorted(ids))
    ref_count = {pk: len(by_line.get(pk, ())) for pk in ids}
    groups, disputed = [], {}
    for key, members in sorted(candidates.items()):
        group_mains = [member for (kind, _), member in members.items() if kind == 'main']
        order = lambda m: keeper_key(lines[m['line']], stats, ref_count)
        # A main that was already there keeps its key (this run never changes it); then this run's mains in rule 3g order keep theirs
        # unless they clash with one kept before; the other lines' codes that clash with a kept new main wait in disputa.
        kept = sorted((m for m in group_mains if m['action'] == 'keep'), key=order)
        fresh_kept = []
        for main in sorted((m for m in group_mains if m['action'] != 'keep'), key=order):
            if any(clashes(main['brand'], other['brand']) for other in kept):
                keeper_sku = lines[next(o for o in kept if clashes(main['brand'], o['brand']))['line']]['sku']
                main['estado'], main['dispute_note'] = 'disputa', f'Código duplicado de {keeper_sku}: caso de unificación.'
            else:
                kept.append(main)
                fresh_kept.append(main)
        keeper = min(kept, key=order)
        rows = []
        for (kind, _), member in sorted(members.items(), key=lambda item: str(item[0])):
            line_id = member['line'] if kind == 'main' else member['part_id']
            rows.append({'line': str(line_id), 'sku': lines[line_id]['sku'], 'main': kind == 'main', 'code': member['code'],
                         'brand': member['brand'], 'reference': member.get('ref') if kind == 'main' else member['pk'],
                         'estado': member['estado']})
            if kind == 'ref' and member['estado'] in EXCLUSIVE:
                holder = next((m for m in fresh_kept if clashes(member['brand'], m['brand'])), None)
                if holder is not None:
                    note = f'El código {member["code"]} es el SKU {lines[holder["line"]]["sku"]}: caso de unificación.'
                    disputed[member['pk']] = {'note': note, 'before': {'estado': member['estado'], 'dispute_note': member['dispute_note']}}
                    rows[-1]['estado'] = 'disputa'
        groups.append({'clave': key, 'keeper': str(keeper['line']), 'keeper_sku': lines[keeper['line']]['sku'],
                       'lines': sorted({row['line'] for row in rows}), 'rows': rows})
    return groups, disputed


def shared_numbers(lines, mains, live_refs, groups, absorbed):
    """[{'lines', 'numbers', 'keeper'}]: the library numbers two or more catalog lines carry, grouped by the lines they share. absorbed:
    {line id: its keeper, or None} for the lines the merge hook merges: a number such a line carries is its keeper's after the merge
    (a line with no keeper given drops out)."""
    from ..oem_reference_models import OEMReference, PartOEMLink
    absorbed = absorbed if isinstance(absorbed, dict) else dict.fromkeys(map(str, absorbed or ()))
    by_text = {str(pk): pk for pk in lines}
    keeper_of = {by_text[pk]: by_text.get(keeper) for pk, keeper in absorbed.items() if pk in by_text}
    catalog = {pk for pk, line in lines.items() if live(line) and line['estado'] in ('publicada', 'retirada')
               and (pk not in keeper_of or keeper_of[pk] is not None)}

    def carrier(pk):  # the line that carries a number once the hook's merges are made
        return keeper_of.get(pk) or pk
    numbers = defaultdict(list)
    for pk, code, manufacturer in OEMReference.objects.values_list('pk', 'code', 'manufacturer').iterator(chunk_size=CHUNK):
        numbers[code].append((pk, manufacturer))
    carriers = defaultdict(set)  # library reference id -> lines
    by_sku = defaultdict(set)
    for pk in catalog:
        by_sku[compact(lines[pk]['sku'])].add(pk)
    for code, refs in numbers.items():
        for ref_id, _ in refs:
            carriers[ref_id] |= {carrier(pk) for pk in by_sku.get(code, set())}
    for row in live_refs:
        if row['part_id'] not in catalog:
            continue
        for ref_id, manufacturer in numbers.get(compact(row['code']), ()):
            if row['tipo'] != 'oem' or row['brand'] == manufacturer:
                carriers[ref_id].add(carrier(row['part_id']))
    for ref_id, part_id in PartOEMLink.objects.filter(source__in=['catalog', 'sku_name']).values_list('reference_id', 'part_id').iterator(chunk_size=CHUNK):
        if part_id in catalog:
            carriers[ref_id].add(carrier(part_id))
    code_of = {ref_id: code for code, refs in numbers.items() for ref_id, _ in refs}
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    shared = {ref_id: found for ref_id, found in carriers.items() if len(found) >= 2}
    for found in shared.values():
        first, *rest = sorted(found, key=str)
        for other in rest:
            parent[find(other)] = find(first)
    grouped = defaultdict(lambda: {'lines': set(), 'numbers': set()})
    for ref_id, found in shared.items():
        root = find(next(iter(found)))
        grouped[root]['lines'] |= found
        grouped[root]['numbers'].add(code_of[ref_id])
    from ..line_merge import kept_apart_among, separated_pairs
    merge_sets = [set(group['lines']) for group in groups]
    stats = line_stats(sorted({pk for group in grouped.values() for pk in group['lines']}))
    pairs = separated_pairs()  # reads only the pair columns: works before 0075 too
    out = []
    for group in grouped.values():
        members = {str(pk) for pk in group['lines']}
        if any(members <= merge_set for merge_set in merge_sets):
            continue  # a merge group (part B) decides these lines
        if pairs and len(kept_apart_among(members, pairs)) == len(members) * (len(members) - 1) // 2:
            continue  # every pair kept apart (a shared_oem_number case dismissed, Separar SKU): a person decided
        own = [pk for pk in group['lines'] if compact(lines[pk]['sku']) in group['numbers']]
        keeper = own[0] if len(own) == 1 else min(group['lines'], key=lambda pk: keeper_key(lines[pk], stats, {}))
        out.append({'lines': sorted(members), 'numbers': sorted(group['numbers']), 'keeper': str(keeper), 'keeper_sku': lines[keeper]['sku']})
    return sorted(out, key=lambda group: group['keeper_sku'])


def learned_codes(lines, mains, by_line):
    """[{mapping...}]: each SupplierCodeMapping with the live Reference of its line that has its code (compact, its brand first, then
    none), the line's planned main when that is it (main_of), or neither (an interno Reference is made)."""
    from .. import lines as line_module
    from ..matching_models import SupplierCodeMapping
    from ..brands import lookup
    mappings = list(SupplierCodeMapping.objects.select_related('supplier').order_by('pk'))
    canonical = line_module.canonical_ids({m.part_id for m in mappings})
    by_text = {str(pk): pk for pk in lines}
    out = []
    for mapping in mappings:
        part = by_text.get(canonical.get(str(mapping.part_id)))
        code = compact(mapping.code)
        brand = lookup(mapping.brand) if (mapping.brand or '').strip() else None
        rows = [row for row in by_line.get(part, ()) if compact(row['code']) == code and row['supplier_id'] in (None, mapping.supplier_id)]
        rows.sort(key=lambda row: (row['brand'] != (brand.nombre if brand else ''), bool(row['brand']), row['pk']))
        main = mains.get(part)
        main_of = str(part) if (not rows and main and main['action'] == 'create' and compact(main['code']) == code) else None
        out.append({'id': mapping.pk, 'line': str(part) if part else None, 'supplier': str(mapping.supplier_id), 'supplier_name': mapping.supplier.name,
                    'code': mapping.code, 'brand': brand.nombre if brand else '', 'date': mapping.created_at.date().isoformat(),
                    'reference': rows[0]['pk'] if rows else None, 'main_of': main_of})
    return out


# --- apply ----------------------------------------------------------------------------------------------------------------------------

def merge_hook():
    """Part B's merge hook (mall.line_merge.s2b_compact_duplicates), or None while it is not there:

        hook(groups, *, apply, actor, run, log) -> {'merged': [{'keeper', 'absorbed': [line ids], 'merge': id}], 'cases': [ids],
                                                    'absorbed': [line ids], 'refused': [{'clave', 'reason'}]}

    groups as merge_groups returns them (rows: line, sku, main, code, brand, reference: the id, None for a main the dry run has not
    made). apply=False plans only (read-only); apply=True runs inside this run's transaction, after the mains are written, and appends
    its own manifest lines ({**run, 'hook': True, 'model', 'id', 'action', 'before', 'after'}) to log; --rollback hands those lines
    back to mall.line_merge.s2b_rollback(log=, run=, lines=) before it undoes its own."""
    from django.utils.module_loading import import_string
    try:
        return import_string(MERGE_HOOK)
    except ImportError:
        return None


def merge_rollback_hook():
    from django.utils.module_loading import import_string
    try:
        return import_string(MERGE_ROLLBACK_HOOK)
    except ImportError:
        return None


def write(result, log, run, user, hook=None):
    """Write the plan in bulk. Returns the counts written."""
    from django.db.models import F
    from django.utils import timezone
    from .. import references
    from ..matching_engine import part_row, save_case
    from ..matching_models import MatchingCase
    from ..models import Part, Reference, ReferenceSource
    lines, mains, now = result['lines'], result['mains'], timezone.now()
    counts = Counter()
    # The other lines' codes a new main takes the key of wait in disputa first, so no key is ever held twice (reference_one_live_line).
    for pk, entry in result['disputed'].items():
        Reference._base_manager.filter(pk=pk, estado__in=EXCLUSIVE).update(estado='disputa', dispute_note=entry['note'], version=F('version') + 1,
                                                                          updated_at=now)
        log.append({**run, 'model': 'mall.Reference', 'id': pk, 'action': 'update', 'role': 'disputa', 'before': recorded(entry['before']),
                    'after': {'estado': 'disputa', 'dispute_note': entry['note']}})
        counts['disputa'] += 1

    def reuse(pk, main):
        before = main['before']
        after = {'is_main': True, 'brand': main['brand'], 'tipo': main['tipo'], 'ref_type': REF_TYPE_OF[main['tipo']], 'code': main['code'],
                 'printed_forms': list(before['printed_forms'] or []), 'estado': main['estado'], 'dispute_note': main.get('dispute_note', before['dispute_note'])}
        if before['code'] != main['code'] and before['code'] not in after['printed_forms']:
            after['printed_forms'] = [*after['printed_forms'], before['code']][:references.MAX_PRINTED]
        changed = {field: value for field, value in after.items() if before.get(field) != value}
        Reference._base_manager.filter(pk=main['ref']).update(**changed, version=F('version') + 1, updated_at=now)
        log.append({**run, 'model': 'mall.Reference', 'id': main['ref'], 'action': 'update', 'role': 'main', 'line': str(pk),
                    'before': recorded({field: before.get(field) for field in changed}), 'after': recorded(changed)})
        counts['mains_reused'] += 1

    reused = [(pk, main) for pk, main in mains.items() if main['action'] == 'reuse']
    for pk, main in reused:  # a reused row that waits in disputa gives up its key before a new main takes it
        if main['estado'] == 'disputa':
            reuse(pk, main)
    built = []
    for pk, main in mains.items():
        if main['action'] == 'create':
            ref = references.build(pk, main['code'], main['brand'], tipo=main['tipo'], estado=main['estado'], actor=user,
                                   dispute_note=main.get('dispute_note', ''))
            ref.is_main = True
            built.append((pk, ref))
    references.bulk_create([ref for _, ref in built], actor=user, serialized=True)  # under the freeze and the matching lock
    for pk, ref in built:
        mains[pk]['ref'] = ref.pk
        log.append({**run, 'model': 'mall.Reference', 'id': ref.pk, 'action': 'create', 'role': 'main', 'line': str(pk),
                    'after': recorded({'code': ref.code, 'brand': ref.brand, 'tipo': ref.tipo, 'estado': ref.estado, 'is_main': True})})
    counts['mains_created'] = len(built)
    for pk, main in reused:
        if main['estado'] != 'disputa':
            reuse(pk, main)
    by_tipo = defaultdict(list)
    for pk, change in result['tipos'].items():
        by_tipo[change['after']['tipo']].append(pk)
        log.append({**run, 'model': 'mall.Part', 'id': str(pk), 'action': 'update', 'before': change['before'], 'after': change['after']})
    for tipo, pks in by_tipo.items():
        for start in range(0, len(pks), CHUNK):
            counts['lines_tipo'] += Part._base_manager.filter(pk__in=pks[start:start + CHUNK]).update(tipo=tipo, is_OEM=tipo == 'OEM')
    hooked = {}
    if hook is not None and result['groups']:
        for group in result['groups']:
            for row in group['rows']:
                if row['main'] and row['reference'] is None:
                    row['reference'] = mains[uuid.UUID(row['line'])]['ref']
        hooked = hook(result['groups'], apply=True, actor=user, run=run, log=log) or {}
        result['hook'] = hooked
    absorbed = {str(pk) for pk in hooked.get('absorbed') or []}
    for group in result['shared']:
        if set(group['lines']) & absorbed:
            continue
        digest = hashlib.sha256('|'.join(group['lines']).encode()).hexdigest()[:24]
        existing = MatchingCase.objects.filter(key=f'catalog:shared:{digest}').first()
        if existing is not None and existing.status not in ('review', 'unmatched'):
            counts['shared_decided'] += 1  # a person decided it (a catch-up run): never reopened here
            continue
        rows = {pk: part_row(part) for pk, part in ((str(p.pk), p) for p in Part._base_manager.filter(pk__in=group['lines']).prefetch_related('codes'))}
        sources = [rows[pk] for pk in group['lines'] if pk != group['keeper']]
        case = save_case(key=f'catalog:shared:{digest}', kind='catalog', sources=sources, candidates=[rows[group['keeper']]],
                         target=group['keeper_sku'], method=SHARED_METHOD,
                         reason=(f'Estos SKU llevan los mismos números OEM ({", ".join(group["numbers"][:6])}'
                                 f'{"…" if len(group["numbers"]) > 6 else ""}). Si son el mismo repuesto, agrúpalos en {group["keeper_sku"]}; '
                                 'si no, mantenlos separados.'))
        if existing is None:  # only a case this run created is its rollback's to delete
            log.append({**run, 'model': 'matching.MatchingCase', 'id': case.pk, 'action': 'create', 'after': {'key': case.key, 'method': SHARED_METHOD}})
            counts['shared_cases'] += 1
        else:
            counts['shared_kept'] += 1
    sources, internos = [], []
    for mapping in result['mappings']:
        ref_id = mapping['reference'] or (mains[uuid.UUID(mapping['main_of'])]['ref'] if mapping['main_of'] else None)
        citation = f"Código aprendido · {mapping['supplier_name']} · {mapping['date']}"
        detail = {'legacy': 'SupplierCodeMapping', 'mapping': mapping['id'], 'supplier': mapping['supplier'], 'code': mapping['code'],
                  'brand': mapping['brand']}
        if ref_id is None:
            if mapping['line'] is None:
                continue
            ref = references.build(uuid.UUID(mapping['line']), mapping['code'], mapping['brand'], tipo='interno', supplier=mapping['supplier'],
                                   actor=user)
            internos.append((mapping, ref, citation, detail))
            continue
        if not ReferenceSource.objects.filter(reference_id=ref_id, kind=MAPPING_KIND, citation=citation).exists():
            sources.append(ReferenceSource(reference_id=ref_id, kind=MAPPING_KIND, citation=citation, detail=detail, created_by=user))
    held = set(Reference.objects.filter(tipo='interno', estado__in=EXCLUSIVE).values_list('clave', 'brand', 'supplier_id'))
    fresh = []
    for mapping, ref, citation, detail in internos:
        key = (references.clave(ref.code), ref.brand, uuid.UUID(mapping['supplier']))
        if key in held:
            continue
        held.add(key)
        fresh.append((ref, citation, detail))
    references.bulk_create([ref for ref, _, _ in fresh], actor=user, serialized=True)
    for ref, citation, detail in fresh:
        log.append({**run, 'model': 'mall.Reference', 'id': ref.pk, 'action': 'create', 'role': 'interno', 'line': str(ref.part_id),
                    'after': recorded({'code': ref.code, 'brand': ref.brand, 'tipo': 'interno', 'supplier': ref.supplier_id})})
        sources.append(ReferenceSource(reference_id=ref.pk, kind=MAPPING_KIND, citation=citation, detail=detail, created_by=user))
    ReferenceSource.objects.bulk_create(sources, batch_size=500)
    for source in sources:
        log.append({**run, 'model': 'mall.ReferenceSource', 'id': source.pk, 'action': 'create',
                    'after': recorded({'reference_id': source.reference_id, 'kind': source.kind, 'citation': source.citation})})
    counts['sources'], counts['internos'] = len(sources), len(fresh)
    return dict(counts)


# --- rollback -------------------------------------------------------------------------------------------------------------------------

def applied_lines():
    """{(model, id): [lines]} of what the applies on this database did and no rollback has put back, oldest first."""
    rows, database = defaultdict(list), current_database()
    for line in manifest_lines():
        if line.get('stage') != STAGE or line.get('database') != database or not line.get('model') or line.get('hook'):
            continue  # the merge hook's own lines (hook: true) are its rollback's
        key, action = (line['model'], str(line['id'])), line.get('action')
        if action in ('update', 'create'):
            rows[key].append(line)
        elif action in ('rollback', 'delete'):
            rows.pop(key, None)
    return rows


def first_apply_at():
    """When the first apply on this database started (its manifest's run line), or None."""
    from django.utils.dateparse import parse_datetime
    times = [line.get('at') for line in manifest_lines() if line.get('stage') == STAGE and line.get('database') == current_database()
             and line.get('action') == 'run' and line.get('mode') == 'apply' and line.get('at')]
    return parse_datetime(min(times)) if times else None


def undo(log, run, *, landed_at=None):
    """Put back what the applies on this database did: {'restored', 'deleted', 'changed': [...], 'hook': ..., 'purged', 'unmarked'}.

    Then the mains the S2b code made outside the manifest (create_line, the Part.save shim, ensure_mains, Separar, set_main): the code
    before S2b would list them as alternos. A main created since S2b landed (landed_at, else the first apply's start) whose code is
    its line's codigo and that nothing names as its replacement is deleted (mall.references.purge; before S3 nothing else hangs from a
    Reference); every other main left becomes a plain Reference again (is_main false). The References of a merge the hook could not
    undo are left as they are (listed)."""
    from django.db.models import F
    from django.utils import timezone
    from .. import references
    from ..matching_models import MatchingCase
    from ..models import Part, Reference, ReferenceSource
    chains = applied_lines()
    by_model = defaultdict(dict)
    for (model, pk), chain in chains.items():
        by_model[model][pk] = chain
    restored, deleted, changed, now = 0, 0, [], timezone.now()
    hook_result = None
    rollback_hook = merge_rollback_hook()
    if rollback_hook is not None:
        hook_result = rollback_hook(log=log, run=run, lines=[line for line in manifest_lines() if line.get('stage') == STAGE
                                                             and line.get('database') == current_database() and line.get('hook')])
    kept = {int(pk) for pk in (hook_result or {}).get('kept') or []}  # a merge left in place keeps what it recorded
    for pk in sorted(kept & {int(pk) for pk in by_model.get('mall.Reference', {})}):
        changed.append(f'referencia {pk}: es de una unificación que se deja, se deja')
    for pk, chain in by_model.get('mall.ReferenceSource', {}).items():
        source = ReferenceSource.objects.filter(pk=pk).first()
        if source is not None:
            source.delete()
            deleted += 1
            log.append({**run, 'model': 'mall.ReferenceSource', 'id': int(pk), 'action': 'delete', 'before': chain[-1]['after']})
    for pk, chain in by_model.get('matching.MatchingCase', {}).items():
        case = MatchingCase.objects.filter(pk=pk, status='review').first()
        if case is None:
            changed.append(f'caso {pk}: ya se decidió, se deja')
            continue
        case.delete()
        deleted += 1
        log.append({**run, 'model': 'matching.MatchingCase', 'id': pk, 'action': 'delete', 'before': chain[-1]['after']})
    refs = {pk: chain for pk, chain in by_model.get('mall.Reference', {}).items() if int(pk) not in kept}
    created = [int(pk) for pk, chain in refs.items() if chain[0]['action'] == 'create']
    if created:
        free = set(Reference._base_manager.filter(pk__in=created, reemplaza__isnull=True).values_list('pk', flat=True))
        for pk in sorted(set(created) - free):
            changed.append(f'referencia {pk}: otra la nombra como reemplazo, se deja')
        purged = references.purge(sorted(free))
        deleted += purged
        for pk in sorted(free):
            log.append({**run, 'model': 'mall.Reference', 'id': pk, 'action': 'delete', 'before': refs[str(pk)][0]['after']})
    for pk, chain in sorted(refs.items(), key=lambda item: int(item[0])):
        updates = [line for line in chain if line['action'] == 'update']
        if chain[0]['action'] == 'create' or not updates:
            continue
        fields = list(dict.fromkeys(field for line in updates for field in line['after']))
        row = Reference._base_manager.filter(pk=int(pk)).values(*fields).first()
        if row is None:
            continue
        now_values = recorded(row)
        last = {field: next(line['after'][field] for line in reversed(updates) if field in line['after']) for field in fields}
        first = {field: next(line['before'][field] for line in updates if field in line['before']) for field in fields}
        if now_values == first:
            continue
        if now_values != last:
            changed.append(f'referencia {pk}: cambió después de aplicar')
            continue
        Reference._base_manager.filter(pk=int(pk)).update(**first, version=F('version') + 1, updated_at=now)
        restored += 1
        log.append({**run, 'model': 'mall.Reference', 'id': int(pk), 'action': 'rollback', 'before': now_values, 'after': first})
    for pk, chain in by_model.get('mall.Part', {}).items():
        row = Part._base_manager.filter(pk=pk).values(*PART_FIELDS).first()
        if row is None:
            continue
        first, last = chain[0]['before'], chain[-1]['after']
        if recorded(row) == first:
            continue
        if recorded(row) != last:
            changed.append(f'línea {pk}: su tipo cambió después de aplicar')
            continue
        Part._base_manager.filter(pk=pk).update(**first)
        restored += 1
        log.append({**run, 'model': 'mall.Part', 'id': pk, 'action': 'rollback', 'before': last, 'after': first})
    purged, unmarked = code_mains(log, run, landed_at=landed_at or first_apply_at(), skip=kept, changed=changed)
    return {'restored': restored, 'deleted': deleted, 'changed': changed, 'hook': hook_result, 'purged': purged, 'unmarked': unmarked}


def code_mains(log, run, *, landed_at, skip, changed):
    """The mains left once the manifest is undone (made by the S2b code): (deleted, unmarked), see undo()."""
    from django.db.models import F
    from django.utils import timezone
    from .. import references
    from ..models import Reference
    rows = list(Reference._base_manager.filter(is_main=True).exclude(pk__in=skip).select_related('part').order_by('pk'))
    doomed = [row for row in rows if landed_at is not None and row.created_at is not None and row.created_at >= landed_at
              and row.code == row.part.sku]
    named = set(Reference._base_manager.filter(reemplazado_por__in=[row.pk for row in doomed]).values_list('reemplazado_por', flat=True))
    doomed = [row for row in doomed if row.pk not in named]
    for pk in sorted(named):
        changed.append(f'referencia {pk}: principal que otra nombra como reemplazo, queda como referencia')
    deleted = references.purge([row.pk for row in doomed])
    for row in doomed:
        log.append({**run, 'model': 'mall.Reference', 'id': row.pk, 'action': 'delete', 'role': 'code_main', 'line': str(row.part_id),
                    'before': recorded({'code': row.code, 'brand': row.brand, 'tipo': row.tipo, 'estado': row.estado, 'is_main': True,
                                        'created_at': row.created_at})})
    gone = {row.pk for row in doomed}
    left = [row.pk for row in rows if row.pk not in gone]
    for start in range(0, len(left), CHUNK):
        Reference._base_manager.filter(pk__in=left[start:start + CHUNK]).update(is_main=False, version=F('version') + 1, updated_at=timezone.now())
    for pk in left:
        log.append({**run, 'model': 'mall.Reference', 'id': pk, 'action': 'rollback', 'role': 'code_main', 'before': {'is_main': True},
                    'after': {'is_main': False}})
    return deleted, len(left)


# --- run ------------------------------------------------------------------------------------------------------------------------------

ROWS = (('lineas', 'líneas vivas'), ('reusar', 'principal reusada'), ('crear', 'principal nueva'), ('oem', 'tipo oem'),
        ('aftermarket', 'tipo aftermarket'), ('aftermarket_marca', '  con marca'), ('otro', 'tipo otro'),
        ('grupos_duplicados', 'grupos de duplicados'), ('lineas_duplicadas', '  líneas'), ('grupos_compartidos', 'grupos OEM compartidos'),
        ('numeros_compartidos', '  números'), ('aprendidos', 'códigos aprendidos'), ('internos', '  internos nuevos'),
        ('is_oem_cambia', 'is_OEM cambia'))


def print_summary(stdout, mode, result, writes=None):
    title = {'apply': 'Aplicado', 'dry_run': 'Simulación'}[mode]
    final = result['final']
    stdout.write(f'{title} · S2b referencia principal por línea\n')
    if not (result['schema']['references'] and result['schema']['tipo']):
        stdout.write('  (sin 0073/0075 en esta base: simulación sobre las columnas de antes; los números reales salen del ensayo)\n')
    stdout.write(f"  {'':26} {'esperado':>9} {'resultado':>10}\n")
    for key, label in ROWS:
        stdout.write(f"  {label:26} {EXPECTED[key]:>9,} {final.get(key, 0):>10,}\n")
    if final.get('ya_tenia'):
        stdout.write(f"  líneas que ya tenían principal: {final['ya_tenia']:,}\n")
    stdout.write('  por regla: ' + ' · '.join(f'{rule} {final.get(f"regla_{rule}", 0):,}' for rule in range(1, 7)) + '\n')
    for label, key in (('OEM sin fabricante (regla 3)', 'oem_sin_fabricante'), ('Marca de sufijo sin identificar (regla 5, para Marcas)', 'marca_sin_identificar')):
        skus = result['listed'].get(key, [])
        if skus:
            stdout.write(f'  {label}: {len(skus)} · ' + ', '.join(skus[:12]) + (' …' if len(skus) > 12 else '') + '\n')
    if result['groups']:
        hook = 'parte B decide' if merge_hook() else 'sin el hook de la parte B: quedan en disputa y listados'
        stdout.write(f"  Grupos de duplicados ({len(result['groups'])}, {hook}):\n")
        for group in result['groups'][:15]:
            stdout.write(f"    {group['clave']}: queda {group['keeper_sku']} · " + ', '.join(f"{row['code']} en {row['sku']}" for row in group['rows']
                                                                                      if row['line'] != group['keeper']) + '\n')
    decided = result.get('hook') or result.get('hook_plan') or {}
    if decided.get('merged') or decided.get('refused'):
        merged, refused = decided.get('merged') or [], decided.get('refused') or []
        verb = 'Unificados solos' if mode == 'apply' else 'Se unificarían solos'
        stdout.write(f"  {verb} (OD4, compact_duplicate, Separar los deshace): {len(merged)} grupos · "
                     f"{sum(len(group['absorbed']) for group in merged)} líneas\n")
        for group in merged[:15]:
            stdout.write(f"    {group['keeper_sku']} ← {', '.join(group['skus'])}\n")
        stdout.write(f"  Casos compact_duplicate (en disputa hasta que se decidan): {len(refused)}\n")
        for group in refused[:15]:
            stdout.write(f"    {group['keeper_sku']} ← {', '.join(group['skus'])} · {group['guard']}: {group['reason']}\n")
    if result['shared']:
        stdout.write(f"  Números OEM en varias líneas ({len(result['shared'])} casos shared_oem_number):\n")
        for group in result['shared'][:10]:
            stdout.write(f"    {group['keeper_sku']} ← {len(group['lines']) - 1} línea(s) · {', '.join(group['numbers'][:4])}\n")
    if writes:
        stdout.write('  Escrito: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(writes.items())) + '\n')


def report(result):
    return {'expected': EXPECTED, 'result': dict(result['final']), 'groups': result['groups'], 'disputed': len(result['disputed']),
            'shared': result['shared'], 'mappings': result['mappings'], 'listed': result['listed'], 'absorbed': result['absorbed'],
            'tipos': len(result['tipos']), 'compact': result.get('hook_plan') or {}}


def run(*, apply=False, rollback=False, username=None, report_path=None, stdout=None, hook=None, landed_at=None):
    """Plan, and with apply write it, or with rollback undo the applies on this database (landed_at: when the S2b code landed, for
    the mains it made; the first apply's start when None). None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from ..matching_queue import matching_lock, matching_work
    from ..models import Part
    stdout = stdout or sys.stdout
    hook = hook if hook is not None else merge_hook()
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
        if not (state['references'] and state['tipo']):
            stdout.write('Faltan las migraciones 0073–0075 en esta base: aplícalas primero (la simulación funciona sin ellas).\n')
            return None
        if rollback and tightened():
            stdout.write('0076_line_main_reference_checks está aplicada: reviértela primero (python manage.py migrate mall 0075_line_main_reference).\n')
            return None
    run_line = {'stage': STAGE, 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}

    def go():
        log = []
        with transaction.atomic():
            if rollback:
                result = {'mode': 'rollback', **undo(log, run_line, landed_at=landed_at)}
            else:
                if apply:  # every line locked, so the plan and its writes see the same rows
                    list(Part._base_manager.select_for_update().order_by('pk').values_list('pk', flat=True))
                planned = plan(hook=hook, actor=user.username if user else '')  # the hook plans the merges in both modes
                result = {'mode': 'apply' if apply else 'dry_run', **report(planned)}
                writes = None
                if apply:
                    writes = result['written'] = write(planned, log, run_line, user, hook=hook)
                    result['hook'] = result['compact'] = planned.get('hook')
                print_summary(stdout, result['mode'], planned, writes)
                if report_path:
                    with open(report_path, 'w', encoding='utf-8') as handle:
                        json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
                    stdout.write(f'Informe: {report_path}\n')
            if log:
                result['manifest'] = str(write_manifest([{**run_line, 'action': 'run', 'mode': result['mode'], 'rows': len(log)}, *log]))
        result['rows_logged'] = len(log)
        if rollback:
            stdout.write(f"Rollback · S2b: {result['restored']} filas restauradas · {result['deleted']} borradas · "
                         f"{len(result['changed'])} cambiadas después (se dejan)\n")
            hooked = result.get('hook') or {}
            stdout.write(f"  unificaciones separadas {hooked.get('split', 0)} · casos borrados {hooked.get('cases', 0)} · principales del código "
                         f"borradas {result.get('purged', 0)} · desmarcadas {result.get('unmarked', 0)}\n")
            for line in hooked.get('changed') or []:
                stdout.write(f'  {line}\n')
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
    parser = argparse.ArgumentParser(description='S2b: la referencia principal de cada línea, su tipo, los duplicados y los números compartidos.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los cambios sin escribir nada (también antes de 0073/0075).')
    mode.add_argument('--apply', action='store_true', help='Escribe todo en una transacción (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base (requiere el congelamiento en vivo).')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply; opcional con --rollback).')
    parser.add_argument('--report', help='Escribe en esta ruta (JSON) los grupos, los casos, los códigos aprendidos y las listas.')
    parser.add_argument('--json', action='store_true', help='Imprime también el resultado en JSON.')
    parser.add_argument('--landed-at', help='Con --rollback: cuándo aterrizó el código de S2b (ISO 8601, T_D5); las principales que el código '
                                            'creó desde entonces se borran. Por defecto, el inicio del primer --apply.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .guard import require_freeze
    require_freeze(options.apply or options.rollback)
    stdout = stdout or sys.stdout
    landed_at = None
    if options.landed_at:
        from django.utils.dateparse import parse_datetime
        landed_at = parse_datetime(options.landed_at)
        if landed_at is None:
            stdout.write(f'--landed-at no es una fecha ISO 8601: {options.landed_at}\n')
            return 1
    result = run(apply=options.apply, rollback=options.rollback, username=options.user, report_path=options.report, stdout=stdout,
                 landed_at=landed_at)
    if result is not None and options.json:
        stdout.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
    return 0 if result is not None else 1


if __name__ == '__main__':
    sys.exit(main())
