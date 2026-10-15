"""Stage S6.2 of the inventory refactor (plan §3 S6, data step 2 and step 4 for the items it places): the pending Productos nuevos
(SkuProposal) become lines in preparación with their ids kept, and Revisión IA follows them.

    python -m mall.refactor.s6_new_products --dry-run|--apply --user USERNAME|--rollback [--report PATH] [--json]

It runs before S6 lands, once 0083/0084 are applied (the code before S6 never reads lines in preparación, and the freeze keeps the
matching worker from proposing meanwhile), and again after, as a catch-up: a proposal already turned into a line is left alone.

Each pending proposal (plan §3 S6 data step 2):
- its items still unplaced (pending, no line, no Reference) hang as pending from its line; one placed since is listed and stays;
- its main (mall.new_products.main_plan): the proposal's code under its OEM maker, oem, when its oem_code is its own code (a dealer's
  genuine part); the supplier's own code (interno) for internal_code, a brand that is the supplier's name (OD20) or a short code
  with no brand (OD6); otherwise its brand's public tipo (aftermarket for a brand of parts); an OEM-like code with no brand, '';
- a new line in preparación, origen proveedor, id = the proposal's id, with its description, subgrupo, field_sources, version,
  created_at and its last change as changed_at; its main holds the code;
- unless a live line outside the catalog already holds that key (a line in referencia: a library number no product carried, rule 7;
  or a line this run made for another proposal of the same key): the code joins that line, which moves to preparación, and the
  proposal maps to it. A key on a catalog line or on two lines is listed and left (the engine attaches it, or a line case decides);
- an OEM number that is not its own code (the library's or an administrator's) joins the same way when a line in referencia or
  preparación holds it, is listed when a catalog line does, else becomes an oem Reference of the new line;
- an administrator's alternos become References; the codes its items declare become reference cases (rule 5b, one per item and
  code: mall.new_products.declared_cases);
- the legacy supplier cases of the items it places, unmatched or in review, are dismissed ('Colocado en Productos nuevos', step 4).
Then the Revisión IA verdicts point to their line (a pending proposal's line, an approved one's SKU), and each run's proposal_ids are
remapped (pending → its line, approved → its SKU); a run left with ids that map to nothing is closed at what it reviewed, with a
note. Resolved proposals are written to output/refactor/s6/proposals_archive.json. No SkuProposal row is ever changed.

It writes in one transaction under the matching worker's lock, the supplier Accounts and then the items locked; lines and mains in
bulk (mall.lines.bulk_create_lines, mall.references.bulk_create), the items through mall.variants.hang. On the live database --apply
and --rollback need the catalog freeze (mall.refactor.guard). Every row it creates or changes goes, with its values before, to
output/refactor/s6/new_products_manifest.jsonl before the transaction commits.

Before 0083/0084 are applied its dry run is a simulation (cases and verdicts are only counted), and before S4's backfill no proposal
can join a library number. A rehearsal copy gives the real numbers.

--rollback (plan S6 rollback 1; run python -m mall.refactor.s6_cases --rollback first), for the code before S6, which reads proposals:
1. Revisión IA's verdicts since landing (line only): a line that was a proposal gives the verdict its proposal back, the others are
   deleted (logged), so 0084 reverses.
2. The lines S6 made (this manifest; a Reference tagged detail['s6'] by a proposal or mall.new_products.place, an edited main
   included; any line from a supplier in preparación or rechazada, which no code before S6 made). A line that was a pending proposal
   writes back to it what was decided or edited since (approved, linked, rejected; its codigo, description, subgrupo, OEM number,
   código interno, alternos, version). Still in preparación or rechazada, it loses its items (pending, no Reference) and is deleted
   with its References when nothing else points at them (images, favourites, specifications... keep it, listed); published or merged
   since, it stays, listed. A rejection of a line that was no proposal is lost, listed.
3. The lines rule 7 moved from referencia (this manifest's joins; the rule-7 mark in field_sources, mall.lines.prepared_q, for the
   engine's moves after landing), still in preparación or rechazada: their items detached, the References S6 added to them purged
   (this manifest's, tagged ones, any created since the apply), back to referencia with their version and last change.
4. The cases this manifest created are deleted and the legacy ones it dismissed come back; every case the S6 code opened (keys
   reference:, code_change:, variant:, line:, merge:) is deleted, logged with its values; verdicts and runs get their values back.
References S6 added to catalog lines (rules 3 and 3e, approved declared codes) stay: the code before S6 reads them as alternos. Then
reverse 0084 and 0083 from the worktree.
"""
import argparse
import json
import os
import shutil
import sys
import uuid
from collections import Counter, defaultdict

STAGE = 's6'
MANIFEST = 'new_products_manifest.jsonl'
ARCHIVE = 'proposals_archive.json'
# The live dry run of 2026-10-14, after S4 (14,669 lines in referencia) and before 0083/0084 (cases and verdicts only counted): 4,175
# pending proposals, all of Motion Motors, none of whose codes the library or another line holds (a run before S4's backfill gave the
# same numbers). Mains: 3,962 dealer codes
# (KAIYI 3,013, DFSK 949) oem; 51 interno (23 internal_code, 28 under MOTION MOTORS: OD20); 104 aftermarket (SERES 46, GENIUS 12...);
# 58 otro (50 OEM-like codes with no brand, 8 brands of no tipo). 138 declared codes become reference cases; the 4,184 items' legacy
# cases are dismissed; the 1,993 verdicts and 3 runs map whole (none closed). The live counts move with the suppliers' loads.
EXPECTED = {'propuestas': 4175, 'lineas_nuevas': 4175, 'unidas': 0, 'en_catalogo': 0, 'conflictos': 0, 'sin_articulos': 0,
            'articulos': 4184, 'oem': 3962, 'interno': 51, 'aftermarket': 104, 'otro': 58, 'casos_declarados': 138,
            'casos_legados': 4184, 'veredictos': 1993, 'corridas': 3}
CHUNK = 2000
TAG = 's6'


def current_database():
    from django.db import connection
    return str(connection.settings_dict['NAME'])


def stage_dir():
    from .output import stage_dir as shared
    return shared(STAGE)


def manifest_path():
    return stage_dir() / MANIFEST


