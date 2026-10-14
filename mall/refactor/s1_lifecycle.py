"""Stage S1 of the inventory refactor: every line takes the estado and origen of its history, and the Brand table is seeded.

    python -m mall.refactor.s1_lifecycle --dry-run|--apply|--rollback [--user USERNAME] [--report PATH] [--json]

Lines (Part). estado follows the pair the code before S1 wrote (mall.lines.repaired_estado): a merged line is unificada, an inactive
one retirada, the rest publicada (2,472 · 232 · 31,526 on 2026-10-13). A line an approved Productos nuevos proposal created comes from
the supplier (origen proveedor, 123) and takes the proposal's decided_by and decided_at; the rest stay catalogo. An active line in
referencia, preparacion or rechazada (only a person can say which it is) is listed and left alone.

Brands. One row per brand spelling of the 13 brand columns, normalized (upper case, single spaces: 264), plus the brand names of the
code suffix table's brand TAGs (with D4 applied in memory until S0.1 is: a no-op afterwards). A TAG name becomes a brand only when it
names one identified brand: no '/', at least 3 letters, and identified by its attribution or already a brand spelling elsewhere. An
attribution identifies it when it names one brand with nothing unverified or unidentified about it, and either names something other
than the token (DG → DONGIL) or is a confident seed row (DENSO, NGK); an owner-labelled row that only repeats its token ('AMARILLA
(unverified)', 'OEM') identifies nothing. Compound or unidentified names (SPK/WAP, HELM/NDK, 'brands: alternatives', PC, U,
AMARILLA...) go to the report instead. tipo is vehiculo for a library manufacturer or a vehicle make (sku_proposals.vehicle_makes,
oem_links.EXTRA_MAKES and the makes they map to, CATALOG_EXTRA_MAKES), repuesto for a cross-reference brand, a company code's brand or
an identified TAG. A make spelled two ways keeps the library's spelling. Aliases: the whitespace variants as the columns hold them,
the make abbreviations (TOY → TOYOTA, NIS → NISSAN) and the TAG tokens whose brand name differs (DG → DONGIL), except a compound token
(RAZ/NDK), one with fewer than 2 letters (V) or a word that is also another brand's abbreviation (PRO, VAL, VALUE); an alias that
would name two brands, or that is a brand's own name, goes to the report too. Familias: only KAIYI → CHERY and LEXUS → TOYOTA (OD2).
Brands and aliases that exist are kept as they are.

It writes in bulk (no signals) under the matching worker's lock, in one transaction; on the live database --apply and --rollback need
the S0.11 guard (mall.refactor.guard: the catalog freeze is on and no writer is still busy). Every row it changes or creates goes,
with its values before, to mall.refactor.output.manifest_path('s1') (the main checkout's git-ignored output/refactor/s1/,
/app/output/… in the containers, REFACTOR_OUTPUT in a rehearsal) before the transaction commits, so a failed write changes nothing;
the output folder must exist, and a manifest holding another database's lines is refused. Re-running changes nothing new: it runs
before landing (the code before S1 ignores the new columns) and again after, as the catch-up. --rollback puts back what the applies on
this database changed and no rollback has put back yet, unless a row changed since: lines to their values before (when a line changed
between two applies, to the values that change left, and it is listed), and the brands and aliases it created are deleted while
nothing else hangs from them. With 0072's CHECKs in place the lines cannot go back to the pre-S1 defaults, and the code before S1
cannot write lines at all (it writes active and merged_into without estado). So S1 rolls back in this order, from the worktree, under
the freeze: reverse 0072 (python manage.py migrate mall 0071_line_lifecycle; S1's code runs without the CHECKs), then optionally
--rollback, then revert the code (git revert, reload gunicorn, restart the workers), and last, optionally, reverse 0071 (migrate mall
0070_deal_progress, from the worktree, since main no longer has the file). Never revert the code while 0072 is applied. --report
writes every brand with its tipo, sources and aliases, and everything skipped, for the owner.
"""
import argparse
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter, defaultdict

