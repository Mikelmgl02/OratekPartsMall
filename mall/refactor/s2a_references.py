"""Stage S2a of the inventory refactor: every Reference (mall_partcode, PartCode before S2a) takes its tipo, estado and sources, the
spelling duplicates retire, and every brand the codes carry is a brand.

    python -m mall.refactor.s2a_references --dry-run|--apply --user USERNAME|--rollback [--purge-retired] [--report PATH] [--json]

tipo is ref_type, mapped straight (plan A2): oem → oem, company → aftermarket, unknown → otro (440 · 847 · 2,479 on 2026-10-13).
Nothing is reclassified: the merge aliases keep brand '' and tipo otro (their TAG brands move in S6, since services.matching_part, the
import preview and MatchIndex match brandless variants on brand in [x, '']). A row whose tipo already agrees with its ref_type keeps it.

estado, on the rows 0073 left at its default (created_at and updated_at NULL, estado declarado: the code from S2a stamps both on every
row it writes, so an estado it set is never derived again): an OEM alterno filed under a library number takes the number's status
(verified → verificado, declared → declarado, inferred → inferido, disputed → disputa with the number's dispute note); otherwise a
code the OEM finder wrote (reference_source algo:...) is inferido and the rest declarado.

Spelling duplicates: two live rows of one line with one key (clave, brand, supplier: 13780-61J00-NP and 13780-61J00-NP-; 7 pairs on
2026-10-13). One stays: the line's codigo, then oem before aftermarket before otro, verificado before declarado before inferido, the
cleaner spelling (no trailing - _ / ., fewer characters other than letters, digits and -) and the lower id. The others retire
(estado retirada, reemplazado_por the one that stays, who and why in detail['retired']) and their spelling joins its printed_forms;
the manifest records each spelling's supplier items (supplier_invent_id, line, status, stock), since most hold stock under both. A key
two lines hold is only listed, and so is a duplicate whose spare is a branded OEM alterno (the OEM library cites it, and only
mall.references.retire, one save at a time, takes it off the library as the mirror does): a person decides (0074 refuses to tighten
while either exists).

Sources: each non-blank reference_source becomes a ReferenceSource by its prefix (plan A3, mall.references.source_of): algo: buscador,
a URL ia, 'Declarado por' proveedor, 'CONFIRMADO POR' and 'Marcado como OEM por' admin, a supplier catalog's citation (GMB, ASVA, ATE)
catalogo, anything else admin; the citation is the text itself (S4 meets the OEM finder's library sources on it). About 714.

Brands: every non-blank brand must be a Brand nombre or alias (Appendix C, I12). A missing spelling becomes a brand under its
normalized name (mall.brands.normalize; vehiculo when it only names OEM alternos, repuesto otherwise), or an alias of the brand that
name already is. All are listed.

It writes in bulk (no signals, and nothing in the OEM library) under the matching worker's lock, in one transaction; on the live
database --apply and --rollback need the S0.11 guard (mall.refactor.guard: the catalog freeze is on and no writer is still busy).
Every row it changes or creates goes, with its values before, to mall.refactor.output.manifest_path('s2a') (the main checkout's git-ignored output/refactor/s2a/, /app/output/… in the containers,
REFACTOR_OUTPUT in a rehearsal) before the transaction commits; the folder must exist, and a manifest holding another database's lines
is refused. The dry run also works before 0073 is applied (every row read as 0073 leaves it: tipo otro, estado declarado, no source)
and compares the database's generated clave with mall.references.clave() on every code once it is. Re-running changes nothing new: it
runs before landing (the code before S2a ignores the new columns and the 7 retired rows only stay in its code lists) and again after,
as the catch-up.

--rollback puts back what the applies on this database changed and no rollback has put back yet, unless a row changed since (listed):
the References' estado, dispute note, replacement, printed forms and detail before, the sources, brands and aliases it created
deleted. tipo is never put back: before, it only held 0073's default (otro), the code before S2a never reads it, and otro next to
ref_type oem or company breaks 0074's reference_tipo_matches_ref_type. A row coming back to a key a live row holds (a retired duplicate,
or a disputed one whose key another line took) cannot while 0074's reference_one_live_line exists (listed: run --rollback again after
reversing 0074). A row the code retired after the apply is not put back: --purge-retired deletes the rows the code retired that
nothing names as its replacement (mall.references.purge, repeated until none is left), which the code before S2a would show as live
codes, and lists the rest; without it they are listed. S2a rolls back in this order, under the freeze, from the worktree:
--rollback --purge-retired; reverse 0074 (python manage.py migrate mall 0073_reference; it refuses while one printed brand and code
is held twice); --rollback again when the first one listed rows waiting for it; revert the code (git revert, reload gunicorn, restart
the workers); and last, optionally, reverse 0073 (migrate mall 0072_line_lifecycle_checks: 0073's foreign keys delete in the database
too, so the code before S2a can delete a code with sources). Never revert the code while 0074 is applied: the code before S2a writes
ref_type without tipo, which its CHECKs refuse. --report writes the duplicates, the keys two lines hold, the sources and the brands,
for the owner.
"""
import argparse
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter, defaultdict