def manifest_lines():
    path = manifest_path()
    if not path.exists():
        return []
    with open(path, encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def manifest_problem():
    """Why the manifest cannot take this run's lines, or None (the output folder is missing, or it holds another database's lines)."""
    from .output import main_checkout
    if not os.environ.get('REFACTOR_OUTPUT', '').strip() and not (main_checkout() / 'output').is_dir():
        return f'Falta la carpeta {main_checkout() / "output"} para el manifiesto; ejecútalo dentro del contenedor de la app.'
    others = sorted({str(line.get('database')) for line in manifest_lines()} - {current_database()})
    if others:
        return (f"El manifiesto {manifest_path()} tiene líneas de otra base ({', '.join(others)}): muévelo antes de aplicar o deshacer "
                'en esta.')
    return None


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
    """{'verdict_line': 0084 applied (SkuReviewVerdict.line), 'cases': 0083 applied (MatchingCase.new_code), 'referencia': S4's
    backfill made its lines in referencia}."""
    from django.db import connection
    with connection.cursor() as cursor:
        verdicts = {c.name for c in connection.introspection.get_table_description(cursor, 'mall_skureviewverdict')}
        cases = {c.name for c in connection.introspection.get_table_description(cursor, 'mall_matchingcase')}
        cursor.execute("SELECT EXISTS (SELECT 1 FROM mall_part WHERE estado = 'referencia')")
        referencia = bool(cursor.fetchone()[0])
    return {'verdict_line': 'line_id' in verdicts, 'cases': 'new_code' in cases, 'referencia': referencia}


# --- plan -----------------------------------------------------------------------------------------------------------------------------

def free(item):
    return item.part_id is None and item.reference_id is None and item.matching_status == 'pending'


def clashes(rows, brand, owner):
    """The exclusive rows a (clave, brand, owner) key clashes with (mall.references.conflicts, on rows already read): an interno key
    with the same supplier's same brand; a public one with no brand with any public row, with a brand with that brand or none."""
    if owner is not None:
        return [row for row in rows if row['supplier_id'] == owner and row['brand'] == brand]
    return [row for row in rows if row['supplier_id'] is None and (not brand or row['brand'] in (brand, ''))]


def holder_of(rows, brand, owner):
    """(the row a key joins, why) among clashing rows: the one line outside the catalog that holds them (the exact key first), or None
    with 'libre' (nothing holds it), 'en_catalogo' (a catalog line does) or 'conflicto' (two or more lines do)."""
    hits = clashes(rows, brand, owner)
    if not hits:
        return None, 'libre'
    if len({row['part_id'] for row in hits}) > 1:
        return None, 'conflicto'
    row = sorted(hits, key=lambda row: (row['brand'] != brand, not row['is_main'], str(row['pk'])))[0]
    if row['merged'] or row['estado_linea'] in ('publicada', 'retirada', 'unificada'):
        return None, 'en_catalogo'
    return row, 'unir'


def converted():
    """{proposal id: its line id} for the proposals an earlier apply turned into lines (the S6 tag on their main, or on the Reference
    their code added to the line it joined)."""
    from ..models import Reference
    found = {}
    for part, detail in Reference.all_objects.filter(detail__has_key=TAG).values_list('part_id', 'detail'):
        proposal = (detail.get(TAG) or {}).get('propuesta')
        if proposal:
            found.setdefault(str(proposal), str(part))
    return found


def plan():
    """What --apply would write, read-only: one entry per pending proposal, the counts, and what is listed."""
    from django.db.models import Count
    from .. import references
    from ..models import Part, Reference
    from ..new_products import canonical_brand, main_plan
    from ..sku_proposal_models import SkuProposal
    from ..sku_proposals import merge_references
    from ..sku_review_models import SkuReviewRun, SkuReviewVerdict
    proposals = list(SkuProposal.objects.filter(status='pending').select_related('part_type__grupo')
                     .prefetch_related('items__item__supplier').order_by('created_at', 'pk'))
    lined = converted()
    claves = set()
    for proposal in proposals:
        claves.add(references.clave(proposal.sku))
        if proposal.oem_code:
            claves.add(references.clave(proposal.oem_code))
    held = defaultdict(list)
    keys = sorted(claves)
    for start in range(0, len(keys), CHUNK):
        for row in (Reference.all_objects.filter(clave__in=keys[start:start + CHUNK], estado__in=references.EXCLUSIVE)
                    .values('pk', 'clave', 'brand', 'supplier_id', 'part_id', 'is_main', 'part__estado', 'part__merged_into_id')):
            held[row['clave']].append({**row, 'estado_linea': row['part__estado'], 'merged': bool(row['part__merged_into_id'])})
    counts, listed, entries, planned = Counter(), defaultdict(list), [], {}
    for proposal in proposals:
        counts['propuestas'] += 1
        if str(proposal.pk) in lined:  # turned into a line by an earlier run: the catch-up leaves it
            counts['ya_convertidas'] += 1
            continue
        items = [row.item for row in proposal.items.all()]
        waiting = [item for item in items if free(item)]
        for item in items:
            if not free(item):
                listed['articulos_colocados'].append(f'{proposal.sku}: {item.codigo} ({item.supplier_invent_id}) ya tiene línea')
        if not waiting:
            counts['sin_articulos'] += 1
            listed['sin_articulos'].append(proposal.sku)
            continue
        suppliers = Counter(item.supplier_id for item in waiting)
        most = sorted(suppliers, key=lambda pk: (-suppliers[pk], str(pk)))[0]  # an interno main is the supplier with most items
        lead = next(item.supplier for item in waiting if item.supplier_id == most)
        own = bool(proposal.oem_code) and references.clave(proposal.oem_code) == references.clave(proposal.sku)
        brand, tipo, owner = main_plan(proposal.sku, proposal.brand, lead, internal=proposal.internal_code,
                                       oem_brand=proposal.oem_brand if own else '')
        code, key = references.code_key(proposal.sku), references.clave(proposal.sku)
        entry = {'proposal': proposal, 'items': waiting, 'code': code, 'clave': key, 'brand': brand, 'tipo': tipo, 'owner': owner,
                 'line': proposal.pk, 'ref': None, 'action': 'create', 'oem': None, 'held_oem': None}
        mine = [row for row in held.get(key, [])]
        mine += [{'pk': None, 'clave': key, 'brand': other['brand'], 'supplier_id': other['owner'], 'part_id': other['line'],
                  'is_main': True, 'estado_linea': 'preparacion', 'merged': False, 'entry': other}
                 for other in planned.get(key, [])]
        row, why = holder_of(mine, brand, owner)
        if why in ('en_catalogo', 'conflicto'):
            counts[why] += 1
            listed[why].append(f'{proposal.sku} ({brand or "sin marca"}): {why.replace("_", " ")}')
            continue
        if row is not None:
            entry.update(action='join', line=row['part_id'], ref=row['pk'], joins=row.get('entry'),
                         line_estado=row['estado_linea'])
        if proposal.oem_code and not own:
            oem_key, oem_brand = references.clave(proposal.oem_code), canonical_brand(proposal.oem_brand)
            other, how = holder_of(held.get(oem_key, []), oem_brand, None)
            entry['oem'] = {'code': references.code_key(proposal.oem_code), 'brand': oem_brand, 'how': how}
            if how == 'unir' and entry['action'] == 'create':
                entry.update(action='join_oem', line=other['part_id'], ref=None, line_estado=other['estado_linea'])
            elif how in ('en_catalogo', 'conflicto'):
                entry['held_oem'] = how
                listed['oem_en_otra_linea'].append(f'{proposal.sku}: su número OEM {proposal.oem_code} ({how.replace("_", " ")})')
        entries.append(entry)
        if entry['action'] == 'create':
            planned.setdefault(key, []).append(entry)
            counts['lineas_nuevas'] += 1
            counts[tipo] += 1
        else:
            counts['unidas'] += 1
            listed['unidas'].append(f'{proposal.sku} → línea {entry["line"]} ({entry.get("line_estado")})')
        counts['articulos'] += len(waiting)
        for item in waiting:
            declared = {(references.clave(ref['code']), references.brand_key(ref.get('brand', '')))
                        for ref in merge_references(item.references or [])}
            counts['casos_declarados'] += len({pair for pair in declared if pair[0] != key})
    from ..matching_models import MatchingCase
    placed = [item.pk for entry in entries for item in entry['items']]
    for start in range(0, len(placed), CHUNK):
        counts['casos_legados'] += MatchingCase.objects.filter(item_id__in=placed[start:start + CHUNK], kind='supplier',
                                                               status__in=('unmatched', 'review')).count()
    mapping = {**lined, **{str(entry['proposal'].pk): str(entry['line']) for entry in entries}}
    mapping.update({str(pk): str(part) for pk, part in SkuProposal.objects.filter(status__in=['approved', 'linked'], part__isnull=False)
                    .values_list('pk', 'part_id')})
    verdicts = SkuReviewVerdict.objects.values_list('pk', 'proposal_id')
    counts['veredictos'] = sum(1 for _, proposal in verdicts if str(proposal) in mapping)
    counts['veredictos_sin_linea'] = sum(1 for _, proposal in verdicts if str(proposal) not in mapping)
    runs, every = [], list(SkuReviewRun.objects.order_by('created_at'))
    asked = sorted({str(pk) for run in every for pk in run.proposal_ids} - set(mapping))
    known = set()  # ids that are lines already (a run an earlier apply remapped): each stays
    for start in range(0, len(asked), CHUNK):
        known |= {str(pk) for pk in Part._base_manager.filter(pk__in=asked[start:start + CHUNK]).values_list('pk', flat=True)}
    for run in every:
        mapped = [mapping.get(str(pk)) or (str(pk) if str(pk) in known else None) for pk in run.proposal_ids]
        total = -(-len(run.proposal_ids) // run.batch_size) if run.proposal_ids else 0
        finished = run.completed_batches >= total
        close = None in mapped and not finished
        runs.append({'run': run, 'mapped': mapped, 'close': close, 'missing': mapped.count(None), 'finished': finished})
        counts['corridas'] += 1
        if close:
            counts['corridas_cerradas'] += 1
            listed['corridas_cerradas'].append(f'{run.pk}: {mapped.count(None)} de {len(mapped)} ya no son productos por revisar')
    archive = list(SkuProposal.objects.filter(status='resolved').prefetch_related('items').order_by('created_at', 'pk'))
    counts['archivadas'] = len(archive)
    history = dict(SkuProposal.objects.exclude(status__in=['pending', 'resolved']).values_list('status').annotate(n=Count('pk')))
    return {'entries': entries, 'counts': counts, 'listed': listed, 'runs': runs, 'mapping': mapping, 'archive': archive,
            'history': history}


# --- apply ----------------------------------------------------------------------------------------------------------------------------

def fits(ref, entry):
    """Whether a proposal's items hang from `ref` as it is: the same clave and supplier, and the same brand or (public) one of the two
    brandless (a branded item joins the brandless Reference of its clave, a brandless one the branded: rule 2)."""
    from .. import references
    if references.clave(ref.code) != entry['clave'] or ref.supplier_id != entry['owner']:
        return False
    return ref.brand == entry['brand'] or (entry['owner'] is None and (not ref.brand or not entry['brand']))


def write(planned, log, run, user):
    """Write the plan (inside the caller's transaction). Returns the counts written."""
    from django.db import transaction
    from django.db.models import F
    from django.utils import timezone
    from .. import lines, references, variants
    from ..matching_models import MatchingCase
    from ..models import Part, Reference
    from ..new_products import PLACED_REASON, canonical_brand, citation, declared_cases
    from ..reference_matching import written_brand
    from ..sku_review_models import SkuReviewRun, SkuReviewVerdict
    written, now = Counter(), timezone.now()
    entries = planned['entries']
    stamp = {'como': 'propuesta', 'run': run['run'], 'at': now.isoformat()}

    def log_create(model, pk, after=None):
        log.append({**run, 'model': model, 'id': str(pk), 'action': 'create', **({'after': recorded(after)} if after else {})})

    # 1. The new lines and their mains, in bulk.
    creates = [entry for entry in entries if entry['action'] == 'create']
    new_lines = []
    for entry in creates:
        proposal = entry['proposal']
        line = Part(id=proposal.pk, sku=entry['code'], name='', description=proposal.description, estado='preparacion', origen='proveedor',
                    field_sources=dict(proposal.field_sources or {}), version=proposal.version, created_at=proposal.created_at,
                    changed_at=proposal.updated_at, tipo=references.line_tipo(entry['tipo']))
        line.is_OEM = line.tipo == 'OEM'
        if proposal.part_type_id:
            lines.set_subgrupo(line, proposal.part_type)
        new_lines.append(line)
    lines.bulk_create_lines(new_lines, actor=user, mains=False)
    mains = []
    for entry, line in zip(creates, new_lines):
        main = references.build(line, entry['code'], entry['brand'], tipo=entry['tipo'], supplier=entry['owner'],
                                source=citation(entry['items'][0], line), actor=user,
                                detail={TAG: {**stamp, 'propuesta': str(entry['proposal'].pk)}})
        main.is_main = True
        mains.append(main)
    references.bulk_create(mains, serialized=True, actor=user)
    for entry, line, main in zip(creates, new_lines, mains):
        entry['line_obj'], entry['main_obj'] = line, main
        log_create('mall.Part', line.pk, {'sku': line.sku, 'estado': line.estado, 'propuesta': str(entry['proposal'].pk)})
        log_create('mall.Reference', main.pk, {'part_id': line.pk, 'code': main.code, 'brand': main.brand, 'tipo': main.tipo,
                                               'supplier_id': main.supplier_id, 'is_main': True})
    written['lineas'] = len(new_lines)
    written['principales'] = len(mains)

    def hang(items, ref):
        if not items:
            return
        variants.hang([item.pk for item in items], ref)
        for item in items:
            log.append({**run, 'model': 'mall.SupplierItem', 'id': str(item.pk), 'action': 'hang',
                        'before': {'reference_id': None, 'part_id': None, 'matching_status': item.matching_status},
                        'after': recorded({'reference_id': ref.pk, 'part_id': ref.part_id})})
        written['articulos'] += len(items)

    def own_ref(line, item):
        """The Reference of `line` an item that is not of the main's key hangs from: its supplier's own code there (interno)."""
        ref, added = references.add(line, item.codigo, written_brand(item.brand), tipo='interno',
                                    supplier=item.supplier_id, source=citation(item, line), actor=user,
                                    detail={TAG: {**stamp, 'como': 'articulo'}})
        if added:
            log_create('mall.Reference', ref.pk, {'part_id': line.pk, 'code': ref.code, 'supplier_id': ref.supplier_id})
            written['referencias'] += 1
        return ref

    def place_items(entry, line, ref):
        same, other = [], []
        for item in entry['items']:
            fits = references.clave(item.codigo) == ref.clave and ref.supplier_id in (None, item.supplier_id)
            (same if fits else other).append(item)
        hang(same, ref)
        for item in other:
            hang([item], own_ref(line, item))

    # 2. The items of the new lines.
    for entry in creates:
        place_items(entry, entry['line_obj'], entry['main_obj'])

    # 3. Joins: the code joins a line outside the catalog that holds its key (or its OEM number).
    for entry in entries:
        if entry['action'] == 'create':
            continue
        line = lines.all_lines(Part.objects.select_for_update()).get(pk=entry['line'])
        before = {'estado': line.estado, 'version': line.version, 'changed_at': line.changed_at, 'decided_by_id': line.decided_by_id,
                  'decided_at': line.decided_at}
        if entry['action'] == 'join' and entry.get('joins') is not None:
            ref = entry['joins']['main_obj']
        elif entry['action'] == 'join':
            ref = Reference.all_objects.get(pk=entry['ref'])
        else:
            ref = None
        if ref is None or not fits(ref, entry):  # the proposal's code on the line it joins (its key's own Reference)
            ref, added = references.add(line, entry['code'], entry['brand'], tipo=entry['tipo'], supplier=entry['owner'],
                                        source=citation(entry['items'][0], line), actor=user,
                                        detail={TAG: {**stamp, 'como': 'union', 'propuesta': str(entry['proposal'].pk)}})
            if added:
                log_create('mall.Reference', ref.pk, {'part_id': line.pk, 'code': ref.code, 'brand': ref.brand})
                written['referencias'] += 1
        elif TAG not in (ref.detail or {}):  # the key's Reference already there: tagged, so a catch-up knows the proposal is done
            references.update(ref, detail={**(ref.detail or {}), TAG: {**stamp, 'como': 'union', 'propuesta': str(entry['proposal'].pk)}},
                              actor=user)
        place_items(entry, line, ref)
        if line.estado == 'referencia':
            lines.prepare(line, user=user)
        elif line.estado == 'preparacion' and entry.get('joins') is None:
            Part.objects.filter(pk=line.pk).update(version=F('version') + 1, changed_at=now)
        log.append({**run, 'model': 'mall.Part', 'id': str(line.pk), 'action': 'join', 'before': recorded(before),
                    'propuesta': str(entry['proposal'].pk)})
        entry['line_obj'] = line
        written['uniones'] += 1

    # 4. OEM numbers that are not the proposal's own code, an administrator's alternos, the declared codes.
    for entry in entries:
        line, proposal = entry['line_obj'], entry['proposal']
        if entry['oem'] and entry['oem']['how'] == 'libre':
            source = (proposal.field_sources or {}).get('oem') or {}
            text = (f'Biblioteca OEM: {source.get("note") or "equivalencia de posventa"}' if source.get('source') == 'oem_library'
                    else f'Indicado por {source.get("by") or "un administrador"}' if source.get('source') == 'admin'
                    else f'Investigación IA: {source.get("note") or "sin cita"}' if source.get('source') == 'ai'
                    else citation(entry['items'][0]))
            try:
                with transaction.atomic():
                    ref, added = references.add(line, entry['oem']['code'], entry['oem']['brand'], tipo='oem', actor=user,
                                                source=f'{text} · producto nuevo {str(proposal.pk)[:8]}'[:500], detail={TAG: {**stamp, 'como': 'oem'}})
            except references.ReferenceConflict as error:  # another proposal of this run took the number first
                planned['listed']['oem_no_agregado'].append(f'{proposal.sku}: {entry["oem"]["code"]} ({error.message})')
                added = False
            if added:
                log_create('mall.Reference', ref.pk, {'part_id': line.pk, 'code': ref.code, 'brand': ref.brand, 'tipo': 'oem'})
                written['referencias'] += 1
        if ((proposal.field_sources or {}).get('references') or {}).get('source') == 'admin':
            by = ((proposal.field_sources or {}).get('references') or {}).get('by') or 'un administrador'
            for row in proposal.references or []:
                try:
                    with transaction.atomic():
                        ref, added = references.add(line, row['code'], row.get('brand', ''), ref_type=row.get('ref_type', 'unknown'),
                                                    source=f'Indicado por {by} · producto nuevo {str(proposal.pk)[:8]}'[:500], actor=user,
                                                    detail={TAG: {**stamp, 'como': 'alterno'}})
                except references.ReferenceConflict as error:
                    planned['listed']['alternos_no_agregados'].append(f'{proposal.sku}: {row["code"]} ({error.message})')
                    continue
                if added:
                    log_create('mall.Reference', ref.pk, {'part_id': line.pk, 'code': ref.code})
                    written['referencias'] += 1
        for item in entry['items']:
            for case in declared_cases(item, line):
                log_create('mall.MatchingCase', case.pk, {'key': case.key})
                written['casos'] += 1

    # 5. The legacy supplier cases of the items placed (step 4).
    placed = [item.pk for entry in entries for item in entry['items']]
    for start in range(0, len(placed), CHUNK):
        rows = MatchingCase.objects.filter(item_id__in=placed[start:start + CHUNK], kind='supplier', status__in=('unmatched', 'review'))
        for pk, status, reason in rows.values_list('pk', 'status', 'reason'):
            log.append({**run, 'model': 'mall.MatchingCase', 'id': str(pk), 'action': 'update',
                        'before': {'status': status, 'reason': reason}, 'after': {'status': 'dismissed', 'reason': PLACED_REASON}})
        written['casos_legados'] += rows.update(status='dismissed', reason=PLACED_REASON)

    # 6. Verdicts point to their line; runs are remapped.
    mapping = planned['mapping']
    by_line = defaultdict(list)
    for proposal, line in mapping.items():
        by_line[line].append(proposal)
    same = [proposal for proposal, line in mapping.items() if proposal == line]
    for start in range(0, len(same), CHUNK):
        rows = SkuReviewVerdict.objects.filter(proposal_id__in=same[start:start + CHUNK], line__isnull=True)
        ids = [str(pk) for pk in rows.values_list('pk', flat=True)]
        if ids:
            log.append({**run, 'model': 'mall.SkuReviewVerdict', 'ids': ids, 'action': 'update', 'before': {'line_id': None}})
            written['veredictos'] += rows.update(line_id=F('proposal_id'))
    for proposal, line in mapping.items():
        if proposal == line:
            continue
        rows = SkuReviewVerdict.objects.filter(proposal_id=proposal, line__isnull=True)
        ids = [str(pk) for pk in rows.values_list('pk', flat=True)]
        if ids:
            log.append({**run, 'model': 'mall.SkuReviewVerdict', 'ids': ids, 'action': 'update', 'before': {'line_id': None}})
            written['veredictos'] += rows.update(line_id=line)
    for row in planned['runs']:
        saved = SkuReviewRun.objects.select_for_update().get(pk=row['run'].pk)
        before = {'proposal_ids': list(saved.proposal_ids), 'finished_at': saved.finished_at, 'metrics': dict(saved.metrics)}
        if row['close']:
            reviewed = row['mapped'][:saved.completed_batches * saved.batch_size]
            saved.proposal_ids = [pk for pk in reviewed if pk]
            saved.finished_at = now
            saved.metrics = {**saved.metrics, 'nota': (f'Cerrada en S6: {row["missing"]} de {len(row["mapped"])} productos ya no esperan '
                                                       'revisión; se conservan los lotes revisados.')}
        else:
            saved.proposal_ids = [pk for pk in row['mapped'] if pk]
        if saved.proposal_ids == before['proposal_ids'] and saved.finished_at == before['finished_at']:
            continue
        saved.save(update_fields=['proposal_ids', 'finished_at', 'metrics'])
        log.append({**run, 'model': 'mall.SkuReviewRun', 'id': str(saved.pk), 'action': 'update', 'before': recorded(before)})
        written['corridas'] += 1

    # 7. The resolved proposals, archived (the rows stay).
    if planned['archive']:
        path = stage_dir() / ARCHIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{'id': str(p.pk), 'key': p.key, 'sku': p.sku, 'description': p.description, 'brand': p.brand, 'oem_code': p.oem_code,
                 'oem_brand': p.oem_brand, 'references': p.references, 'field_sources': p.field_sources, 'version': p.version,
                 'created_at': p.created_at, 'updated_at': p.updated_at, 'items': [str(row.item_id) for row in p.items.all()]}
                for p in planned['archive']]
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'database': current_database(), 'run': run['run'], 'proposals': rows}, handle, ensure_ascii=False, indent=1, default=str)
        written['archivadas'] = len(rows)
    return dict(written)