STAGE = 's1'
EXPECTED = {'publicada': 31526, 'unificada': 2472, 'retirada': 232, 'proveedor': 123, 'marcas': 270}  # plan §3 S1, 2026-10-13
COLUMNS = (('SupplierItem', 'brand'), ('Reference', 'brand'), ('OEMReference', 'manufacturer'), ('OEMCrossReference', 'brand'),
           ('SkuProposal', 'brand'), ('SkuProposal', 'oem_brand'), ('SupplierCodeMapping', 'brand'), ('OEMReviewCase', 'brand'),
           ('OEMAutoCandidate', 'brand'), ('OEMFinderChange', 'brand'), ('BasketEvent', 'brand'), ('SupplierRequestLine', 'brand'),
           ('Part', 'legacy_brand'))
TAG_KINDS = ('brand', 'house_brand', 'company_code')  # the suffix TAGs that name a brand (genuine, origin, lifecycle... do not)
FAMILIAS = {'KAIYI': 'CHERY', 'LEXUS': 'TOYOTA'}  # OD2: the other candidates wait for the owner
MIN_LETTERS = 3
MIN_ALIAS_LETTERS = 2  # a suffix token as short as V names nothing outside a code (DG, MO and SH still do)
# Suffix tokens that are words or another brand's abbreviation too: as a spelling of a brand field they would name the wrong brand
# (PRO → PROFIX, VAL → RAZOR VALUE although VAL is VALEO's too). They go to the report for the owner.
GENERIC_ALIASES = frozenset({'NEW', 'OEM', 'PLUS', 'PRO', 'TOP', 'VAL', 'VALUE'})
PART_FIELDS = ('estado', 'active', 'origen', 'decided_by_id', 'decided_at')
CHECK = 'line_active_iff_publicada'  # one of 0072's CHECKs: with it in place the pre-S1 defaults cannot come back
CHUNK = 2000
SKIP_REASONS = {'compound': 'compuesta (con / o varias marcas)', 'short': f'menos de {MIN_LETTERS} letras', 'shape': 'forma de código, no un nombre',
                'unidentified': 'sin identificar (la atribución no la nombra y no es marca en otra columna)'}


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
    """The S0.11 guard (mall.refactor.guard): on the live database, the catalog freeze and no writer still busy. main() exits on it
    first, as the other guarded CLIs do; run() checks it again for a caller that skips main()."""
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


# --- lines ---------------------------------------------------------------------------------------------------------------------------

def approved_origins():
    """{line id: (decided_by id, decided_at)} of the lines approved proposals created (the earliest decision when several did)."""
    from ..sku_proposal_models import SkuProposal
    found = {}
    for part_id, by, at in (SkuProposal.objects.filter(status='approved', part__isnull=False).order_by('decided_at', 'pk')
                            .values_list('part_id', 'decided_by_id', 'decided_at')):
        found.setdefault(part_id, (by, at))
    return found


def target_values(row, origins):
    """(values after the backfill, stuck reason or '') for one line's values (PART_FIELDS plus merged_into_id)."""
    from ..lines import repaired_estado
    after = {field: row[field] for field in PART_FIELDS}
    repaired = repaired_estado(row['active'], row['merged_into_id'], row['estado'])
    stuck = ''
    if repaired is None:
        stuck = f"activa en {row['estado']}" if row['estado'] in ('referencia', 'preparacion', 'rechazada') else f"estado desconocido {row['estado']}"
    else:
        after['estado'], after['active'] = repaired
    origin = origins.get(row['pk'])
    if origin and row['origen'] == 'catalogo':
        after['origen'] = 'proveedor'
    if origin and after['origen'] == 'proveedor' and row['decided_at'] is None:
        after['decided_by_id'], after['decided_at'] = origin
    return after, stuck


def expanded():
    """Whether 0071 is applied here (the lifecycle columns and the brand tables exist). A dry run before it reads every line as 0071
    leaves it (publicada, catalogo, no decision) and no brand, so the counts can be checked before the deploy window."""
    from django.db import connection
    with connection.cursor() as cursor:
        columns = {column.name for column in connection.introspection.get_table_description(cursor, 'mall_part')}
        tables = set(connection.introspection.table_names(cursor))
    return set(PART_FIELDS) <= columns and {'mall_brand', 'mall_brandalias'} <= tables