STAGE = 's2a'
# The live dry run of 2026-10-14 12:50 (the rehearsal's dump). Plan §3 S2a counted 3,766 · 440 · 847 · 2,479 · 7 · 2,481 · ≈714 on
# 2026-10-13: since then the 34 legacy merges OD18 separated took their 60 merge aliases (unknown, brand '') away, Productos nuevos
# approvals added seven OEM alternos ('Declarado por …'), and Editar SKU ten codes (six oem, four unknown; eight brandless). The live
# catalog keeps moving: D0 takes the rehearsal's dry run as the expected counts.
EXPECTED = {'filas': 3723, 'oem': 453, 'aftermarket': 847, 'otro': 2423, 'retirada': 7, 'sin_marca': 2429, 'fuentes': 721}
ESTADO_OF_STATUS = {'verified': 'verificado', 'declared': 'declarado', 'inferred': 'inferido', 'disputed': 'disputa'}
FIELDS = ('tipo', 'ref_type', 'estado', 'dispute_note', 'reemplazado_por_id', 'printed_forms', 'detail')
RESTORABLE = ('estado', 'dispute_note', 'reemplazado_por_id', 'printed_forms', 'detail')  # what --rollback puts back: never tipo (undo)
LEGACY = ('pk', 'part_id', 'part__sku', 'brand', 'code', 'ref_type', 'reference_source', 'oem_reference_id')
TIPO_RANK = {'oem': 0, 'aftermarket': 1, 'interno': 2, 'otro': 3}
ESTADO_RANK = {'verificado': 0, 'declarado': 1, 'inferido': 2}
CHECK = 'reference_one_live_line'  # 0074's unique index: with it in place a retired duplicate cannot come back
CHUNK = 2000
DISPUTED = 'En disputa en la Biblioteca OEM'


def current_database():
    from django.db import connection
    return str(connection.settings_dict['NAME'])


def output_dir():
    from .output import main_checkout
    return main_checkout() / 'output'  # git-ignored; /app/output in the containers (a run from a worktree writes in the main checkout's)


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
    """Why the manifest cannot take this run's lines, or None: the output folder is missing (REFACTOR_OUTPUT aside), or the manifest
    holds another database's lines (a rehearsal's or a test run's), which a rollback here must never replay."""
    if not os.environ.get('REFACTOR_OUTPUT', '').strip() and not output_dir().is_dir():
        return f'Falta la carpeta {output_dir()} para el manifiesto; ejecútalo dentro del contenedor de la app.'
    others = sorted({str(line.get('database')) for line in manifest_lines()} - {current_database()})
    if others:
        return (f"El manifiesto {manifest_path()} tiene líneas de otra base ({', '.join(others)}): muévelo antes de aplicar o deshacer "
                'en esta.')
    return None


def refusal():
    """The S0.11 guard (mall.refactor.guard): on the live database, the catalog freeze and no writer still busy."""
    from .guard import refusal as shared
    return shared(True)


def write_manifest(lines):
    """Add the lines to the manifest through a flushed temporary copy renamed over it, so it holds all of them or none. Called inside
    the transaction: a failed write rolls every row back."""
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
    """Values as the manifest holds them (ids and times as text), for comparing a row now with what a run wrote."""
    return json.loads(json.dumps(values, default=str))


def expanded():
    """Whether 0073 is applied here (the new columns of mall_partcode and the ReferenceSource table)."""
    from django.db import connection
    with connection.cursor() as cursor:
        columns = {column.name for column in connection.introspection.get_table_description(cursor, 'mall_partcode')}
        tables = set(connection.introspection.table_names(cursor))
    return {'tipo', 'estado', 'clave', 'printed_forms', 'detail', 'created_at'} <= columns and 'mall_referencesource' in tables


def tightened():
    from django.db import connection
    with connection.cursor() as cursor:
        return CHECK in connection.introspection.get_constraints(cursor, 'mall_partcode')


# --- plan ----------------------------------------------------------------------------------------------------------------------------