# --- rollback -------------------------------------------------------------------------------------------------------------------------

def kept_by(line):
    """What else points at a line its rollback would delete (rows that a delete would cascade away or that protect it), or ''."""
    from ..models import Part
    names = []
    for relation in Part._meta.related_objects:
        model = relation.related_model
        if model._meta.label in ('mall.Reference', 'mall.SupplierItem', 'mall.SkuReviewVerdict') or relation.many_to_many:
            continue
        if relation.one_to_many or relation.one_to_one:
            manager = model._base_manager if hasattr(model, '_base_manager') else model.objects
            if manager.filter(**{relation.field.name: line.pk}).exists():
                names.append(model._meta.verbose_name_plural)
    return ', '.join(names)


S6_KEYS = ('reference:', 'code_change:', 'variant:', 'line:', 'merge:')  # the keys of the cases the S6 code opens (plan §2)


def model_values(row):
    """A row's columns as the manifest records them (its values before a rollback deletes it)."""
    return recorded({field.attname: getattr(row, field.attname) for field in row._meta.concrete_fields})


def applied_at(entries):
    """When the first --apply of this manifest ran (the S6 data step runs under the freeze right before S6 lands): rows created on a line
    since then are the S6 code's. None without one."""
    stamps = sorted(line['at'] for line in manifest_lines() if line.get('action') == 'run' and line.get('mode') == 'apply' and line.get('at'))
    if not stamps:
        return None
    from django.utils.dateparse import parse_datetime
    return parse_datetime(stamps[0])