def part_rows(queryset, schema):
    """The lines' values (PART_FIELDS plus pk, sku and merged_into_id), with 0071's defaults before it is applied."""
    if schema:
        yield from queryset.values('pk', 'sku', 'merged_into_id', *PART_FIELDS).iterator(chunk_size=CHUNK)
        return
    for row in queryset.values('pk', 'sku', 'merged_into_id', 'active').iterator(chunk_size=CHUNK):
        yield {**row, 'estado': 'publicada', 'origen': 'catalogo', 'decided_by_id': None, 'decided_at': None}


def line_plan(schema=True):
    """{'changes': {id: (sku, before, after)}, 'stuck': [{id, sku, reason}], 'final': Counter of estado and origen after the run}."""
    from ..models import Part
    origins = approved_origins()
    changes, stuck, final = {}, [], Counter()
    for row in part_rows(Part.objects.order_by('pk'), schema):
        after, reason = target_values(row, origins)
        before = {field: row[field] for field in PART_FIELDS}
        if reason:
            stuck.append({'id': str(row['pk']), 'sku': row['sku'], 'reason': reason})
        if after != before:
            changes[row['pk']] = (row['sku'], before, after)
        final[after['estado']] += 1
        final[f"origen:{after['origen']}"] += 1
    return {'changes': changes, 'stuck': stuck, 'final': final, 'proposals': len(origins)}


def write_lines(plan, log, run):
    """Lock the lines the plan changes (by id), re-read them, and write each group of equal values in one UPDATE."""
    from ..models import Part
    origins, ids, groups = approved_origins(), sorted(plan['changes']), defaultdict(list)
    for start in range(0, len(ids), CHUNK):
        for row in part_rows(Part.objects.select_for_update().filter(pk__in=ids[start:start + CHUNK]).order_by('pk'), True):
            after, _ = target_values(row, origins)
            before = {field: row[field] for field in PART_FIELDS}
            if after == before:
                continue
            groups[tuple(sorted(after.items()))].append(row['pk'])
            log.append({**run, 'model': 'mall.Part', 'id': str(row['pk']), 'sku': row['sku'], 'action': 'update',
                        'before': recorded(before), 'after': recorded(after)})
    written = 0
    for values, pks in groups.items():
        values = dict(values)
        for start in range(0, len(pks), CHUNK):
            written += Part.objects.filter(pk__in=pks[start:start + CHUNK]).update(**values)
    return written


# --- brands --------------------------------------------------------------------------------------------------------------------------

def named_by_attribution(entry, name):
    """Whether a TAG's attribution identifies the brand `name` it gives (catalog_suffixes.Entry.brand_name): it names one brand, not
    several ('brands: alternatives'), and says nothing unverified or unidentified about it; and it names something other than the
    token, or a confident seed row (high or medium) says it is that brand. Most owner-labelled rows only repeat their own token
    ('AMARILLA' → 'brand: AMARILLA (unverified)', 'OEM' → 'brand: OEM'), which identifies nothing: those names count only when
    another brand column has them, and otherwise go to the report."""
    from ..brands import normalize
    raw = entry.attribution or ''
    text = re.sub(r'^\s*(brands?|supplier)\s*:\s*', '', raw, flags=re.I)
    text = re.sub(r'\(.*?\)', '', text).strip()
    if (not text or re.match(r'^\s*brands\s*:', raw, flags=re.I) or re.search(r'unidentified|unverified', raw, flags=re.I)
            or ' or ' in text or '?' in text or text.lower().startswith(('origin', 'lifecycle', 'commercial'))):
        return False
    return name not in {normalize(entry.token), normalize(entry.canon)} or entry.confidence in ('high', 'medium')


def several_brands(entry):
    """A TAG whose attribution names several brands at once ('brands: alternatives' for RAZ/NDK, PC/VALUE...)."""
    return bool(re.match(r'^\s*brands\s*:', entry.attribution or '', flags=re.I))


def suffix_rows():
    """The code suffix table's rows, with D4 (S0.1) applied in memory while it is not applied: the 45 position and size words are
    no brand names."""
    from .. import catalog_suffixes as cs
    from .s0_suffix_revert import reverted_rows
    rows = cs._db_rows() or []
    return reverted_rows(rows) if rows else rows