def reference_rows(schema, queryset=None):
    """Every Reference's values (LEGACY, FIELDS, supplier_id, created_at, updated_at, clave), retired ones included; with 0073's defaults
    before it is applied (the clave computed in Python)."""
    from .. import references
    from ..models import Reference
    rows = (queryset if queryset is not None else Reference._base_manager.all()).order_by('pk')
    if schema:
        yield from rows.values(*LEGACY, *FIELDS, 'supplier_id', 'created_at', 'updated_at', 'clave').iterator(chunk_size=CHUNK)
        return
    for row in rows.values(*LEGACY).iterator(chunk_size=CHUNK):
        yield {**row, 'tipo': 'otro', 'estado': 'declarado', 'dispute_note': '', 'reemplazado_por_id': None, 'printed_forms': [],
               'detail': {}, 'supplier_id': None, 'created_at': None, 'updated_at': None, 'clave': references.clave(row['code'])}


def keeper_key(row, after):
    """How a duplicate is ranked to stay (lowest first): the line's codigo, tipo, estado, the cleaner spelling, the lower id."""
    code = row['code']
    return (code != row['part__sku'], TIPO_RANK.get(after['tipo'], 9), ESTADO_RANK.get(after['estado'], 9),
            bool(re.search(r'[^A-Z0-9]$', code)), len(re.findall(r'[^A-Z0-9-]', code)), len(code), row['pk'])


def supplier_items(spellings):
    """{spelling: [{supplier, supplier_invent_id, part, status, quantity}]} of the supplier items whose codigo is each spelling."""
    from ..models import SupplierItem
    found = defaultdict(list)
    for item in (SupplierItem.objects.filter(codigo__in=sorted(spellings)).order_by('supplier_id', 'supplier_invent_id')
                 .values('codigo', 'supplier_id', 'supplier_invent_id', 'part_id', 'matching_status', 'reported_quantity')):
        found[item['codigo']].append({'supplier': str(item['supplier_id']), 'supplier_invent_id': item['supplier_invent_id'],
                                      'part': str(item['part_id']) if item['part_id'] else None, 'status': item['matching_status'],
                                      'quantity': item['reported_quantity']})
    return dict(found)