def write_back(proposal, line, log, run, result):
    """What a person (or Revisión IA) did since S6 landed to the line a pending proposal became, written back to the proposal for the
    code before S6, which reads proposals: published → approved (part: the line), merged → linked (part: the line it went into),
    rejected → rejected with its reason and note; still in preparación → its codigo, description, subgrupo, OEM number, «código
    interno», administrator's alternos, field_sources and version. The values before go to the manifest. Returns what it wrote."""
    from django.utils import timezone
    from .. import lines, references
    from ..lines import PREPARED
    from ..models import Reference, SupplierItem
    from ..new_products import oem_of
    from ..sku_proposal_models import SkuProposal
    names = ('status', 'part_id', 'decided_by_id', 'decided_at', 'reject_reason', 'reject_note', 'sku', 'description', 'part_type_id',
             'field_sources', 'version', 'internal_code', 'oem_code', 'oem_brand', 'references', 'updated_at')
    before = {name: getattr(proposal, name) for name in names}
    decided_at = line.decided_at or line.updated_at or timezone.now()
    if line.merged_into_id or line.estado == 'unificada':
        target = lines.canonical(line)
        fields = {'status': 'linked', 'part_id': target.pk if target is not None else line.merged_into_id, 'decided_at': decided_at,
                  'decided_by_id': line.decided_by_id}
    elif line.estado in ('publicada', 'retirada'):
        fields = {'status': 'approved', 'part_id': line.pk, 'decided_at': decided_at, 'decided_by_id': line.decided_by_id}
    elif line.estado == 'rechazada':
        fields = {'status': 'rejected', 'reject_reason': line.decision_reason or 'other', 'reject_note': line.decision_note or '',
                  'decided_at': decided_at, 'decided_by_id': line.decided_by_id}
    elif line.estado == 'preparacion':
        codes = list(Reference.objects.filter(part_id=line.pk).order_by('pk'))
        main, oem = references.main_in(codes), oem_of(line, codes)
        sources = {key: value for key, value in (line.field_sources or {}).items() if key != PREPARED}
        fields = {'sku': line.sku, 'description': line.description, 'part_type_id': line.part_type_id, 'field_sources': sources,
                  'version': line.version, 'internal_code': main is not None and main.tipo == 'interno',
                  'oem_code': oem.code if oem is not None and oem.brand else '', 'oem_brand': oem.brand if oem is not None and oem.code else ''}
        if (sources.get('references') or {}).get('source') == 'admin':
            hung = set(SupplierItem.objects.filter(part_id=line.pk).values_list('reference_id', flat=True))
            fields['references'] = [{'code': ref.code, 'brand': ref.brand, 'ref_type': ref.ref_type} for ref in codes
                                    if not ref.is_main and ref.tipo != 'interno' and (oem is None or ref.pk != oem.pk) and ref.pk not in hung]
        if line.changed_at:
            fields['updated_at'] = line.changed_at
    else:
        return {}
    changed = {name: value for name, value in fields.items() if before.get(name) != value}
    if set(changed) - {'updated_at'}:
        SkuProposal.objects.filter(pk=proposal.pk).update(**changed)
        log.append({**run, 'model': 'mall.SkuProposal', 'id': str(proposal.pk), 'action': 'write_back',
                    'before': recorded({name: before[name] for name in changed}), 'after': recorded(changed)})
        result['propuestas_actualizadas'] += 1
        return changed
    return {}