def tag_names(spellings):
    """({brand name: [tokens]} of the identified TAG brand names, [{name, tokens, reason}] of the rest)."""
    from .. import catalog_suffixes as cs
    from ..brands import normalize
    rows = suffix_rows()
    table = cs.SuffixTable(rows, source='db')
    names = defaultdict(lambda: {'tokens': [], 'named': False, 'shape': False, 'several': True})
    for row in rows:
        if row['cls'] != 'TAG' or row.get('tag_kind') not in TAG_KINDS or row['token'] == cs.DELETED_TOKEN:
            continue
        entry = table._entry(row)
        name = normalize(entry.brand_name())
        names[name]['tokens'].append(row['token'])
        names[name]['named'] |= named_by_attribution(entry, name)
        names[name]['several'] &= several_brands(entry)
        names[name]['shape'] |= row.get('match') == 'shape' or row['token'].startswith(('CODE:', 'ALT_TAIL:'))
    identified, skipped = {}, []
    for name, info in sorted(names.items()):
        tokens = sorted(info['tokens'])
        reason = ('shape' if info['shape'] else 'compound' if '/' in name or info['several'] else 'unidentified' if ':' in name
                  else 'short' if len(re.findall('[A-Z]', name)) < MIN_LETTERS
                  else '' if info['named'] or name in spellings else 'unidentified')
        if reason:
            skipped.append({'name': name, 'tokens': tokens, 'reason': reason})
        else:
            identified[name] = tokens
    return identified, skipped


def alias_refusal(token):
    """Why a suffix token is not made an alias of the brand its TAG names, or '': compound (SPK/WAP names two), too short, or a word."""
    if '/' in token:
        return 'compuesto (con /): nombra varias marcas'
    if len(re.findall('[A-Z]', token)) < MIN_ALIAS_LETTERS:
        return f'menos de {MIN_ALIAS_LETTERS} letras'
    if token in GENERIC_ALIASES:
        return 'palabra genérica o abreviatura de otra marca'
    return ''


def brand_plan():
    """What the Brand table should hold: {'brands': {nombre: {tipo, sources, aliases}}, 'aliases': {alias: nombre},
    'familias': {member: head}, 'skipped_tags', 'skipped_aliases'} (read-only)."""
    from django.apps import apps
    from django.conf import settings
    from ..brands import normalize
    from ..oem_links import EXTRA_MAKES
    from ..sku_proposals import vehicle_makes
    spellings, written = defaultdict(set), defaultdict(set)
    for model, field in COLUMNS:
        for value in apps.get_model('mall', model)._base_manager.exclude(**{field: ''}).values_list(field, flat=True).distinct():  # retired rows too
            name = normalize(value)
            if name:
                spellings[name].add(f'{model}.{field}')
                written[name].add(value.strip().upper())
    library = {name for name, sources in spellings.items() if 'OEMReference.manufacturer' in sources}
    crossref = {name for name, sources in spellings.items() if 'OEMCrossReference.brand' in sources}
    Reference = apps.get_model('mall', 'Reference')  # PartCode until S2a
    company = {normalize(b) for b in Reference._base_manager.filter(ref_type='company').exclude(brand='').values_list('brand', flat=True).distinct()}
    tags, skipped_tags = tag_names(spellings)
    makes = {normalize(token): normalize(make) for token, make in vehicle_makes().items()}
    vehicle = (library | set(makes.values()) | {normalize(make) for make in EXTRA_MAKES} | {normalize(make) for make in EXTRA_MAKES.values()}
               | {normalize(m) for m in settings.CATALOG_EXTRA_MAKES})
    parts_brand = crossref | company | set(tags)
    # A make spelled two ways keeps the library's spelling (VOLKSWAGEN, not VW; TOYOTA, not the TOY suffix), unless a parts maker's
    # catalog or code uses the other spelling as its own brand (LEX).
    folded = {name: makes[name] for name in set(spellings) | set(tags)
              if makes.get(name, name) != name and makes[name] in library and name not in crossref | company}
    brands = {}
    for name in sorted(set(spellings) | set(tags)):
        if name in folded:
            continue
        sources = sorted(spellings.get(name, ())) + (['sufijo TAG: ' + ' '.join(tags[name][:8])] if name in tags else [])
        tipo = 'vehiculo' if name in vehicle else 'repuesto' if name in parts_brand else ''
        brands[name] = {'tipo': tipo, 'sources': sources}
    wanted, skipped_aliases = defaultdict(dict), []  # {alias: {nombre: why}}
    for name, forms in written.items():
        for form in forms - {name}:
            wanted[form][folded.get(name, name)] = 'variante de espacios'
    for name, make in folded.items():
        for form in written.get(name, set()) | {name}:
            wanted[form][make] = 'grafía de la biblioteca'
    for token, make in makes.items():
        if token != make and make in brands:
            wanted[token][make] = 'abreviatura de la marca de vehículo'
    for name, tokens in tags.items():
        for token in tokens:
            token = token.strip().upper()
            if token == name:
                continue
            reason = alias_refusal(token)
            if reason:
                skipped_aliases.append({'alias': token, 'brands': [name], 'reason': reason})
            else:
                wanted[token][name] = 'sufijo del código'
    aliases = {}
    for alias, targets in sorted(wanted.items()):
        if len(targets) > 1:
            skipped_aliases.append({'alias': alias, 'brands': sorted(targets), 'reason': 'nombraría dos marcas'})
        elif alias in brands or normalize(alias) in set(brands) - set(targets):  # a whitespace variant names its own brand
            skipped_aliases.append({'alias': alias, 'brands': sorted(targets), 'reason': f'{normalize(alias)} ya es una marca'})
        else:
            aliases[alias] = next(iter(targets))
    for alias, nombre in aliases.items():
        brands[nombre].setdefault('aliases', []).append(alias)
    familias = {member: head for member, head in FAMILIAS.items() if member in brands and head in brands}
    return {'brands': brands, 'aliases': aliases, 'familias': familias, 'skipped_tags': skipped_tags, 'skipped_aliases': skipped_aliases}