def plan(schema=True, *, actor='', queryset=None):
    """{'rows': {id: row}, 'changes': {id: (before, after)}, 'duplicates': [...], 'cross_line': [...], 'oem_duplicates': [...],
    'sources': [...], 'brands': {...}, 'final': Counter, 'clave_mismatch': [...]} (read-only)."""
    from django.utils import timezone
    from .. import references
    from ..brand_models import Brand, BrandAlias
    from ..brands import normalize
    from ..oem_reference_models import OEMReference
    rows = {row['pk']: row for row in reference_rows(schema, queryset)}
    library = {pk: (status, note) for pk, status, note in OEMReference.objects.filter(
        pk__in={row['oem_reference_id'] for row in rows.values() if row['oem_reference_id']}).values_list('pk', 'status', 'dispute_note')}
    afters, mismatch = {}, []
    for pk, row in rows.items():
        after = {field: row[field] for field in FIELDS}
        after['printed_forms'] = list(after['printed_forms'] or [])
        after['detail'] = dict(after['detail'] or {})
        if references.REF_TYPE_OF.get(row['tipo']) != row['ref_type']:
            after['tipo'] = references.tipo_of(row['ref_type'])
            after['ref_type'] = row['ref_type']
        if row['created_at'] is None and row['updated_at'] is None and row['estado'] == 'declarado':  # as 0073 left it
            status, note = library.get(row['oem_reference_id'], (None, ''))
            if after['tipo'] == 'oem' and status in ESTADO_OF_STATUS:
                after['estado'] = ESTADO_OF_STATUS[status]
                if after['estado'] == 'disputa':
                    after['dispute_note'] = row['dispute_note'] or note or DISPUTED
            else:
                after['estado'] = references.default_estado(row['reference_source'])
        if schema and row['clave'] != references.clave(row['code']):
            mismatch.append({'id': pk, 'code': row['code'], 'clave': row['clave'], 'python': references.clave(row['code'])})
        afters[pk] = after
    groups = defaultdict(list)
    for pk, row in rows.items():
        if afters[pk]['estado'] in references.EXCLUSIVE:
            groups[(row['clave'], row['brand'], row['supplier_id'])].append(pk)
    duplicates, cross_line, oem_duplicates = [], [], []
    stamp = {'by': actor, 'at': timezone.now().isoformat()}
    for key, pks in sorted(groups.items(), key=lambda item: min(item[1])):
        if len(pks) < 2:
            continue
        if len({rows[pk]['part_id'] for pk in pks}) > 1:
            cross_line.append({'clave': key[0], 'brand': key[1], 'rows': [{'id': pk, 'code': rows[pk]['code'], 'sku': rows[pk]['part__sku']}
                                                                       for pk in sorted(pks)]})
            continue
        ranked = sorted(pks, key=lambda pk: keeper_key(rows[pk], afters[pk]))
        keeper, retired = ranked[0], ranked[1:]
        if any(rows[pk]['ref_type'] == 'oem' and rows[pk]['brand'] for pk in retired):  # the OEM library cites it: a person retires it
            oem_duplicates.append({'clave': key[0], 'brand': key[1], 'keeper': {'id': keeper, 'code': rows[keeper]['code']},
                                   'rows': [{'id': pk, 'code': rows[pk]['code'], 'sku': rows[pk]['part__sku']} for pk in sorted(pks)]})
            continue
        for pk in retired:
            afters[pk].update(estado='retirada', reemplazado_por_id=keeper)
            afters[pk]['detail'] = {**afters[pk]['detail'], 'retired': {**stamp, 'reason': f'Grafía duplicada de {rows[keeper]["code"]} (S2a)'}}
            if rows[pk]['code'] not in afters[keeper]['printed_forms'] and rows[pk]['code'] != rows[keeper]['code']:
                afters[keeper]['printed_forms'] = [*afters[keeper]['printed_forms'], rows[pk]['code']]
        duplicates.append({'sku': rows[keeper]['part__sku'], 'clave': key[0], 'brand': key[1],
                           'keeper': {'id': keeper, 'code': rows[keeper]['code']},
                           'retired': [{'id': pk, 'code': rows[pk]['code']} for pk in retired]})
    if duplicates:
        items = supplier_items({rows[entry['keeper']['id']]['code'] for entry in duplicates}
                               | {retired['code'] for entry in duplicates for retired in entry['retired']})
        for entry in duplicates:
            spellings = [entry['keeper']['code'], *(retired['code'] for retired in entry['retired'])]
            entry['supplier_items'] = {spelling: items.get(spelling, []) for spelling in spellings}
    changes = {}
    for pk, after in afters.items():
        before = {field: rows[pk][field] for field in FIELDS}
        before['printed_forms'], before['detail'] = list(before['printed_forms'] or []), dict(before['detail'] or {})
        if after != before:
            changes[pk] = (before, after)
    existing = set()
    if schema:
        from ..models import ReferenceSource
        existing = set(ReferenceSource.objects.values_list('reference_id', 'kind', 'citation'))
    catalogs = references.catalog_citations()
    sources, kinds = [], Counter()
    for pk, row in rows.items():
        found = references.source_of(row['reference_source'], catalogs)
        if found is None:
            continue
        kind, citation, detail = found
        kinds[kind] += 1
        if (pk, kind, citation) not in existing:
            sources.append({'reference_id': pk, 'kind': kind, 'citation': citation, 'detail': detail})
    names, aliases = set(Brand.objects.values_list('nombre', flat=True)), set(BrandAlias.objects.values_list('alias', flat=True))
    spelled = defaultdict(set)
    for row in rows.values():
        if row['brand']:
            spelled[row['brand']].add(afters[row['pk']]['tipo'])
    brands, new_aliases = {}, {}
    for spelling, tipos in sorted(spelled.items()):
        if spelling in names or spelling in aliases:
            continue
        nombre = normalize(spelling)
        if nombre not in names:
            brands.setdefault(nombre, {'tipo': 'vehiculo' if tipos == {'oem'} else 'repuesto', 'spellings': []})['spellings'].append(spelling)
        if spelling != nombre:
            new_aliases[spelling] = nombre
    final = Counter()
    for pk, row in rows.items():
        after = afters[pk]
        final['filas'] += 1
        final[after['tipo']] += 1
        final[f"estado:{after['estado']}"] += 1
        final['sin_marca'] += not row['brand']
        final['fuentes'] += references.source_of(row['reference_source'], catalogs) is not None
    return {'rows': rows, 'changes': changes, 'duplicates': duplicates, 'cross_line': cross_line, 'oem_duplicates': oem_duplicates,
            'sources': sources, 'kinds': kinds, 'brands': brands, 'aliases': new_aliases, 'final': final, 'clave_mismatch': mismatch}


# --- apply ---------------------------------------------------------------------------------------------------------------------------