def undo(log, run):
    """--rollback (the module's docstring). Returns the counts and what it left."""
    from django.db import IntegrityError, transaction
    from django.db.models import ProtectedError, Q, RestrictedError
    from .. import lines, references, variants
    from ..lines import PREPARED, prepared_q
    from ..matching_models import MatchingCase
    from ..models import Part, Reference, SupplierItem
    from ..sku_proposal_models import SkuProposal
    from ..sku_review_models import SkuReviewRun, SkuReviewVerdict
    entries = [line for line in manifest_lines() if line.get('action') not in (None, 'run', 'rolled_back')]
    since = applied_at(entries)
    result, left = Counter(), []

    # 1. Verdicts Revisión IA wrote after landing hang only from their line (proposal NULL), and reversing 0084 makes proposal NOT NULL
    #    again: a line that was a proposal gives it back its id; any other line's verdicts are deleted, their values in the manifest.
    proposals = set(str(pk) for pk in SkuProposal.objects.values_list('pk', flat=True))
    for verdict in SkuReviewVerdict.objects.filter(proposal__isnull=True).order_by('pk'):
        if verdict.line_id and str(verdict.line_id) in proposals:
            SkuReviewVerdict.objects.filter(pk=verdict.pk).update(proposal_id=verdict.line_id)
            log.append({**run, 'model': 'mall.SkuReviewVerdict', 'id': str(verdict.pk), 'action': 'set_proposal', 'before': {'proposal_id': None}})
            result['veredictos_con_propuesta'] += 1
        else:
            log.append({**run, 'model': 'mall.SkuReviewVerdict', 'id': str(verdict.pk), 'action': 'delete', 'before': model_values(verdict)})
            verdict.delete()
            result['veredictos_borrados'] += 1

    # 2. The lines S6 made: the manifest's, those whose References carry the tag of mall.new_products.place or of a proposal (a main an
    #    edit or «código interno» replaced keeps it), and any line from a supplier in preparación or rechazada (none existed before S6).
    created_lines = {line['id'] for line in entries if line['model'] == 'mall.Part' and line['action'] == 'create'}
    created_lines |= {str(part) for part, detail in Reference.all_objects.filter(detail__has_key=TAG).values_list('part_id', 'detail')
                      if (detail.get(TAG) or {}).get('como') in ('propuesta', 'place')}
    created_lines |= {str(pk) for pk in Part._base_manager.filter(origen='proveedor', estado__in=('preparacion', 'rechazada'))
                      .values_list('pk', flat=True)}
    joined = {}
    for line in entries:
        if line['model'] == 'mall.Part' and line['action'] == 'join':
            joined.setdefault(line['id'], line['before'])  # the first join holds the values before S6
    created_refs = {int(line['id']) for line in entries if line['model'] == 'mall.Reference' and line['action'] == 'create'}
    hung = defaultdict(set)  # the items each apply hung, by the line they went to
    for line in entries:
        if line['model'] == 'mall.SupplierItem' and line['action'] == 'hang':
            hung[str(line['after']['part_id'])].add(line['id'])

    def detach_all(items):
        for item in items:
            log.append({**run, 'model': 'mall.SupplierItem', 'id': str(item.pk), 'action': 'rollback',
                        'before': recorded({'reference_id': item.reference_id, 'part_id': item.part_id, 'matching_status': item.matching_status,
                                            'codigo': item.codigo, 'codigo_pendiente': item.codigo_pendiente})})
            variants.detach(item)
            result['articulos'] += 1

    rule_7 = {str(pk) for pk in Part._base_manager.filter(prepared_q()).values_list('pk', flat=True)}
    for pk in sorted(created_lines - set(joined) - rule_7):
        line = Part._base_manager.filter(pk=pk).first()
        if line is None:
            continue
        proposal = SkuProposal.objects.filter(pk=pk, status='pending').first()  # a line the data step made keeps its proposal's id
        if proposal is not None:
            write_back(proposal, line, log, run, result)
        elif line.estado == 'rechazada':
            left.append(f'línea {line.sku} ({pk}): se rechazó después de S6 y no era una propuesta; el rechazo se pierde')
        if line.estado not in ('preparacion', 'rechazada') or line.merged_into_id:
            left.append(f'línea {line.sku} ({pk}): está {lines.phrase(line.estado)}, se deja')
            continue
        detach_all(SupplierItem.objects.filter(part_id=line.pk).order_by('pk'))
        why = kept_by(line)
        if why:
            left.append(f'línea {line.sku} ({pk}): tiene {why}, se deja sin artículos')
            continue
        refs = list(Reference._base_manager.filter(part_id=line.pk).values_list('pk', flat=True))
        try:
            with transaction.atomic():
                Reference._base_manager.filter(pk__in=refs).update(reemplazado_por=None)
                purged = references.purge(refs)
                Part._base_manager.filter(pk=line.pk).delete()
        except (IntegrityError, ProtectedError, RestrictedError) as error:
            left.append(f'línea {line.sku} ({pk}): algo más la nombra ({error.__class__.__name__}), se deja sin artículos')
            continue
        result['referencias'] += purged
        log.append({**run, 'model': 'mall.Part', 'id': str(pk), 'action': 'delete', 'before': recorded({'sku': line.sku, 'estado': line.estado})})
        result['lineas'] += 1

    # 3. The lines rule 7 brought from referencia: the manifest's joins and every line with the rule-7 mark (mall.lines.prepared_q: the
    #    engine's moves after landing). Still in preparación or rechazada, each loses its items (all came with S6: a line in referencia
    #    holds none) and the codes S6 added to it, and goes back to referencia with its version, its last change and no decision.
    marked = {str(pk): None for pk in Part._base_manager.filter(prepared_q(), estado__in=('preparacion', 'rechazada')).values_list('pk', flat=True)}
    for pk, before in {**marked, **joined}.items():
        line = Part._base_manager.filter(pk=pk).first()
        if line is None:
            continue
        mark = (line.field_sources or {}).get(PREPARED) or {}
        before = before or {'estado': mark.get('desde', 'referencia'), 'version': mark.get('version') or line.version,
                            'changed_at': mark.get('changed_at'), 'decided_by_id': None, 'decided_at': None}
        if line.estado not in ('preparacion', 'rechazada') or line.merged_into_id:
            left.append(f'línea {line.sku} ({pk}): estaba {lines.phrase(before.get("estado"))} y está {lines.phrase(line.estado)}, se deja')
            continue
        if line.estado == 'rechazada':
            left.append(f'línea {line.sku} ({pk}): se rechazó después de S6; vuelve a referencia y el rechazo se pierde')
        added = Q(pk__in=created_refs) | Q(detail__has_key=TAG, is_main=False)
        if since is not None:
            added |= Q(created_at__gte=since, is_main=False)
        refs = list(Reference._base_manager.filter(Q(part_id=pk) & added).values_list('pk', flat=True))
        detach_all(SupplierItem.objects.filter(Q(part_id=pk) | Q(reference_id__in=refs)).order_by('pk'))
        result['referencias'] += references.purge(refs)
        for ref_pk, detail in Reference._base_manager.filter(part_id=pk, detail__has_key=TAG).values_list('pk', 'detail'):
            Reference._base_manager.filter(pk=ref_pk).update(detail={key: value for key, value in detail.items() if key != TAG})
        if SupplierItem.objects.filter(part_id=pk).exists():
            left.append(f'línea {line.sku} ({pk}): aún tiene artículos, se deja en {lines.phrase(line.estado)}')
            continue
        sources = {key: value for key, value in (line.field_sources or {}).items() if key != PREPARED}
        Part._base_manager.filter(pk=pk).update(estado=before.get('estado') or 'referencia', active=False, version=before.get('version') or line.version,
                                               changed_at=before.get('changed_at'), decided_by_id=before.get('decided_by_id'),
                                               decided_at=before.get('decided_at'), decision_reason='', decision_note='', field_sources=sources)
        log.append({**run, 'model': 'mall.Part', 'id': str(pk), 'action': 'return_referencia',
                    'before': recorded({'estado': line.estado, 'version': line.version, 'field_sources': line.field_sources})})
        result['lineas_devueltas'] += 1

    # 4. Cases: those the manifest created go, the legacy ones it dismissed come back; then every case the S6 code opened since (its
    #    keys: reference:, code_change:, variant:, line:, merge:), logged with its values. Converted legacy cases keep their legacy keys:
    #    python -m mall.refactor.s6_cases --rollback gives them back their kind first.
    for line in entries:
        if line['model'] == 'mall.MatchingCase' and line['action'] == 'create':
            result['casos'] += MatchingCase.objects.filter(pk=line['id']).delete()[1].get('mall.MatchingCase', 0)
        elif line['model'] == 'mall.MatchingCase' and line['action'] == 'update':
            result['casos_legados'] += MatchingCase.objects.filter(pk=line['id'], status='dismissed').update(**line['before'])
        elif line['model'] == 'mall.SkuReviewVerdict' and line['action'] == 'update':
            result['veredictos'] += SkuReviewVerdict.objects.filter(pk__in=line['ids']).update(line_id=None)
        elif line['model'] == 'mall.SkuReviewRun':
            before = line['before']
            result['corridas'] += SkuReviewRun.objects.filter(pk=line['id']).update(proposal_ids=before['proposal_ids'],
                                                                                    finished_at=before['finished_at'], metrics=before['metrics'])
    s6_cases = Q()
    for prefix in S6_KEYS:
        s6_cases |= Q(key__startswith=prefix)
    for case in MatchingCase.objects.filter(s6_cases).order_by('pk'):
        log.append({**run, 'model': 'mall.MatchingCase', 'id': str(case.pk), 'action': 'delete',
                    'before': {**model_values(case), 'lines': [str(pk) for pk in case.lines.values_list('pk', flat=True)]}})
        case.delete()
        result['casos_s6'] += 1
    later = SkuReviewRun.objects.filter(created_at__gte=since) if since is not None else SkuReviewRun.objects.none()
    for review in later.order_by('created_at'):
        left.append(f'revisión IA {review.pk}: se creó después de S6; sus ids son líneas, y las que ya no existen se ignoran')
    log.append({**run, 'model': 'manifest', 'action': 'rolled_back', 'rows': len(entries)})
    return {**dict(result), 'left': left}