def write_brands(plan, log, run):
    """Create the planned brands and aliases that do not exist (bulk) and set the two familias where a member has none. Returns
    {'brands', 'aliases', 'familias', 'conflicts': [...]}: a planned name an existing alias already holds, or an alias another brand
    holds, is left as the owner set it."""
    from ..brand_models import Brand, BrandAlias
    existing = {brand.nombre: brand for brand in Brand.objects.select_for_update().order_by('pk')}
    held = {alias.alias: alias.brand_id for alias in BrandAlias.objects.select_for_update().order_by('pk')}
    conflicts = []
    new = []
    for nombre, info in plan['brands'].items():
        if nombre in existing:
            continue
        if nombre in held:
            conflicts.append(f'{nombre}: ya es alias de otra marca, no se crea')
            continue
        new.append(Brand(nombre=nombre, tipo=info['tipo']))
    Brand.objects.bulk_create(new, batch_size=500)
    for brand in new:
        existing[brand.nombre] = brand
        log.append({**run, 'model': 'mall.Brand', 'id': brand.pk, 'action': 'create',
                    'after': {'nombre': brand.nombre, 'tipo': brand.tipo, 'familia_id': None}})
    ids = {brand.pk: nombre for nombre, brand in existing.items()}
    aliases = []
    for alias, nombre in plan['aliases'].items():
        brand = existing.get(nombre)
        if alias in held:
            if brand is None or held[alias] != brand.pk:
                conflicts.append(f'{alias}: ya es alias de {ids.get(held[alias], "otra marca")}, no de {nombre}')
            continue
        if alias in existing or brand is None:
            conflicts.append(f'{alias}: es una marca, no se crea como alias de {nombre}')
            continue
        aliases.append(BrandAlias(alias=alias, brand=brand))
    BrandAlias.objects.bulk_create(aliases, batch_size=500)
    for alias in aliases:
        log.append({**run, 'model': 'mall.BrandAlias', 'id': alias.pk, 'action': 'create',
                    'after': {'alias': alias.alias, 'brand_id': alias.brand_id}})
    familias = 0
    for member, head in plan['familias'].items():
        member, head = existing.get(member), existing.get(head)
        if member is None or head is None or member.familia_id or head.familia_id or Brand.objects.filter(familia=member).exists():
            continue
        Brand.objects.filter(pk=member.pk).update(familia=head)
        familias += 1
        log.append({**run, 'model': 'mall.Brand', 'id': member.pk, 'action': 'update', 'before': {'familia_id': None},
                    'after': {'familia_id': head.pk}})
    return {'brands': len(new), 'aliases': len(aliases), 'familias': familias, 'conflicts': conflicts}