def write(result, log, run, user):
    """Write the plan in bulk: the References' new values (one UPDATE per group of equal values), the sources, the brands and aliases,
    and a note per duplicate with its supplier items. Returns the counts written."""
    from ..brand_models import Brand, BrandAlias
    from ..models import Reference, ReferenceSource
    rows, groups = result['rows'], defaultdict(list)
    for pk, (before, after) in sorted(result['changes'].items()):
        changed = {field: after[field] for field in FIELDS if after[field] != before[field]}
        groups[json.dumps(changed, sort_keys=True, default=str)].append(pk)
        log.append({**run, 'model': 'mall.Reference', 'id': pk, 'code': rows[pk]['code'], 'sku': rows[pk]['part__sku'], 'action': 'update',
                    'before': recorded({field: before[field] for field in changed}), 'after': recorded(changed)})
    written = 0
    for values, pks in groups.items():
        values = json.loads(values)
        for start in range(0, len(pks), CHUNK):
            written += Reference._base_manager.filter(pk__in=pks[start:start + CHUNK]).update(**values)
    for entry in result['duplicates']:  # none is a branded OEM alterno (plan lists those): the OEM library has nothing to follow
        log.append({**run, 'action': 'note', 'kind': 'spelling_duplicate', **entry})
    new_sources = [ReferenceSource(reference_id=source['reference_id'], kind=source['kind'], citation=source['citation'],
                                   detail=source['detail'], created_by=user) for source in result['sources']]
    ReferenceSource.objects.bulk_create(new_sources, batch_size=500)
    for source in new_sources:
        log.append({**run, 'model': 'mall.ReferenceSource', 'id': source.pk, 'action': 'create',
                    'after': recorded({'reference_id': source.reference_id, 'kind': source.kind, 'citation': source.citation, 'detail': source.detail})})
    new_brands = [Brand(nombre=nombre, tipo=info['tipo'], notas='Sembrada por S2a: una referencia la lleva.') for nombre, info in result['brands'].items()]
    Brand.objects.bulk_create(new_brands, batch_size=500)
    for brand in new_brands:
        log.append({**run, 'model': 'mall.Brand', 'id': brand.pk, 'action': 'create', 'after': {'nombre': brand.nombre, 'tipo': brand.tipo}})
    by_name = dict(Brand.objects.filter(nombre__in=set(result['aliases'].values())).values_list('nombre', 'pk'))
    new_aliases = [BrandAlias(alias=alias, brand_id=by_name[nombre]) for alias, nombre in result['aliases'].items() if nombre in by_name]
    BrandAlias.objects.bulk_create(new_aliases, batch_size=500)
    for alias in new_aliases:
        log.append({**run, 'model': 'mall.BrandAlias', 'id': alias.pk, 'action': 'create', 'after': {'alias': alias.alias, 'brand_id': alias.brand_id}})
    return {'references': written, 'sources': len(new_sources), 'brands': len(new_brands), 'aliases': len(new_aliases)}


# --- rollback ------------------------------------------------------------------------------------------------------------------------

def applied_lines():
    """{(model, id): [lines]} of what the applies on this database did to each row and no rollback has put back yet, oldest first."""
    rows, database = defaultdict(list), current_database()
    for line in manifest_lines():
        if line.get('stage') != STAGE or line.get('database') != database or not line.get('model'):
            continue
        key, action = (line['model'], str(line['id'])), line.get('action')
        if action in ('update', 'create'):
            rows[key].append(line)
        elif action == 'rollback':
            rows[key] = [link for link in rows[key] if link['action'] == 'create']
        elif action == 'delete':
            rows.pop(key, None)
    return rows


