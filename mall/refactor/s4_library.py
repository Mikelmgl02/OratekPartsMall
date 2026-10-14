"""Stage S4 of the inventory refactor: every number of the OEM library becomes a Reference, the numbers no product carries become lines
in referencia, and the library's sources, aftermarket codes, tombstones and SKU links hang from those References.

    python -m mall.refactor.s4_library --dry-run|--apply --user USERNAME|--rollback [--report PATH] [--json]

Carriers (plan §3 S4 step 1; catalog links are ignored, migration step 4). A live line (any estado but unificada) carries a number
when it holds a live Reference with one of the number's claves (its compact form's, or a printed spelling's) under the number's maker,
no brand, or a brand of the maker's familia (KAIYI and CHERY, LEXUS and TOYOTA: mall.brands), or when an sku_name link ties it to the
number. The name links link_oem_numbers would still add (82 on 2026-10-14, numbers Motion Motors declared) are not carriers: those
numbers go to lines of their own in referencia, estado declarado, and rule 5b reviews them (S6); the report lists them.

One Reference per OEMReference (step 3): uid = the OEMReference's id, brand = its manufacturer, tipo oem, code = its first printed
form (the others in printed_forms), estado from its status (verified → verificado, declared → declarado, inferred → inferido,
disputed → disputa with its note), reemplazado_por the Reference of the number that replaces it, notes, created_by and created_at, and
detail {description, applications, part_type, system, family}. By carrier:
- a Reference (clave, maker) on a carrier exists (the OEM alternos of the catalog, 641 on 2026-10-13): it takes the uid (S2a's
  uids were never exposed) and the detail, the number's spellings, its notes when it has none, its status when that changes no key, and
  tipo oem (its line's tipo follows when it is the main);
- otherwise the keeper carrier gets a new Reference (clave, maker): the line holding the key with no brand when one does (I5: no other
  line may hold it under the maker), else the line whose own codigo is the number, else rule 3g (publicada first, stock, order lines,
  References, the spelling without a trailing - or _). A brandless main keeps its brand; a familia brand stays as it is beside it.
  These Reference carry detail.origen = 'biblioteca' (library data: the OEM finder's snapshot and the cases' rows leave them out);
- no carrier: one line in referencia per (clave, familia root) (step 2): codigo the main number's printed form, origen biblioteca,
  tipo OEM, estado referencia (active False), description, aplicaciones_texto and the subgrupo its part type maps to (A6, OD9: an
  unmapped text stays in field_sources['subgrupo']); its main is the familia root's number (else the first maker's), and the other
  makers' numbers are References of the same line (49 lines hold two on 2026-10-13).
A number two or more lines carry (66) goes to its keeper, and its carriers get a legacy catalog case (method shared_oem_number) unless
S2b's case for them, or one holding all of them, is there (OD16 waived: S4 merges nothing).
SKU lines carrying a number take its subgrupo and aplicaciones_texto where theirs are blank (field_sources biblioteca) (step 3).
Sources (step 4): each OEMReferenceSource is upserted as a ReferenceSource on (reference, kind, citation), detail merged as the
library merged it, the kind per A4 (catalog_approval citing "Declarado por" is proveedor), the old kind in detail.legacy_kind; the OEM
finder's rows meet the buscador sources S2a made of the same algo: texts.
Re-point (step 5): OEMCrossReference, OEMCrossReferenceRetired and PartOEMLink take ref_id = the Reference whose uid is their
reference_id. New subgrupos (step 6) come with their templates (mall.technical_data.part_type_for).

It runs before landing (the code before S4 filters lines by estado, and only sees the new OEM References in its code lists: the
expected codes diff) and again after, as the catch-up; re-running changes nothing new. It writes in bulk (no signals) under the
matching worker's lock, in one transaction; on the live database --apply and --rollback need the S0.11 guard (mall.refactor.guard).
Every row it creates or changes goes, with its values before, to mall.refactor.output.manifest_path('s4') before the transaction
commits. --apply needs 0075/0076 (S2b's mains) and 0079 applied. The dry run works without them (live before S2b): the mains S2b
would make are simulated from mall.refactor.s2b_main_refs.plan (its compact-duplicate merges left out), every aftermarket code counts
as to re-point, and nothing is read from columns the database lacks. A rehearsal copy gives the real numbers.

--rollback (within the 24 hours the Biblioteca stays read-only, OD22): the cases it opened (still in review) and the sources it made
are deleted, the rows it re-pointed lose ref_id again, the References it changed take their values back, and the References and lines
it created are deleted when nothing points at them (anything else is listed and left). Reverse 0080 first: it refuses while ref_id
is NOT NULL.
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

STAGE = 's4'
# Plan §3 S4 (2026-10-13 simulation, before the GMB fix took 208 numbers away and added 46; the live counts move with the catalog).
EXPECTED = {'numeros': 15880, 'existente': 641, 'principal_sin_marca': 26, 'familia': 2, 'nombre_sku': 209, 'varios_portadores': 66,
            'sin_portador': 14935, 'lineas_referencia': 14886, 'lineas_dos_numeros': 49, 'referencias_nuevas': 15180,
            'fuentes': 15893, 'nombre_pendiente': 82}
ESTADO_OF = {'verified': 'verificado', 'declared': 'declarado', 'inferred': 'inferido', 'disputed': 'disputa'}
EXCLUSIVE = ('verificado', 'declarado', 'inferido')
LIVE = ('verificado', 'declarado', 'inferido', 'disputa')
CATALOG = ('publicada', 'retirada')
REF_FIELDS = ('uid', 'detail', 'tipo', 'ref_type', 'printed_forms', 'notes', 'estado', 'dispute_note', 'reemplazado_por_id')
LINE_FILL = ('category', 'subcategory', 'part_type_id', 'aplicaciones_texto', 'field_sources')
REPOINTED = (('mall.OEMCrossReference', 'mall_oemcrossreference'), ('mall.OEMCrossReferenceRetired', 'mall_oemcrossreferenceretired'),
             ('mall.PartOEMLink', 'mall_partoemlink'))
SHARED_METHOD = 'shared_oem_number'
ORIGIN = 'biblioteca'
CHUNK = 2000


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


def columns(table):
    from django.db import connection
    with connection.cursor() as cursor:
        return {column.name: column for column in connection.introspection.get_table_description(cursor, table)}


def schema():
    """{'tipo': 0075 applied (Part.tipo), 'ref_id': 0079 applied, 'mains': S2b's mains made, 'tightened': 0080 applied (ref_id NOT
    NULL)}."""
    from ..models import Reference
    links = columns('mall_oemcrossreference')
    ref_id = links.get('ref_id')
    return {'tipo': 'tipo' in columns('mall_part'), 'ref_id': ref_id is not None, 'tightened': ref_id is not None and not ref_id.null_ok,
            'mains': Reference._base_manager.filter(is_main=True).exists()}


# --- reading --------------------------------------------------------------------------------------------------------------------------

def compact(code):
    return re.sub(r'[^A-Z0-9]', '', (code or '').upper())


def is_live(line):
    return not line['merged_into_id'] and line['estado'] != 'unificada'


def line_rows():
    """{id: line} of every line, read only through columns the database has before S2b (S1's)."""
    from ..models import Part
    fields = ['pk', 'sku', 'estado', 'active', 'merged_into_id', 'description', 'part_type_id', 'category', 'subcategory',
              'aplicaciones_texto', 'field_sources', 'is_OEM']
    return {row['pk']: row for row in Part._base_manager.order_by('pk').values(*fields).iterator(chunk_size=CHUNK)}


def reference_rows():
    """Every Reference's values (S2a's columns), retired ones included."""
    from ..models import Reference
    fields = ['pk', 'part_id', 'code', 'clave', 'brand', 'tipo', 'ref_type', 'estado', 'is_main', 'supplier_id', 'uid', 'detail',
              'printed_forms', 'notes', 'dispute_note', 'reemplazado_por_id']
    return list(Reference._base_manager.order_by('pk').values(*fields).iterator(chunk_size=CHUNK))


def simulated_mains(lines, refs):
    """Before S2b's backfill (the live database before its window), the mains it would make (mall.refactor.s2b_main_refs.plan, its
    compact-duplicate merges left out): reused rows become mains in memory, new ones are added as rows without an id. Returns the
    virtual rows added."""
    from . import s2b_main_refs
    planned = s2b_main_refs.plan(hook=None)
    by_pk = {row['pk']: row for row in refs}
    added = []
    for line_id, main in planned['mains'].items():
        if main['action'] == 'keep':
            continue
        if main['action'] == 'reuse' and main.get('ref') in by_pk:
            row = by_pk[main['ref']]
            row.update(is_main=True, brand=main['brand'], tipo=main['tipo'], code=main['code'], estado=main['estado'])
            continue
        added.append({'pk': f'virtual:{line_id}', 'part_id': line_id, 'code': main['code'], 'clave': main['clave'], 'brand': main['brand'],
                      'tipo': main['tipo'], 'ref_type': '', 'estado': main['estado'], 'is_main': True, 'supplier_id': None, 'uid': None,
                      'detail': {}, 'printed_forms': [], 'notes': '', 'dispute_note': main.get('dispute_note', ''),
                      'reemplazado_por_id': None, 'virtual': True})
    refs.extend(added)
    return added


def library():
    from ..oem_reference_models import OEMReference
    fields = ['pk', 'manufacturer', 'code', 'printed_forms', 'system', 'family', 'part_type', 'description', 'applications', 'status',
              'dispute_note', 'superseded_by_id', 'notes', 'created_by_id', 'created_at']
    return list(OEMReference.objects.order_by('manufacturer', 'code').values(*fields).iterator(chunk_size=CHUNK))


def library_sources():
    from ..oem_reference_models import OEMReferenceSource
    by_number = defaultdict(list)
    for row in OEMReferenceSource.objects.order_by('pk').values('pk', 'reference_id', 'kind', 'citation', 'detail', 'created_by_id',
                                                                 'created_at').iterator(chunk_size=CHUNK):
        by_number[row['reference_id']].append(row)
    return by_number


def name_links():
    """{number id: [line ids]} of the sku_name links (their OEMReference: the column reference_id, there before and after 0079)."""
    from ..oem_reference_models import PartOEMLink
    found = defaultdict(list)
    for number, line in (PartOEMLink.objects.filter(source='sku_name').order_by('pk').values_list('legacy_reference_id', 'part_id')
                         .iterator(chunk_size=CHUNK)):  # ordered by id: the model's ordering names ref_id, which only 0079 adds
        if number:
            found[number].append(line)
    return found


def familia_roots(names):
    """{spelling: the nombre heading its familia} (mall.brands.familia_map), the normalized spelling for one that names no brand."""
    from ..brands import familia_map, normalize
    names = sorted({name for name in names if name})
    roots = {}
    for start in range(0, len(names), CHUNK):
        roots.update(familia_map(names[start:start + CHUNK]))
    return {name: roots.get(name) or normalize(name) for name in names}


def existing_sources():
    """{(reference id, kind, citation): (source id, detail)} of the ReferenceSources there are (S2a's, and a previous run's)."""
    from ..models import ReferenceSource
    return {(row[0], row[1], row[2]): (row[3], row[4]) for row in
            ReferenceSource.objects.values_list('reference_id', 'kind', 'citation', 'pk', 'detail').iterator(chunk_size=CHUNK)}


def part_types():
    from ..models import PartType
    return {(row[1], row[2]): row[0] for row in PartType.objects.values_list('pk', 'category', 'name')}


# --- plan -----------------------------------------------------------------------------------------------------------------------------

def number_claves(number):
    from .. import references
    return references.library_claves(number['code'], number['printed_forms'])


def first_form(number):
    return (number['printed_forms'] or [number['code']])[0]


def detail_of(number, *, origin=False):
    from .. import references
    detail = references.library_detail(number['description'], number['applications'], number['part_type'], number['system'], number['family'])
    return {**detail, 'origen': ORIGIN} if origin else detail


def keeper_key(line, stats, held, claves, brandless):
    from .. import references
    stock, orders = stats.get(line['pk'], (0, 0))
    return (line['pk'] not in brandless, references.clave(line['sku']) not in claves, line['estado'] != 'publicada', -stock, -orders,
            -held.get(line['pk'], 0), bool(re.search(r'[-_]$', line['sku'])), line['sku'], str(line['pk']))


def plan(state=None):
    """Everything the apply writes, read-only. Returns {'numbers', 'lines', 'updates', 'new_refs', 'fills', 'sources', 'cases',
    'part_types', 'pending', 'final', 'schema', ...}."""
    from .. import references
    from ..oem_reference import subgrupo_for
    from .s2b_main_refs import line_stats
    state = state or schema()
    lines = line_rows()
    refs = reference_rows()
    simulated = [] if state['mains'] else simulated_mains(lines, refs)
    numbers = library()
    roots = familia_roots({number['manufacturer'] for number in numbers} | {row['brand'] for row in refs})
    by_uid = {row['uid']: row for row in refs if row['uid'] is not None}
    held = Counter(row['part_id'] for row in refs if row['estado'] != 'retirada')
    index = defaultdict(list)  # clave -> live References (no supplier) on live lines
    for row in refs:
        if row['estado'] in LIVE and row['supplier_id'] is None and row['part_id'] in lines and is_live(lines[row['part_id']]):
            index[row['clave']].append(row)
    links = name_links()
    final, listed = Counter(), defaultdict(list)
    plans, unplaced = [], []
    final['numeros'] = len(numbers)
    multi = {}
    for number in numbers:
        maker, claves = number['manufacturer'], number_claves(number)
        root = roots.get(maker, maker)
        entry = {'id': number['pk'], 'maker': maker, 'code': number['code'], 'first': first_form(number), 'claves': sorted(claves),
                 'root': root, 'estado': ESTADO_OF.get(number['status'], 'declarado'), 'number': number}
        done = by_uid.get(number['pk'])
        if done is not None:
            entry.update(action='hecho', ref=done['pk'], line=done['part_id'])
            final['hecho'] += 1
            plans.append(entry)
            continue
        carrying = [row for key in claves for row in index.get(key, ()) if row['brand'] in ('', maker) or roots.get(row['brand']) == root]
        exact = sorted((row for row in carrying if row['brand'] == maker), key=lambda row: (row['estado'] not in EXCLUSIVE, str(row['pk'])))
        carriers = {row['part_id'] for row in carrying} | {line for line in links.get(number['pk'], ()) if line in lines and is_live(lines[line])}
        if len(carriers) >= 2:
            multi[number['pk']] = carriers
        if exact:
            row = exact[0]
            entry.update(action='existente', ref=row['pk'], line=row['part_id'], row=row)
            final['existente'] += 1
        elif carriers:
            entry.update(action='nueva_en_linea', carriers=sorted(carriers, key=str), carrying=carrying)
        else:
            entry['action'] = 'referencia'
            final['sin_portador'] += 1
            unplaced.append(entry)
        plans.append(entry)
    # Keepers of the numbers that go on a carrier, rule 3g needing the lines' stock and order lines.
    pending_keepers = [entry for entry in plans if entry['action'] == 'nueva_en_linea']
    stats = line_stats(sorted({line for entry in pending_keepers if len(entry['carriers']) > 1 for line in entry['carriers']}, key=str))
    for entry in pending_keepers:
        claves = set(entry['claves'])
        brandless = {row['part_id'] for row in entry['carrying'] if not row['brand'] and row['estado'] in EXCLUSIVE}
        keeper = min(entry['carriers'], key=lambda pk: keeper_key(lines[pk], stats, held, claves, brandless))
        own = [row for row in entry['carrying'] if row['part_id'] == keeper]
        if any(not row['brand'] for row in own):
            kind = 'principal_sin_marca' if any(row['is_main'] and not row['brand'] for row in own) else 'codigo_sin_marca'
        elif own:
            kind = 'familia'
        else:
            kind = 'nombre_sku'
        entry.update(line=keeper, kind=kind)
        final[kind] += 1
        elsewhere = [row for row in index.get(references.clave(entry['first']), ()) if not row['brand'] and row['estado'] in EXCLUSIVE
                     and row['part_id'] != keeper]
        if elsewhere:  # another line keeps the key with no brand: the number waits in disputa (never on a clean run)
            entry['dispute'] = f"El número {entry['maker']} {entry['code']} también lo lleva {lines[elsewhere[0]['part_id']]['sku']}: caso de unificación."
    # Numbers two or more lines carry: the keeper holds them (the line with its own (clave, maker) Reference, the codigo's line, or rule 3g).
    for entry in plans:
        if entry['id'] in multi and entry.get('line') in lines:
            final['varios_portadores'] += 1
            own = references.clave(lines[entry['line']]['sku']) in set(entry['claves'])
            final['varios_existente' if entry['action'] == 'existente' else 'varios_codigo_propio' if own else 'varios_regla_3g'] += 1
        if entry['action'] == 'existente' and entry['row']['is_main']:
            final['existente_principal'] += 1
    # Lines in referencia: one per (clave, familia root).
    groups = defaultdict(list)
    for entry in unplaced:
        groups[(references.clave(entry['first']), entry['root'])].append(entry)
    new_lines = []
    for (key, root), members in sorted(groups.items()):
        members.sort(key=lambda entry: (entry['maker'] != root, entry['maker'], entry['code']))
        main = members[0]
        texts = main['number']
        fields = references.library_line_fields(texts['description'], texts['part_type'], texts['applications'])
        line_key = f'{key}|{root}'
        new_lines.append({'key': line_key, 'sku': main['first'], 'main': main['id'], 'numbers': [entry['id'] for entry in members], **fields})
        for entry in members:
            entry.update(line=line_key, kind='referencia', is_main=entry is main)
        final['lineas_referencia'] += 1
        final['lineas_dos_numeros'] += len(members) > 1
    # What changes on the References there are, and which ones this run creates.
    updates, new_refs = {}, []
    for entry in plans:
        number = entry['number']
        if entry['action'] == 'existente':
            row = entry['row']
            after = {'uid': number['pk'], 'detail': {**(row['detail'] or {}), **detail_of(number)}}
            forms = list(dict.fromkeys([*(row['printed_forms'] or []), *[form for form in number['printed_forms'] if form != row['code']]]))[:20]
            if forms != (row['printed_forms'] or []):
                after['printed_forms'] = forms
            if row['tipo'] != 'oem':
                after.update(tipo='oem', ref_type='oem')
            if not (row['notes'] or '').strip() and (number['notes'] or '').strip():
                after['notes'] = number['notes']
            wanted = entry['estado']
            if wanted != row['estado'] and (wanted in EXCLUSIVE) == (row['estado'] in EXCLUSIVE) or wanted == 'disputa':
                after['estado'] = wanted
                after['dispute_note'] = (number['dispute_note'] or 'En disputa en la Biblioteca OEM.') if wanted == 'disputa' else ''
            entry['retyped_main'] = row['tipo'] != 'oem' and row['is_main']
            updates[row['pk']] = {'line': row['part_id'], 'virtual': bool(row.get('virtual')),
                                  'before': {field: row.get(field) for field in REF_FIELDS if field in after}, 'after': after}
        elif entry['action'] in ('nueva_en_linea', 'referencia'):
            estado, note = entry['estado'], number['dispute_note'] if entry['estado'] == 'disputa' else ''
            if entry.get('dispute'):
                estado, note = 'disputa', entry['dispute']
            if estado == 'disputa' and not (note or '').strip():
                note = 'En disputa en la Biblioteca OEM.'
            rest = [form for form in [*number['printed_forms'][1:], *([number['code']] if number['code'] != entry['first']
                                                                      and references.clave(entry['first']) != number['code'] else [])]
                    if form != entry['first']][:20]
            new_refs.append({'number': number['pk'], 'line': entry['line'], 'code': entry['first'], 'brand': entry['maker'], 'estado': estado,
                             'dispute_note': note, 'printed_forms': rest, 'notes': number['notes'] or '', 'is_main': bool(entry.get('is_main')),
                             'detail': detail_of(number, origin=True), 'created_by_id': number['created_by_id'],
                             'created_at': number['created_at']})
    final['referencias_nuevas'] = len(new_refs)
    final['referencias_actualizadas'] = len(updates)
    # Supersession: each number's Reference points at the Reference of the number that replaces it.
    supersede = {entry['id']: entry['number']['superseded_by_id'] for entry in plans if entry['number']['superseded_by_id']}
    final['reemplazos'] = len(supersede)
    # SKU lines (not in referencia) carrying numbers: their blank subgrupo and aplicaciones_texto from the library.
    fills = plan_fills(lines, plans, subgrupo_for)
    final['lineas_completadas'] = len(fills)
    # Sources.
    sources = plan_sources(plans, library_sources(), existing_sources())
    final['fuentes'] = len(sources['create']) + len(sources['merge'])
    final['fuentes_nuevas'], final['fuentes_unidas'] = len(sources['create']), len(sources['merge'])
    # Re-point.
    final.update(repoint_counts(state))
    # New subgrupos.
    known = part_types()
    wanted = {(line['category'], line['subcategory']) for line in new_lines if line.get('category')}
    wanted |= {(fill['after']['category'], fill['after']['subcategory']) for fill in fills.values() if fill['after'].get('category')}
    missing = sorted(pair for pair in wanted if pair not in known)
    final['subgrupos_nuevos'] = len(missing)
    # Cases for numbers two or more lines carry.
    cases = plan_cases(lines, multi, plans)
    final['casos'] = len(cases)
    pending = pending_name_links(lines, numbers, plans)
    final['nombre_pendiente'] = len(pending)
    by_kind = Counter(entry['number']['part_type'] for entry in unplaced if not subgrupo_for(entry['number']['part_type']))
    listed['sin_subgrupo'] = [f'{text or "(vacío)"} · {count}' for text, count in by_kind.most_common(40)]
    return {'numbers': plans, 'lines': new_lines, 'updates': updates, 'new_refs': new_refs, 'supersede': supersede, 'fills': fills,
            'sources': sources, 'cases': cases, 'part_types': missing, 'pending': pending, 'final': final, 'listed': dict(listed),
            'schema': state, 'simulated_mains': len(simulated), 'line_rows': lines}


def plan_fills(lines, plans, subgrupo_for):
    """{line id: {'before', 'after'}}: the catalog lines that carry library numbers and have their subgrupo or aplicaciones_texto
    blank, filled from them: the subgrupo only when every number of the line maps to the same one (A6); the applications of the number
    the line's codigo is, else of its first number."""
    from .. import references
    by_line = defaultdict(list)
    for entry in plans:
        if entry['action'] in ('existente', 'nueva_en_linea') and entry['line'] in lines and lines[entry['line']]['estado'] != 'referencia':
            by_line[entry['line']].append(entry['number'])
    fills = {}
    for line_id, numbers in by_line.items():
        line = lines[line_id]
        numbers.sort(key=lambda number: (references.clave(line['sku']) not in number_claves(number), number['manufacturer'], number['code']))
        after, sources = {}, dict(line['field_sources'] or {})
        mapped = {subgrupo_for(number['part_type']) for number in numbers}
        if not line['part_type_id'] and not (line['category'] or line['subcategory']) and len(mapped) == 1 and None not in mapped:
            after['category'], after['subcategory'] = next(iter(mapped))
            sources['subgrupo'] = {'source': ORIGIN, 'texto': numbers[0]['part_type']}
        applications = next((number['applications'].strip() for number in numbers if (number['applications'] or '').strip()), '')
        if not (line['aplicaciones_texto'] or '').strip() and applications:
            after['aplicaciones_texto'] = applications
            sources['aplicaciones_texto'] = {'source': ORIGIN}
        if after:
            after['field_sources'] = sources
            fills[line_id] = {'before': {field: line.get(field) for field in LINE_FILL}, 'after': after}
    return fills


def plan_sources(plans, by_number, existing):
    """{'create': [...], 'merge': [...]}: each OEMReferenceSource of a number as a ReferenceSource of its Reference (A4), merged into
    the one with its (reference, kind, citation) when there is one (S2a's buscador sources of the same algo: text, a previous run's)."""
    from ..oem_reference import merge_detail, source_kind_of
    create, merge, planned = [], [], {}
    for entry in plans:
        target = entry.get('ref') if entry['action'] in ('hecho', 'existente') else None
        for source in by_number.get(entry['id'], ()):
            kind = source_kind_of(source['kind'], source['citation'])
            citation = (source['citation'] or '').strip()[:500]
            detail = {**(source['detail'] or {}), 'legacy_kind': source['kind'], 'legacy_id': source['pk'],
                      'legacy_created_at': source['created_at'].isoformat() if source['created_at'] else None}
            key = (target if target is not None else f"nuevo:{entry['id']}", kind, citation)
            if key in planned:  # two library kinds of one citation that become one kind: one source
                planned[key]['detail'] = merge_detail(planned[key]['detail'], detail)
                continue
            found = existing.get(key) if target is not None and not str(target).startswith('virtual:') else None
            if found is not None:
                merged = merge_detail(found[1], detail)
                if merged != found[1]:
                    merge.append({'id': found[0], 'before': found[1], 'detail': merged})
                planned[key] = {'detail': merged}
                continue
            row = {'number': entry['id'], 'reference': target, 'kind': kind, 'citation': citation, 'detail': detail,
                   'created_by_id': source['created_by_id']}
            planned[key] = row
            create.append(row)
    return {'create': create, 'merge': merge}


def repoint_counts(state):
    """How many aftermarket codes, tombstones and SKU links the run re-points: the ones without ref_id (every one before 0079)."""
    from ..oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, PartOEMLink
    out = {}
    for name, model in (('equivalencias', OEMCrossReference), ('equivalencias_retiradas', OEMCrossReferenceRetired), ('vinculos', PartOEMLink)):
        rows = model.objects.filter(legacy_reference__isnull=False)
        out[name] = rows.filter(reference__isnull=True).count() if state['ref_id'] else rows.count()
    return out


def plan_cases(lines, multi, plans):
    """[{'lines', 'numbers', 'keeper', 'key'}]: the lines that carry one number between them, grouped as S2b grouped them (numbers
    sharing a line join one group), each group's key the one S2b gives it; a group S2b's case already has (by key, or a
    shared_oem_number case holding all its lines) is left out."""
    from ..matching_models import MatchingCase
    keeper_of = {entry['id']: entry.get('line') for entry in plans}
    code_of = {entry['id']: entry['code'] for entry in plans}
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for carriers in multi.values():
        first, *rest = sorted(carriers, key=str)
        for other in rest:
            parent[find(other)] = find(first)
    grouped = defaultdict(lambda: {'lines': set(), 'numbers': set(), 'keepers': set()})
    for number, carriers in multi.items():
        root = find(next(iter(carriers)))
        grouped[root]['lines'] |= carriers
        grouped[root]['numbers'].add(code_of[number])
        if keeper_of.get(number) in carriers:
            grouped[root]['keepers'].add(keeper_of[number])
    covered = []
    for case in MatchingCase.objects.filter(method=SHARED_METHOD).values('key', 'sources', 'candidates'):
        covered.append((case['key'], {str(row.get('id')) for row in [*(case['sources'] or []), *(case['candidates'] or [])]}))
    out = []
    for group in grouped.values():
        members = sorted(str(pk) for pk in group['lines'])
        key = f"catalog:shared:{hashlib.sha256('|'.join(members).encode()).hexdigest()[:24]}"
        if any(key == other or set(members) <= ids for other, ids in covered):
            continue
        keeper = min(group['keepers'] or group['lines'], key=lambda pk: (lines[pk]['estado'] != 'publicada', lines[pk]['sku'], str(pk)))
        out.append({'lines': members, 'numbers': sorted(group['numbers']), 'keeper': str(keeper), 'keeper_sku': lines[keeper]['sku'], 'key': key})
    return sorted(out, key=lambda group: group['keeper_sku'])


def pending_name_links(lines, numbers, plans):
    """[{line, sku, number, maker, code, placed}]: the name links link_oem_numbers would still add (a catalog line whose codigo names a
    library number it has no link to). They are not carriers: the number goes where the plan puts it (a line of its own in referencia
    when nothing carries it), declarado, for rule 5b's review (S6)."""
    from ..oem_links import name_targets
    from ..oem_reference_models import PartOEMLink
    catalog = [(pk, line['sku'], line['description'], '') for pk, line in lines.items() if is_live(line) and line['estado'] in CATALOG]
    by_compact = defaultdict(list)
    for number in numbers:
        by_compact[number['code']].append((number['pk'], number['manufacturer']))
    wanted = name_targets(catalog, library=by_compact)
    have = {(str(part), str(number)) for part, number in
            PartOEMLink.objects.filter(source='sku_name').order_by('pk').values_list('part_id', 'legacy_reference_id')}
    where = {str(entry['id']): entry for entry in plans}
    by_id = {str(number['pk']): number for number in numbers}
    out = []
    for part, refs in sorted(wanted.items()):
        for ref in sorted(refs):
            if (part, ref) in have:
                continue
            entry, line = where.get(ref, {}), lines[uuid.UUID(part)]
            out.append({'line': part, 'sku': line['sku'], 'number': ref, 'maker': by_id[ref]['manufacturer'], 'code': by_id[ref]['code'],
                        'placed': entry.get('action'), 'estado': entry.get('estado')})
    return out


# --- apply ----------------------------------------------------------------------------------------------------------------------------

def write(result, log, run, user):
    """Write the plan in bulk. Returns the counts written."""
    from django.db import connection
    from django.db.models import F
    from django.utils import timezone
    from .. import lines as line_module
    from .. import references
    from ..matching_engine import part_row, save_case
    from ..models import Part, Reference, ReferenceSource
    from ..technical_data import part_type_for
    now, counts = timezone.now(), Counter()
    # New subgrupos, with their templates.
    known = part_types()
    for category, name in result['part_types']:
        part_type = part_type_for(category, name)
        known[(part_type.category, part_type.name)] = part_type.pk
        log.append({**run, 'model': 'mall.PartType', 'id': part_type.pk, 'action': 'create', 'after': {'category': category, 'name': name}})
        counts['subgrupos_nuevos'] += 1
    # Lines in referencia, without mains: their library number is their main.
    built_lines = []
    for spec in result['lines']:
        category, subcategory = spec.get('category', ''), spec.get('subcategory', '')
        built_lines.append(Part(sku=spec['sku'], estado='referencia', origen='biblioteca', active=False, tipo='OEM', is_OEM=True,
                                description=spec['description'], aplicaciones_texto=spec['aplicaciones_texto'], category=category,
                                subcategory=subcategory, part_type_id=known.get((category, subcategory)) if category else None,
                                field_sources=spec['field_sources'], created_at=now))
    line_module.bulk_create_lines(built_lines, batch_size=CHUNK, actor=user, mains=False)
    line_of = {spec['key']: line.pk for spec, line in zip(result['lines'], built_lines)}
    for spec, line in zip(result['lines'], built_lines):
        log.append({**run, 'model': 'mall.Part', 'id': str(line.pk), 'action': 'create', 'role': 'referencia',
                    'after': recorded({'sku': line.sku, 'estado': 'referencia', 'origen': 'biblioteca', 'numbers': spec['numbers']})})
    counts['lineas_referencia'] = len(built_lines)
    # The References there are: uid, detail and the rest (one UPDATE each, no signals).
    for pk, change in result['updates'].items():
        after = change['after']
        Reference._base_manager.filter(pk=pk).update(**after, version=F('version') + 1, updated_at=now)
        log.append({**run, 'model': 'mall.Reference', 'id': pk, 'action': 'update', 'role': 'existente', 'before': recorded(change['before']),
                    'after': recorded(after)})
        counts['referencias_actualizadas'] += 1
    for entry in result['numbers']:  # a main retyped oem: its line's tipo follows (I11)
        if entry.get('retyped_main'):
            line = Part._base_manager.filter(pk=entry['line']).values('tipo', 'is_OEM').first()
            if line and (line['tipo'], line['is_OEM']) != ('OEM', True):
                Part._base_manager.filter(pk=entry['line']).update(tipo='OEM', is_OEM=True, updated_at=now)
                log.append({**run, 'model': 'mall.Part', 'id': str(entry['line']), 'action': 'update', 'role': 'tipo', 'before': line,
                            'after': {'tipo': 'OEM', 'is_OEM': True}})
    # New References: on their carrier, or as the lines' mains and the familia's other numbers.
    built = []
    for spec in result['new_refs']:
        line = line_of.get(spec['line'], spec['line'])
        ref = references.build(line, spec['code'], spec['brand'], tipo='oem', estado=spec['estado'], actor=user, detail=spec['detail'],
                               notes=spec['notes'], printed_forms=spec['printed_forms'], dispute_note=spec['dispute_note'])
        ref.uid, ref.is_main = spec['number'], spec['is_main']
        ref.created_by_id, ref.created_at = spec['created_by_id'], spec['created_at'] or now
        built.append((spec, ref))
    references.bulk_create([ref for _, ref in built], batch_size=CHUNK, actor=user, serialized=True)  # under the freeze and the lock
    ref_of = {}
    for spec, ref in built:
        ref_of[spec['number']] = ref.pk
        log.append({**run, 'model': 'mall.Reference', 'id': ref.pk, 'action': 'create', 'role': 'principal' if spec['is_main'] else 'numero',
                    'line': str(ref.part_id), 'after': recorded({'uid': spec['number'], 'code': ref.code, 'brand': ref.brand, 'estado': ref.estado,
                                                                 'is_main': ref.is_main})})
    counts['referencias_nuevas'] = len(built)
    # Supersession.
    uid_of = dict(Reference._base_manager.filter(uid__in=list({*result['supersede'], *result['supersede'].values()})).values_list('uid', 'pk'))
    for number, superseder in result['supersede'].items():
        mine, target = uid_of.get(number), uid_of.get(superseder)
        if mine is None or target is None or mine == target:
            continue
        before = Reference._base_manager.filter(pk=mine).values_list('reemplazado_por_id', flat=True).first()
        if before != target:
            Reference._base_manager.filter(pk=mine).update(reemplazado_por_id=target, version=F('version') + 1, updated_at=now)
            log.append({**run, 'model': 'mall.Reference', 'id': mine, 'action': 'update', 'role': 'reemplazo', 'before': {'reemplazado_por_id': before},
                        'after': {'reemplazado_por_id': target}})
            counts['reemplazos'] += 1
    # Catalog lines' blank subgrupo and applications.
    for line_id, fill in result['fills'].items():
        after = dict(fill['after'])
        if after.get('category'):
            after['part_type_id'] = known.get((after['category'], after['subcategory']))
        Part._base_manager.filter(pk=line_id).update(**after, updated_at=now)
        log.append({**run, 'model': 'mall.Part', 'id': str(line_id), 'action': 'update', 'role': 'completada', 'before': recorded(fill['before']),
                    'after': recorded(after)})
        counts['lineas_completadas'] += 1
    # Sources.
    uid_ref = dict(Reference._base_manager.filter(uid__in=[row['number'] for row in result['sources']['create']]).values_list('uid', 'pk'))
    rows = []
    for row in result['sources']['create']:
        reference = row['reference'] if isinstance(row['reference'], int) else uid_ref.get(row['number'])
        if reference is None:
            continue
        rows.append(ReferenceSource(reference_id=reference, kind=row['kind'], citation=row['citation'], detail=row['detail'],
                                    created_by_id=row['created_by_id']))
    ReferenceSource.objects.bulk_create(rows, batch_size=CHUNK)
    for source in rows:
        log.append({**run, 'model': 'mall.ReferenceSource', 'id': source.pk, 'action': 'create',
                    'after': recorded({'reference_id': source.reference_id, 'kind': source.kind, 'citation': source.citation})})
    for row in result['sources']['merge']:
        ReferenceSource.objects.filter(pk=row['id']).update(detail=row['detail'])
        log.append({**run, 'model': 'mall.ReferenceSource', 'id': row['id'], 'action': 'update', 'before': {'detail': row['before']},
                    'after': {'detail': row['detail']}})
    counts['fuentes_nuevas'], counts['fuentes_unidas'] = len(rows), len(result['sources']['merge'])
    # Re-point the aftermarket codes, their tombstones and the links: ref = the Reference whose uid is their reference_id.
    with connection.cursor() as cursor:
        for model, table in REPOINTED:
            cursor.execute(f'UPDATE {table} AS x SET ref_id = r.id FROM mall_partcode AS r '
                           f'WHERE r.uid = x.reference_id AND x.ref_id IS NULL RETURNING x.id, r.id')
            changed = cursor.fetchall()
            for pk, ref in changed:
                log.append({**run, 'model': model, 'id': pk, 'action': 'update', 'before': {'ref_id': None}, 'after': {'ref_id': ref}})
            counts[f'repuntadas:{table}'] = len(changed)
    # Cases.
    for group in result['cases']:
        rows = {str(part.pk): part_row(part) for part in Part._base_manager.filter(pk__in=group['lines']).prefetch_related('codes')}
        case = save_case(key=group['key'], kind='catalog', sources=[rows[pk] for pk in group['lines'] if pk != group['keeper'] and pk in rows],
                         candidates=[rows[group['keeper']]], target=group['keeper_sku'], method=SHARED_METHOD,
                         reason=(f'Estos SKU llevan los mismos números OEM ({", ".join(group["numbers"][:6])}'
                                 f'{"…" if len(group["numbers"]) > 6 else ""}). Si son el mismo repuesto, agrúpalos en {group["keeper_sku"]}; '
                                 'si no, mantenlos separados.'))
        log.append({**run, 'model': 'matching.MatchingCase', 'id': case.pk, 'action': 'create', 'after': {'key': case.key, 'method': SHARED_METHOD}})
        counts['casos'] += 1
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
        elif action in ('rollback', 'delete'):
            rows.pop(key, None)
    return rows


def busy_lines(ids):
    """The lines among ids that something besides their References points at (variants, images, orders, merges...)."""
    from ..models import Part, Reference
    ids, busy = [pk for pk in ids], set()
    for relation in Part._meta.related_objects:
        model, field = relation.related_model, relation.field
        if model is Reference or not getattr(field, 'attname', None):
            continue
        for start in range(0, len(ids), CHUNK):
            busy |= set(model._base_manager.filter(**{f'{field.name}__in': ids[start:start + CHUNK]}).values_list(field.attname, flat=True))
    return busy


def busy_references(ids):
    """The References among ids that something besides their sources points at (aftermarket codes, links, variants, replacements...)."""
    from ..models import Reference, ReferenceSource
    ids, busy = [int(pk) for pk in ids], set()
    for relation in Reference._meta.related_objects:
        model, field = relation.related_model, relation.field
        if model is ReferenceSource or not getattr(field, 'attname', None):
            continue
        for start in range(0, len(ids), CHUNK):
            busy |= set(model._base_manager.filter(**{f'{field.name}__in': ids[start:start + CHUNK]}).values_list(field.attname, flat=True))
    return busy


def undo(log, run):
    """Put back what the applies on this database did: {'restored', 'deleted', 'changed': [...]}."""
    from django.apps import apps
    from django.db.models import F
    from django.utils import timezone
    from .. import references
    from ..matching_models import MatchingCase
    from ..models import Part, PartType, Reference, ReferenceSource
    chains = applied_lines()
    by_model = defaultdict(dict)
    for (model, pk), chain in chains.items():
        by_model[model][pk] = chain
    restored, deleted, changed, now = 0, 0, [], timezone.now()
    for pk, chain in by_model.get('matching.MatchingCase', {}).items():
        case = MatchingCase.objects.filter(pk=pk, status='review').first()
        if case is None:
            changed.append(f'caso {pk}: ya se decidió, se deja')
            continue
        case.delete()
        deleted += 1
        log.append({**run, 'model': 'matching.MatchingCase', 'id': pk, 'action': 'delete', 'before': chain[-1]['after']})
    for pk, chain in by_model.get('mall.ReferenceSource', {}).items():
        if chain[0]['action'] == 'create':
            if ReferenceSource.objects.filter(pk=pk).delete()[0]:
                deleted += 1
                log.append({**run, 'model': 'mall.ReferenceSource', 'id': int(pk), 'action': 'delete', 'before': chain[-1]['after']})
        else:
            first = chain[0]['before']['detail']
            ReferenceSource.objects.filter(pk=pk).update(detail=first)
            restored += 1
            log.append({**run, 'model': 'mall.ReferenceSource', 'id': int(pk), 'action': 'rollback', 'after': {'detail': first}})
    for model, _ in REPOINTED:
        rows = by_model.get(model, {})
        if not rows:
            continue
        cls = apps.get_model(model)
        by_ref = defaultdict(list)
        for pk, chain in rows.items():
            by_ref[chain[-1]['after']['ref_id']].append(int(pk))
        for ref, pks in by_ref.items():
            done = cls.objects.filter(pk__in=pks, reference_id=ref).update(reference=None)
            restored += done
        for pk in rows:
            log.append({**run, 'model': model, 'id': int(pk), 'action': 'rollback', 'after': {'ref_id': None}})
    refs = by_model.get('mall.Reference', {})
    for pk, chain in sorted(refs.items(), key=lambda item: int(item[0])):
        updates = [line for line in chain if line['action'] == 'update']
        if chain[0]['action'] == 'create' or not updates:
            continue
        fields = list(dict.fromkeys(field for line in updates for field in line['after']))
        row = Reference._base_manager.filter(pk=int(pk)).values(*fields).first()
        if row is None:
            continue
        now_values, last = recorded(row), {field: next(line['after'][field] for line in reversed(updates) if field in line['after']) for field in fields}
        first = {field: next(line['before'][field] for line in updates if field in line['before']) for field in fields}
        if now_values == recorded(first):
            continue
        if now_values != recorded(last):
            changed.append(f'referencia {pk}: cambió después de aplicar')
            continue
        Reference._base_manager.filter(pk=int(pk)).update(**first, version=F('version') + 1, updated_at=now)
        restored += 1
        log.append({**run, 'model': 'mall.Reference', 'id': int(pk), 'action': 'rollback', 'before': now_values, 'after': recorded(first)})
    created = [int(pk) for pk, chain in refs.items() if chain[0]['action'] == 'create']
    if created:  # a created number replaced by another created one names it: both go
        Reference._base_manager.filter(pk__in=created, reemplazado_por__in=created).update(reemplazado_por=None)
        busy = busy_references(created)
        for pk in sorted(busy):
            changed.append(f'referencia {pk}: algo la nombra (equivalencias, vínculos, artículos o un reemplazo), se deja')
        free = [pk for pk in created if pk not in busy]
        ReferenceSource.objects.filter(reference_id__in=free).delete()
        deleted += references.purge(free)
        for pk in free:
            log.append({**run, 'model': 'mall.Reference', 'id': pk, 'action': 'delete', 'before': refs[str(pk)][0]['after']})
    lines = by_model.get('mall.Part', {})
    for pk, chain in lines.items():
        updates = [line for line in chain if line['action'] == 'update']
        if chain[0]['action'] == 'create' or not updates:
            continue
        fields = list(dict.fromkeys(field for line in updates for field in line['after']))
        row = Part._base_manager.filter(pk=pk).values(*fields).first()
        if row is None:
            continue
        first = {field: next(line['before'][field] for line in updates if field in line['before']) for field in fields}
        last = {field: next(line['after'][field] for line in reversed(updates) if field in line['after']) for field in fields}
        if recorded(row) == recorded(first):
            continue
        if recorded(row) != recorded(last):
            changed.append(f'línea {pk}: cambió después de aplicar')
            continue
        Part._base_manager.filter(pk=pk).update(**first, updated_at=now)
        restored += 1
        log.append({**run, 'model': 'mall.Part', 'id': pk, 'action': 'rollback', 'before': recorded(row), 'after': recorded(first)})
    made = [uuid.UUID(pk) for pk, chain in lines.items() if chain[0]['action'] == 'create']
    if made:
        left = set(Reference._base_manager.filter(part_id__in=made).values_list('part_id', flat=True))
        busy = busy_lines(made) | left
        for pk in sorted(busy, key=str):
            changed.append(f'línea {pk}: algo la nombra (sus referencias, artículos, imágenes u órdenes), se deja')
        free = [pk for pk in made if pk not in busy]
        for start in range(0, len(free), CHUNK):
            deleted += Part._base_manager.filter(pk__in=free[start:start + CHUNK], estado='referencia').delete()[1].get('mall.Part', 0)
        for pk in free:
            log.append({**run, 'model': 'mall.Part', 'id': str(pk), 'action': 'delete', 'before': lines[str(pk)][0]['after']})
    for pk, chain in by_model.get('mall.PartType', {}).items():
        if not Part._base_manager.filter(part_type_id=pk).exists():
            PartType.objects.filter(pk=pk).delete()  # its empty template goes with it
            deleted += 1
            log.append({**run, 'model': 'mall.PartType', 'id': pk, 'action': 'delete', 'before': chain[0]['after']})
    return {'restored': restored, 'deleted': deleted, 'changed': changed}


# --- run ------------------------------------------------------------------------------------------------------------------------------

ROWS = (('numeros', 'números de la biblioteca'), ('existente', 'ya en una línea (clave, fabricante)'),
        ('principal_sin_marca', 'principal sin marca'), ('familia', 'marca de la familia'), ('nombre_sku', 'vínculo por nombre del SKU'),
        ('varios_portadores', '  con 2+ líneas'), ('sin_portador', 'sin línea'), ('lineas_referencia', 'líneas en referencia'),
        ('lineas_dos_numeros', '  con 2 números'), ('referencias_nuevas', 'referencias nuevas'), ('fuentes', 'fuentes'),
        ('nombre_pendiente', 'vínculos por nombre pendientes'))


def print_summary(stdout, mode, result, writes=None):
    title = {'apply': 'Aplicado', 'dry_run': 'Simulación'}[mode]
    final, state = result['final'], result['schema']
    stdout.write(f'{title} · S4 números de la biblioteca como referencias\n')
    if not (state['mains'] and state['ref_id'] and state['tipo']):
        missing = [name for name, ok in (('0075/0076 (las principales de S2b)', state['tipo'] and state['mains']), ('0079', state['ref_id'])) if not ok]
        stdout.write(f"  (sin {' ni '.join(missing)} en esta base: simulación; {result['simulated_mains']:,} principales de S2b simuladas, sin sus "
                     'unificaciones de duplicados; toda equivalencia cuenta como por re-apuntar. Los números reales salen del ensayo)\n')
    stdout.write(f"  {'':34} {'esperado':>9} {'resultado':>10}\n")
    for key, label in ROWS:
        stdout.write(f"  {label:34} {EXPECTED[key]:>9,} {final.get(key, 0):>10,}\n")
    stdout.write('  (esperado: plan del 2026-10-13, antes del arreglo GMB: −208 y +46 números)\n')
    for key, label in (('hecho', 'ya hechos (corrida anterior)'), ('codigo_sin_marca', 'código sin marca (no principal)'),
                       ('existente_principal', '  ya en una línea: su principal'), ('varios_existente', '  2+ líneas: la que ya lo tiene'),
                       ('varios_codigo_propio', '  2+ líneas: el código propio'), ('varios_regla_3g', '  2+ líneas: regla 3g'),
                       ('referencias_actualizadas', 'referencias existentes actualizadas'), ('reemplazos', 'reemplazos'),
                       ('lineas_completadas', 'líneas con subgrupo o aplicaciones completados'), ('fuentes_nuevas', 'fuentes nuevas'),
                       ('fuentes_unidas', 'fuentes unidas a una existente'), ('equivalencias', 'equivalencias por re-apuntar'),
                       ('equivalencias_retiradas', 'equivalencias retiradas por re-apuntar'), ('vinculos', 'vínculos por re-apuntar'),
                       ('subgrupos_nuevos', 'subgrupos nuevos'), ('casos', 'casos shared_oem_number nuevos')):
        if final.get(key):
            stdout.write(f'  {label}: {final[key]:,}\n')
    if result['part_types']:
        stdout.write('  Subgrupos nuevos: ' + ', '.join(f'{category} / {name}' for category, name in result['part_types']) + '\n')
    if result['listed'].get('sin_subgrupo'):
        stdout.write('  Tipos de pieza sin subgrupo (quedan en field_sources para Revisión IA): ' + '; '.join(result['listed']['sin_subgrupo'][:15]) + '\n')
    if result['pending']:
        placed = Counter(row['placed'] for row in result['pending'])
        stdout.write(f"  Vínculos por nombre pendientes (no se crean; el número queda declarado, para la revisión de la regla 5b): {len(result['pending'])} · "
                     + ' · '.join(f'{key} {count}' for key, count in sorted(placed.items(), key=lambda item: str(item[0]))) + '\n')
        for row in result['pending'][:10]:
            stdout.write(f"    {row['sku']} → {row['maker']} {row['code']} ({row['placed']}, {row['estado']})\n")
    if result['cases']:
        stdout.write(f"  Números en varias líneas sin caso de S2b ({len(result['cases'])} casos shared_oem_number):\n")
        for group in result['cases'][:10]:
            stdout.write(f"    {group['keeper_sku']} ← {len(group['lines']) - 1} línea(s) · {', '.join(group['numbers'][:4])}\n")
    if writes:
        stdout.write('  Escrito: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(writes.items())) + '\n')


def report(result):
    return {'expected': EXPECTED, 'result': dict(result['final']), 'schema': result['schema'], 'simulated_mains': result['simulated_mains'],
            'part_types': result['part_types'], 'pending_name_links': result['pending'], 'cases': result['cases'], 'listed': result['listed'],
            'lines': [{key: value for key, value in line.items() if key != 'field_sources'} for line in result['lines'][:200]],
            'two_numbers': [line for line in result['lines'] if len(line['numbers']) > 1],
            'carried': [{'number': entry['id'], 'maker': entry['maker'], 'code': entry['code'], 'kind': entry.get('kind'),
                         'line': str(entry['line']), 'sku': result['line_rows'][entry['line']]['sku'], 'carriers': len(entry.get('carriers') or [])}
                        for entry in result['numbers'] if entry['action'] == 'nueva_en_linea']}


def run(*, apply=False, rollback=False, username=None, report_path=None, stdout=None):
    """Plan, and with apply write it, or with rollback undo the applies on this database. None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from ..matching_queue import matching_lock, matching_work
    from ..models import Part
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
        if not (state['tipo'] and state['mains'] and state['ref_id']):
            stdout.write('Faltan S2b (0075/0076 y sus principales: python -m mall.refactor.s2b_main_refs) o 0079 en esta base: aplícalos primero '
                         '(la simulación funciona sin ellos).\n')
            return None
        if rollback and state['tightened']:
            stdout.write('0080_library_lines_checks está aplicada: reviértela primero (python manage.py migrate mall 0079_library_lines).\n')
            return None
    run_line = {'stage': STAGE, 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}

    def go():
        log = []
        with transaction.atomic():
            if rollback:
                result = {'mode': 'rollback', **undo(log, run_line)}
            else:
                if apply:  # every line locked, so the plan and its writes see the same rows
                    list(Part._base_manager.select_for_update().order_by('pk').values_list('pk', flat=True))
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
            stdout.write(f"Rollback · S4: {result['restored']} filas restauradas · {result['deleted']} borradas · "
                         f"{len(result['changed'])} cambiadas o nombradas (se dejan)\n")
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
    parser = argparse.ArgumentParser(description='S4: los números de la biblioteca OEM como referencias, y líneas en referencia para los que nadie lleva.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los cambios sin escribir nada (también antes de S2b y de 0079: simulación).')
    mode.add_argument('--apply', action='store_true', help='Escribe todo en una transacción (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base (requiere el congelamiento en vivo).')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply; opcional con --rollback).')
    parser.add_argument('--report', help='Escribe en esta ruta (JSON) los conteos, las listas, los casos y los vínculos pendientes.')
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