# --- rollback ------------------------------------------------------------------------------------------------------------------------

def applied_lines():
    """{(model, id): [lines]} of what the applies on this database did to each row and no rollback has put back yet, oldest first:
    a rollback line drops the updates before it (a created row keeps its create until it is deleted), a delete the whole row."""
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


def unbroken(chain):
    """The newest links of a row's chain of updates with no change from outside between them (each one's before is the after of the
    one before): a rollback puts back the before of the first of them, so a change made between two applies is kept."""
    run = [chain[-1]]
    for link in reversed(chain[:-1]):
        if link['after'] != run[0]['before']:
            break
        run.insert(0, link)
    return run


def checks_in_place():
    from django.db import connection
    from ..models import Part
    with connection.cursor() as cursor:
        return CHECK in connection.introspection.get_constraints(cursor, Part._meta.db_table)


def undo(log, run):
    """Put back what the applies on this database changed, newest first: {'restored', 'deleted', 'changed': [...]}."""
    from ..brand_models import Brand, BrandAlias
    from ..models import Part
    rows, restored, deleted, changed = applied_lines(), 0, 0, []
    lines_by_model = defaultdict(dict)
    for (model, pk), chain in rows.items():
        lines_by_model[model][pk] = chain
    parts = lines_by_model.get('mall.Part', {})
    if parts:
        locked = {str(row['pk']): row for row in Part.objects.select_for_update().filter(pk__in=list(parts)).order_by('pk')
                  .values('pk', *PART_FIELDS)}
        groups = defaultdict(list)
        for pk, chain in parts.items():
            row = locked.get(pk)
            now = recorded({field: row[field] for field in PART_FIELDS}) if row else None
            links = unbroken(chain) if chain else None
            if now is None or links is None or now == links[0]['before']:  # gone, or already back
                continue
            sku = chain[-1].get('sku') or pk
            if now != chain[-1]['after']:
                changed.append(f'línea {sku}: cambió después de aplicar')
                continue
            if len(links) < len(chain):
                changed.append(f'línea {sku}: cambió entre dos aplicaciones; vuelve a como la dejó ese cambio, no a antes de S1')
            groups[json.dumps(links[0]['before'], sort_keys=True)].append(pk)
            log.append({**run, 'model': 'mall.Part', 'id': pk, 'sku': chain[-1].get('sku'), 'action': 'rollback', 'before': now,
                        'after': links[0]['before']})
        for values, pks in groups.items():
            values = json.loads(values)
            restored += Part.objects.filter(pk__in=pks).update(**values)
    for pk, chain in lines_by_model.get('mall.BrandAlias', {}).items():
        alias = BrandAlias.objects.select_for_update().filter(pk=pk).first()
        if alias is None:
            continue
        if {'alias': alias.alias, 'brand_id': alias.brand_id} != chain[-1]['after']:
            changed.append(f'alias {alias.alias}: cambió después de aplicar')
            continue
        alias.delete()
        deleted += 1
        log.append({**run, 'model': 'mall.BrandAlias', 'id': int(pk), 'action': 'delete', 'before': chain[-1]['after']})
    brands = lines_by_model.get('mall.Brand', {})
    for pk, chain in brands.items():  # familias first: a created brand may head one
        updates = [line for line in chain if line['action'] == 'update']
        brand = Brand.objects.select_for_update().filter(pk=pk).first()
        if brand is None or not updates or brand.familia_id == updates[0]['before']['familia_id']:
            continue
        if brand.familia_id != updates[-1]['after']['familia_id']:
            changed.append(f'marca {brand.nombre}: su familia cambió después de aplicar')
            continue
        Brand.objects.filter(pk=pk).update(familia_id=updates[0]['before']['familia_id'])
        restored += 1
        log.append({**run, 'model': 'mall.Brand', 'id': int(pk), 'action': 'rollback', 'before': {'familia_id': brand.familia_id},
                    'after': updates[0]['before']})
    for pk, chain in sorted(brands.items(), key=lambda item: -int(item[0])):
        created = next((line for line in chain if line['action'] == 'create'), None)
        brand = Brand.objects.select_for_update().filter(pk=pk).first()
        if created is None or brand is None:
            continue
        if brand.nombre != created['after']['nombre'] or brand.aliases.exists() or brand.miembros.exists():
            changed.append(f'marca {brand.nombre}: cambió o tiene alias o miembros desde que se creó, se deja')
            continue
        brand.delete()
        deleted += 1
        log.append({**run, 'model': 'mall.Brand', 'id': int(pk), 'action': 'delete', 'before': created['after']})
    return {'restored': restored, 'deleted': deleted, 'changed': changed}