def undo(log, run, *, purge=False):
    """Put back what the applies on this database changed: {'restored', 'deleted', 'purged', 'changed': [...], 'waiting': [...]}.

    Only RESTORABLE columns come back, never tipo (its value before was 0073's default, otro: the code before S2a never reads it, and
    otro beside ref_type oem or company breaks 0074's reference_tipo_matches_ref_type; 0074's repair derives it from ref_type). A row
    the apply retired (a spelling duplicate) comes back unless the code changed it since; a row the code retired after the apply is
    not put back: --purge-retired deletes it when nothing names it (purge_retired), otherwise it is listed."""
    from .. import references
    from ..brand_models import Brand, BrandAlias
    from ..models import Reference, ReferenceSource
    by_model = defaultdict(dict)
    for (model, pk), chain in applied_lines().items():
        by_model[model][pk] = chain
    restored, deleted, purged, changed, waiting = 0, 0, 0, [], []
    tight = tightened()
    chains = by_model.get('mall.Reference', {})
    own = {int(pk) for pk, chain in chains.items()  # the rows an apply retired itself
           if any(link['action'] == 'update' and link['after'].get('estado') == 'retirada' for link in chain)}
    retired_later = []
    if chains:
        locked = {str(row['pk']): row for row in Reference._base_manager.select_for_update().filter(pk__in=[int(pk) for pk in chains])
                  .order_by('pk').values('pk', 'code', 'brand', 'clave', 'supplier_id', *FIELDS)}
        for pk, chain in sorted(chains.items(), key=lambda item: int(item[0])):
            row = locked.get(pk)
            updates = [link for link in chain if link['action'] == 'update']
            if row is None or not updates:
                continue
            if row['estado'] == 'retirada' and int(pk) not in own:  # the code retired it after the apply: --purge-retired's
                retired_later.append(row)
                continue
            fields = [field for field in dict.fromkeys(field for link in updates for field in link['after']) if field in RESTORABLE]
            if not fields:
                continue
            now = recorded({field: row[field] for field in fields})
            last = {field: next(link['after'][field] for link in reversed(updates) if field in link['after']) for field in fields}
            first = {field: next(link['before'][field] for link in updates if field in link['before']) for field in fields}
            if now == first:
                continue
            if now != last:
                changed.append(f"referencia {row['code']} ({pk}): cambió después de aplicar")
                continue
            if (first.get('estado') in references.EXCLUSIVE and row['estado'] not in references.EXCLUSIVE and tight
                    and Reference.objects.filter(clave=row['clave'], brand=row['brand'], supplier_id=row['supplier_id'],
                                                 estado__in=references.EXCLUSIVE).exclude(pk=row['pk']).exists()):
                waiting.append(f"referencia {row['code']} ({pk}): otra referencia viva tiene su clave; revierte 0074 y vuelve a ejecutar --rollback")
                continue
            Reference._base_manager.filter(pk=row['pk']).update(**first)
            restored += 1
            log.append({**run, 'model': 'mall.Reference', 'id': int(pk), 'code': row['code'], 'action': 'rollback', 'before': now, 'after': first})
    if purge:
        purged = purge_retired(log, run, keep=own, changed=changed)
    else:
        changed.extend(f"referencia {row['code']} ({row['pk']}): la retiró el código después de aplicar; --purge-retired la borra si nada la nombra"
                       for row in retired_later)
    for pk, chain in by_model.get('mall.ReferenceSource', {}).items():
        created = next((line for line in chain if line['action'] == 'create'), None)
        source = ReferenceSource.objects.select_for_update().filter(pk=pk).first()
        if created is None or source is None:
            continue
        if recorded({'reference_id': source.reference_id, 'kind': source.kind, 'citation': source.citation, 'detail': source.detail}) != created['after']:
            changed.append(f'fuente {source.citation[:60]} ({pk}): cambió después de aplicar, se deja')
            continue
        source.delete()
        deleted += 1
        log.append({**run, 'model': 'mall.ReferenceSource', 'id': int(pk), 'action': 'delete', 'before': created['after']})
    for pk, chain in by_model.get('mall.BrandAlias', {}).items():
        alias = BrandAlias.objects.select_for_update().filter(pk=pk).first()
        if alias is None:
            continue
        if {'alias': alias.alias, 'brand_id': alias.brand_id} != chain[-1]['after']:
            changed.append(f'alias {alias.alias}: cambió después de aplicar')
            continue
        alias.delete()
        deleted += 1
        log.append({**run, 'model': 'mall.BrandAlias', 'id': int(pk), 'action': 'delete', 'before': chain[-1]['after']})
    for pk, chain in sorted(by_model.get('mall.Brand', {}).items(), key=lambda item: -int(item[0])):
        brand = Brand.objects.select_for_update().filter(pk=pk).first()
        if brand is None:
            continue
        if brand.nombre != chain[-1]['after']['nombre'] or brand.aliases.exists() or brand.miembros.exists():
            changed.append(f'marca {brand.nombre}: cambió o tiene alias o miembros desde que se creó, se deja')
            continue
        brand.delete()
        deleted += 1
        log.append({**run, 'model': 'mall.Brand', 'id': int(pk), 'action': 'delete', 'before': chain[-1]['after']})
    return {'restored': restored, 'deleted': deleted, 'purged': purged, 'changed': changed, 'waiting': waiting}


def purge_retired(log, run, *, keep, changed):
    """Delete the retired References no row names as its replacement, again and again until none is left (A → B → C, A and B retired:
    A goes, then B), except `keep` (the rows an apply retired itself, which a rollback puts back). Every row it deletes goes to the log;
    the ones still named are listed in changed. Returns how many went."""
    from .. import references
    from ..models import Reference
    total = 0
    while True:
        candidates = list(Reference._base_manager.filter(estado='retirada').exclude(pk__in=keep).order_by('pk').values_list('pk', 'code', 'part_id'))
        named = set(Reference._base_manager.filter(reemplazado_por__in=[pk for pk, _, _ in candidates])
                    .values_list('reemplazado_por_id', flat=True)) if candidates else set()
        free = [(pk, code, part_id) for pk, code, part_id in candidates if pk not in named]
        if not free:
            break
        for pk, code, part_id in free:
            log.append({**run, 'model': 'mall.Reference', 'id': pk, 'code': code, 'part': str(part_id), 'action': 'delete', 'reason': 'purge-retired'})
        went = references.purge([pk for pk, _, _ in free])
        total += went
        if not went:  # never loops: what purge refused stays and is listed
            break
    changed.extend(f'referencia retirada {code} ({pk}): otra la nombra como reemplazo, se deja' for pk, code, _ in candidates)
    return total