# --- run ------------------------------------------------------------------------------------------------------------------------------

ROWS = (('propuestas', 'propuestas pendientes'), ('lineas_nuevas', 'líneas nuevas en preparación'), ('unidas', 'unidas a una línea'),
        ('en_catalogo', 'su código es de una línea del catálogo'), ('conflictos', 'su código está en dos líneas'),
        ('sin_articulos', 'sin artículos por colocar'), ('articulos', 'artículos colocados'), ('oem', 'principal OEM'),
        ('interno', 'principal interna'), ('aftermarket', 'principal posventa'), ('otro', 'principal otra'),
        ('casos_declarados', 'casos de códigos declarados'), ('casos_legados', 'casos antiguos descartados'),
        ('veredictos', 'veredictos con su línea'), ('corridas', 'revisiones IA'))


def print_summary(stdout, mode, planned, state, writes=None):
    title = {'apply': 'Aplicado', 'dry_run': 'Simulación'}[mode]
    counts = planned['counts']
    stdout.write(f'{title} · S6 Productos nuevos como líneas en preparación\n')
    if not (state['verdict_line'] and state['cases'] and state['referencia']):
        missing = [name for name, ok in (('las líneas en referencia de S4', state['referencia']), ('0083', state['cases']),
                                         ('0084', state['verdict_line'])) if not ok]
        stdout.write(f'  (simulación: a esta base le faltan {", ".join(missing)}; sin las líneas de S4 ninguna propuesta se une a un '
                     'número de la Biblioteca, y sin 0083/0084 los casos y veredictos solo se cuentan)\n')
    stdout.write(f"  {'':42} {'esperado':>9} {'resultado':>10}\n")
    for key, label in ROWS:
        expected = EXPECTED.get(key)
        stdout.write(f"  {label:42} {'' if expected is None else f'{expected:,}':>9} {counts.get(key, 0):>10,}\n")
    for key in ('ya_convertidas', 'veredictos_sin_linea', 'corridas_cerradas', 'archivadas'):
        if counts.get(key):
            stdout.write(f'  {key.replace("_", " ")}: {counts[key]:,}\n')
    if planned['history']:
        stdout.write('  Historial (sin cambios): ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(planned['history'].items())) + '\n')
    for key, rows in sorted(planned['listed'].items()):
        stdout.write(f'  {key.replace("_", " ").capitalize()}: {len(rows)}\n')
        for row in rows[:15]:
            stdout.write(f'    {row}\n')
    if writes:
        stdout.write('  Escrito: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(writes.items())) + '\n')


def report(planned):
    return {'expected': EXPECTED, 'result': dict(planned['counts']), 'listed': {key: rows for key, rows in planned['listed'].items()},
            'history': planned['history'],
            'joins': [{'proposal': str(entry['proposal'].pk), 'sku': entry['code'], 'line': str(entry['line']), 'how': entry['action']}
                      for entry in planned['entries'] if entry['action'] != 'create'],
            'runs': [{'run': str(row['run'].pk), 'ids': len(row['mapped']), 'missing': row['missing'], 'close': row['close']}
                     for row in planned['runs']]}


def run(*, apply=False, rollback=False, username=None, report_path=None, stdout=None):
    """Plan, and with apply write it, or with rollback undo the applies on this database. None when a write is refused."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone
    from ..matching_queue import matching_lock, matching_work
    from ..models import Account, SupplierItem
    stdout = stdout or sys.stdout
    state = schema()
    user = None
    if apply or rollback:
        from .guard import refusal
        reason = refusal(True) or manifest_problem()
        if reason:
            stdout.write(reason + '\n')
            return None
        user = get_user_model().objects.filter(username=username or '').first()
        if user is None and (apply or username):
            stdout.write('Indica con --user el usuario que figura como autor del cambio.\n' if not username else f'No existe el usuario {username}.\n')
            return None
        if apply and not state['verdict_line']:
            stdout.write('Falta la migración 0084_proposal_lines (y con ella 0083_matching_cases) en esta base: aplícalas primero desde '
                         'el worktree (la simulación funciona sin ellas).\n')
            return None
    run_line = {'stage': 's6_new_products', 'database': current_database(), 'run': str(uuid.uuid4()), 'actor': user.username if user else '',
                'at': timezone.now().isoformat()}

    def go():
        log = []
        with transaction.atomic():
            if rollback:
                result = {'mode': 'rollback', **undo(log, run_line)}
            else:
                if apply:  # the lock order of every writer of items: supplier Accounts, then the items
                    suppliers = sorted(set(SupplierItem.objects.filter(part__isnull=True).values_list('supplier_id', flat=True)))
                    list(Account.objects.select_for_update().filter(pk__in=suppliers).order_by('pk').values_list('pk', flat=True))
                    list(SupplierItem.objects.select_for_update().filter(part__isnull=True).order_by('pk').values_list('pk', flat=True))
                planned = plan()
                result = {'mode': 'apply' if apply else 'dry_run', **report(planned)}
                writes = None
                if apply:
                    writes = result['written'] = write(planned, log, run_line, user)
                    result['listed'] = {key: rows for key, rows in planned['listed'].items()}
                print_summary(stdout, result['mode'], planned, state, writes)
                if report_path:
                    with open(report_path, 'w', encoding='utf-8') as handle:
                        json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
                    stdout.write(f'Informe: {report_path}\n')
            if log:
                result['manifest'] = str(write_manifest([{**run_line, 'action': 'run', 'mode': result['mode'], 'rows': len(log)}, *log]))
        result['rows_logged'] = len(log)
        if rollback:
            stdout.write('Rollback · S6 Productos nuevos: ' + ' · '.join(f'{key} {value:,}' for key, value in sorted(result.items())
                                                                          if isinstance(value, int) and key != 'rows_logged') + '\n')
            for line in result['left'][:40]:
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
    parser = argparse.ArgumentParser(description='S6: los productos nuevos pendientes pasan a ser líneas en preparación.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los cambios sin escribir nada (también antes de 0083/0084: simulación).')
    mode.add_argument('--apply', action='store_true', help='Escribe todo en una transacción (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Deshace con el manifiesto lo aplicado en esta base (requiere el congelamiento en vivo).')
    parser.add_argument('--user', help='Usuario que figura como autor (obligatorio con --apply; opcional con --rollback).')
    parser.add_argument('--report', help='Escribe en esta ruta (JSON) las cuentas, las uniones y las listas.')
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