# --- run -----------------------------------------------------------------------------------------------------------------------------

def summary(lines, brands):
    final = lines['final']
    out = {estado: final.get(estado, 0) for estado in ('publicada', 'unificada', 'retirada', 'referencia', 'preparacion', 'rechazada')}
    out['proveedor'] = final.get('origen:proveedor', 0)
    out['catalogo'] = final.get('origen:catalogo', 0)
    out['marcas'] = len(brands['brands'])
    out['alias'] = len(brands['aliases'])
    out['familias'] = len(brands['familias'])
    return out


def changed_counts(lines):
    counts = Counter()
    for _, before, after in lines['changes'].values():
        if before['estado'] != after['estado'] or before['active'] != after['active']:
            counts[f"estado:{after['estado']}"] += 1
        if before['origen'] != after['origen'] or before['decided_at'] != after['decided_at']:
            counts['origen:proveedor'] += 1
    return counts


def report_brands(brands, existing):
    return [{'nombre': nombre, 'tipo': info['tipo'], 'sources': info['sources'], 'aliases': info.get('aliases', []),
             'familia': brands['familias'].get(nombre, ''), 'exists': nombre in existing} for nombre, info in brands['brands'].items()]


def print_summary(stdout, mode, totals, counts, brands, lines, existing, writes=None):
    title = {'apply': 'Aplicado', 'dry_run': 'Simulación'}[mode]
    stdout.write(f'{title} · S1 ciclo de vida de las líneas y marcas\n')
    stdout.write(f"  {'':22} {'esperado':>9} {'resultado':>10} {'cambian':>8}\n")
    for key, label in (('publicada', 'publicada'), ('unificada', 'unificada'), ('retirada', 'retirada')):
        stdout.write(f"  {label:22} {EXPECTED[key]:>9,} {totals[key]:>10,} {counts.get('estado:' + key, 0):>8,}\n")
    stdout.write(f"  {'origen proveedor':22} {EXPECTED['proveedor']:>9,} {totals['proveedor']:>10,} {counts.get('origen:proveedor', 0):>8,}"
                 f"   (propuestas aprobadas: {lines['proposals']})\n")
    new_brands = sum(1 for nombre in brands['brands'] if nombre not in existing)
    stdout.write(f"  {'marcas':22} {'≈' + format(EXPECTED['marcas'], ','):>9} {totals['marcas']:>10,} {new_brands:>8,}"
                 f"   (alias {totals['alias']}, familias {totals['familias']})\n")
    columns = sum(1 for info in brands['brands'].values() if any(not s.startswith('sufijo') for s in info['sources']))
    only_tags = totals['marcas'] - columns
    stdout.write(f'  marcas de las 13 columnas: {columns} · solo de sufijos TAG: {only_tags} · tipo: '
                 + ', '.join(f'{tipo or "sin tipo"} {n}' for tipo, n in sorted(Counter(i['tipo'] for i in brands['brands'].values()).items()))
                 + '\n')
    if lines['stuck']:
        stdout.write(f"  Líneas que necesitan una persona (no se tocan): {len(lines['stuck'])}\n")
        for row in lines['stuck'][:20]:
            stdout.write(f"    {row['sku']}: {row['reason']}\n")
    if brands['skipped_tags']:
        by_reason = Counter(row['reason'] for row in brands['skipped_tags'])
        stdout.write(f"  Nombres de sufijos TAG que no se siembran ({len(brands['skipped_tags'])}): "
                     + ' · '.join(f'{SKIP_REASONS[r]} {n}' for r, n in sorted(by_reason.items())) + '\n')
        for reason in ('compound', 'short'):
            names = [row['name'] for row in brands['skipped_tags'] if row['reason'] == reason]
            if names:
                stdout.write(f"    {SKIP_REASONS[reason]}: {', '.join(names[:40])}{' …' if len(names) > 40 else ''}\n")
    if brands['skipped_aliases']:
        stdout.write(f"  Alias que no se crean ({len(brands['skipped_aliases'])}): "
                     + ', '.join(f"{row['alias']} ({row['reason']})" for row in brands['skipped_aliases'][:15])
                     + (' …' if len(brands['skipped_aliases']) > 15 else '') + '\n')
    if writes:
        stdout.write(f"  Escrito: {writes['lines']} líneas · {writes['brands']['brands']} marcas · {writes['brands']['aliases']} alias · "
                     f"{writes['brands']['familias']} familias\n")
        for conflict in writes['brands']['conflicts'][:20]:
            stdout.write(f'    {conflict}\n')