# --- run -----------------------------------------------------------------------------------------------------------------------------

def summary(result):
    final = result['final']
    out = {key: final.get(key, 0) for key in ('filas', 'oem', 'aftermarket', 'interno', 'otro', 'sin_marca', 'fuentes')}
    out.update({estado: final.get(f'estado:{estado}', 0) for estado in ('verificado', 'declarado', 'inferido', 'disputa', 'retirada')})
    return out


def changed_counts(result):
    counts = Counter()
    for before, after in result['changes'].values():
        for field in ('tipo', 'estado'):
            if before[field] != after[field]:
                counts[f'{field}:{after[field]}'] += 1
        if before['printed_forms'] != after['printed_forms']:
            counts['printed_forms'] += 1
    return counts


def print_summary(stdout, mode, totals, counts, result, writes=None):
    title = {'apply': 'Aplicado', 'dry_run': 'Simulación'}[mode]
    stdout.write(f'{title} · S2a referencias (PartCode → Reference)\n')
    stdout.write(f"  {'':22} {'esperado':>9} {'resultado':>10} {'cambian':>8}\n")
    rows = (('filas', 'filas', None), ('oem', 'tipo oem', 'tipo:oem'), ('aftermarket', 'tipo aftermarket', 'tipo:aftermarket'),
            ('otro', 'tipo otro', 'tipo:otro'), ('retirada', 'retirada', 'estado:retirada'), ('sin_marca', "brand = ''", None),
            ('fuentes', 'fuentes', None))
    for key, label, change in rows:
        expected = format(EXPECTED[key], ',') if key != 'fuentes' else '≈' + format(EXPECTED[key], ',')
        moved = len(result['sources']) if key == 'fuentes' else counts.get(change, 0) if change else 0
        stdout.write(f"  {label:22} {expected:>9} {totals[key]:>10,} {moved:>8,}\n")
    stdout.write('  estado: ' + ' · '.join(f'{estado} {totals[estado]:,} ({counts.get("estado:" + estado, 0):,} cambian)'
                                           for estado in ('verificado', 'declarado', 'inferido', 'disputa', 'retirada')) + '\n')
    stdout.write('  fuentes por tipo: ' + ', '.join(f'{kind} {n:,}' for kind, n in sorted(result['kinds'].items())) + '\n')
    if result['duplicates']:
        stdout.write(f"  Grafías duplicadas en una misma línea ({len(result['duplicates'])}): queda una, las demás se retiran\n")
        for entry in result['duplicates'][:20]:
            stocked = sum(1 for items in entry['supplier_items'].values() if items)
            stdout.write(f"    {entry['sku']}: queda {entry['keeper']['code']}, se retira "
                         f"{', '.join(retired['code'] for retired in entry['retired'])} ({stocked} grafías con artículos de proveedor)\n")
    if result['oem_duplicates']:
        stdout.write(f"  Grafías duplicadas de alternos OEM ({len(result['oem_duplicates'])}): la Biblioteca OEM las cita; una persona retira la "
                     "que sobra (mall.references.retire), 0074 no se aplica mientras existan\n")
        for entry in result['oem_duplicates'][:20]:
            stdout.write(f"    {entry['clave']} {entry['brand']}: " + ', '.join(f"{row['code']} en {row['sku']}" for row in entry['rows']) + '\n')
    if result['cross_line']:
        stdout.write(f"  Claves que tienen dos líneas ({len(result['cross_line'])}): necesitan una persona, 0074 no se aplica mientras existan\n")
        for entry in result['cross_line'][:20]:
            stdout.write(f"    {entry['clave']} {entry['brand']}: " + ', '.join(f"{row['code']} en {row['sku']}" for row in entry['rows']) + '\n')
    if result['brands'] or result['aliases']:
        stdout.write(f"  Marcas que faltan ({len(result['brands'])}) y alias ({len(result['aliases'])}): "
                     + ', '.join([*result['brands'], *(f'{alias} → {nombre}' for alias, nombre in result['aliases'].items())][:30]) + '\n')
    else:
        stdout.write('  Marcas: cada marca de las referencias ya es una marca o un alias.\n')
    if result['clave_mismatch']:
        stdout.write(f"  Clave de la base distinta de mall.references.clave(): {len(result['clave_mismatch'])}\n")
        for row in result['clave_mismatch'][:10]:
            stdout.write(f"    {row['code']!r}: base {row['clave']!r}, Python {row['python']!r}\n")
    if writes:
        stdout.write(f"  Escrito: {writes['references']} referencias · {writes['sources']} fuentes · {writes['brands']} marcas · "
                     f"{writes['aliases']} alias\n")


def report(result, totals, counts):
    return {'expected': EXPECTED, 'result': totals, 'changes': dict(counts), 'duplicates': result['duplicates'],
            'cross_line': result['cross_line'], 'oem_duplicates': result['oem_duplicates'], 'source_kinds': dict(result['kinds']),
            'new_sources': len(result['sources']),
            'brands': result['brands'], 'aliases': result['aliases'], 'clave_mismatch': result['clave_mismatch']}


def run(*, apply=False, rollback=False, purge=False, username=None, report_path=None, stdout=None):
    """Plan, and with apply write it, or with rollback undo the applies on this database. None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from ..matching_queue import matching_lock, matching_work
    from ..models import Reference
    stdout = stdout or sys.stdout
    user = None
    if apply or rollback:
        reason = refusal() or manifest_problem()
        if reason:
            stdout.write(reason + '\n')
            return None
        user = get_user_model().objects.filter(username=username or '').first()
        if user is None and (apply or username):  # --rollback names its actor when it can (plan §1.2.3: --rollback alone)
            stdout.write('Indica con --user el usuario que figura como autor del cambio.\n' if not username else
                         f'No existe el usuario {username}.\n')
            return None
        if not expanded():
            stdout.write('Falta la migración 0073_reference en esta base: aplícala primero (la simulación funciona sin ella).\n')
            return None
    run_line = {'stage': STAGE, 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}

    def go():
        log = []
        with transaction.atomic():
            if rollback:
                result = {'mode': 'rollback', **undo(log, run_line, purge=purge)}
            else:
                schema = expanded()
                if apply:  # every Reference locked, so the plan and its writes see the same rows
                    list(Reference._base_manager.select_for_update().order_by('pk').values_list('pk', flat=True))
                elif not schema:
                    stdout.write('Sin 0073 en esta base: cada referencia se lee como la deja la migración (otro, declarado, sin fuentes).\n')
                planned = plan(schema, actor=user.username if user else '')
                totals, counts = summary(planned), changed_counts(planned)
                result = {'mode': 'apply' if apply else 'dry_run', **report(planned, totals, counts)}
                writes = None
                if apply:
                    writes = result['written'] = write(planned, log, run_line, user)
                print_summary(stdout, result['mode'], totals, counts, planned, writes)
                if report_path:
                    with open(report_path, 'w', encoding='utf-8') as handle:
                        json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
                    stdout.write(f'Informe: {report_path}\n')
            if log:  # before the commit: a failed write rolls every row back
                result['manifest'] = str(write_manifest([{**run_line, 'action': 'run', 'mode': result['mode'], 'rows': len(log)}, *log]))
        result['rows_logged'] = len(log)
        if rollback:
            stdout.write(f"Rollback · S2a: {result['restored']} referencias restauradas · {result['deleted']} fuentes, marcas o alias borrados · "
                         f"{result['purged']} retiradas borradas · {len(result['changed'])} cambiadas después (se dejan)\n")
            for line in [*result['waiting'], *result['changed']][:40]:
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
    parser = argparse.ArgumentParser(description='S2a: tipo, estado y fuentes de cada referencia, grafías duplicadas y marcas.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los cambios sin escribir nada (también antes de 0073).')
    mode.add_argument('--apply', action='store_true', help='Escribe todo en una transacción (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base (requiere el congelamiento en vivo).')
    parser.add_argument('--purge-retired', action='store_true',
                        help='Con --rollback: borra también las referencias que el código retiró y que nada nombra (el código anterior las mostraría).')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply; opcional con --rollback).')
    parser.add_argument('--report', help='Escribe en esta ruta (JSON) las grafías duplicadas, las claves de dos líneas, las fuentes y las marcas.')
    parser.add_argument('--json', action='store_true', help='Imprime también el resultado en JSON.')
    options = parser.parse_args(argv)
    if options.purge_retired and not options.rollback:
        parser.error('--purge-retired solo va con --rollback.')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .guard import require_freeze
    require_freeze(options.apply or options.rollback)
    stdout = stdout or sys.stdout
    result = run(apply=options.apply, rollback=options.rollback, purge=options.purge_retired, username=options.user,
                 report_path=options.report, stdout=stdout)
    if result is not None and options.json:
        stdout.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
    return 0 if result is not None else 1


if __name__ == '__main__':
    sys.exit(main())