def run(*, apply=False, rollback=False, username=None, report_path=None, stdout=None):
    """Plan, and with apply write it, or with rollback undo the applies on this database. None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from ..brand_models import Brand
    from ..matching_queue import matching_lock, matching_work
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
            stdout.write('Falta la migración 0071_line_lifecycle en esta base: aplícala primero (la simulación funciona sin ella).\n')
            return None
        if rollback and checks_in_place():
            stdout.write('Los CHECK de 0072 están puestos: las líneas no pueden volver a los valores de antes de S1. Revierte primero 0072 '
                         '(python manage.py migrate mall 0071_line_lifecycle) y vuelve a ejecutar.\n')
            return None
    run_line = {'stage': STAGE, 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}

    def go():
        log = []
        with transaction.atomic():
            if rollback:
                result = {'mode': 'rollback', **undo(log, run_line)}
            else:
                schema = expanded()
                lines, brands = line_plan(schema), brand_plan()
                existing = set(Brand.objects.values_list('nombre', flat=True)) if schema else set()
                if not schema:
                    stdout.write('Sin 0071 en esta base: cada línea se lee como la deja la migración (publicada, catálogo).\n')
                totals, counts = summary(lines, brands), changed_counts(lines)
                result = {'mode': 'apply' if apply else 'dry_run', 'expected': EXPECTED, 'result': totals, 'changes': dict(counts),
                          'stuck_lines': lines['stuck'], 'skipped_tags': brands['skipped_tags'], 'skipped_aliases': brands['skipped_aliases']}
                writes = None
                if apply:
                    writes = {'lines': write_lines(lines, log, run_line), 'brands': write_brands(brands, log, run_line)}
                    result['written'] = writes
                print_summary(stdout, result['mode'], totals, counts, brands, lines, existing, writes)
                if report_path:
                    with open(report_path, 'w', encoding='utf-8') as handle:
                        json.dump({**result, 'brands': report_brands(brands, existing)}, handle, ensure_ascii=False, indent=1, default=str)
                    stdout.write(f'Informe: {report_path}\n')
            if log:  # before the commit: a failed write rolls every row back
                result['manifest'] = str(write_manifest([{**run_line, 'action': 'run', 'mode': result['mode'], 'rows': len(log)}, *log]))
        result['rows_logged'] = len(log)
        if rollback:
            stdout.write(f"Rollback · S1: {result['restored']} filas restauradas · {result['deleted']} marcas o alias borrados · "
                         f"{len(result['changed'])} cambiadas después (se dejan)\n")
            for line in result['changed'][:30]:
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
    parser = argparse.ArgumentParser(description='S1: estado y origen de cada línea del catálogo y la tabla de marcas.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los cambios y las marcas sin escribir nada.')
    mode.add_argument('--apply', action='store_true', help='Escribe todo en una transacción (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base (requiere el congelamiento en vivo).')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply; opcional con --rollback).')
    parser.add_argument('--report', help='Escribe en esta ruta (JSON) cada marca con su tipo, fuentes y alias, y lo que no se siembra.')
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
