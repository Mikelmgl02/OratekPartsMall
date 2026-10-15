"""Merging OEM lines and taking them apart again (inventory refactor S2b, plan §3 S2b): the only writer of a line's merged_into, and of
what a merge moves between lines. Agrupar SKU (mall.catalog_grouping.merge_catalog_parts), the matching worker's families
(mall.matching_engine.apply_family) and Separar SKU (mall.catalog_split.split_catalog_part) are thin wrappers over it, and so is the
S2b backfill's compact-duplicate pass (OD4) until S10 took the stage CLIs away.

    merge_lines(target, sources, *, actor, reason, automatic)   the sources become unificada in target with everything they hold
    split_line(target, restore, *, actor, reason)               Separar: lines come back out of target with what each brought
    absorb(ref, line, *, actor, reason)                         a Reference whose key the line already holds: retired onto that one
    keep_apart(lines, *, actor, case, split)                    CatalogSeparation pairs: never merged on their own again
    separated_pairs(ids), apart(a, b)                           the pairs kept apart, checked on canonical lines
    lock_lines(ids)                                             the lock order: supplier Accounts, then lines (plan §1.6.5)

A merge (merge_lines), in one transaction:
- Locks the supplier Accounts of every variant involved by id, then the lines by id, then their References, then the SupplierItems;
  it refuses while one of those suppliers is importing (the lock gap the UI merge had).
- References: every live Reference of a source (and of the lines merged into it before: depth 1) moves to the target as non-main; its
  former main among them. One whose key (clave, brand, supplier) the target already holds is absorbed instead (absorb): it stays on
  its source as retirada, replaced by the target's (reemplazado_por, its spelling joining the target's printed_forms, its sources
  copied), and Separar brings it back. A code waiting in disputa for this merge ("... caso de unificación") is declarado again once no
  other line holds its key. A key another line holds (the brandless reservation, I5) or a source code that is another line's codigo
  refuses the merge.
- Variants follow their References (S3: mall.variants; from 0078 the composite foreign key moves them with each Reference), and an
  absorbed Reference's variants re-hang from the Reference that replaces it; images and favourites move without duplicates (the SKU
  links and historical Alternates that merges re-pointed until S10 are archived with their tables).
  Specs and applications (S6) move only when the target's subgrupo is empty or the same as each source's that carries them: a spec of
  a field the target already has, or an application it already lists, stays on its source; a target without a subgrupo takes the one
  their sources agree on. Any other merge waits (MergeGuard, guard subgrupo: a merge case keeps it).
- Dependents: open OEMReviewCase rows of the sources are resolved ("línea unificada"), pending OEMAutoCandidate rows excluded, pending
  CatalogAssistantSuggestion rows dismissed; order lines, cesta lines, identity changes, verdicts, usage and import issues stay as
  history. Scalars: the target wins and its blanks are filled when the sources agree on a value (field_sources says where from).
- Estado of the target: publicada if any line is publicada; otherwise preparación if a line outside the catalog (not retirada) holds
  variants; otherwise retirada, then rechazada, then referencia (a retired line's variants are its catalog stock, S3: two retired lines
  stay retired). The sources become unificada; lines merged into them are flattened onto the target.
- automatic=True (the worker, the S2b pass) refuses lines kept apart (CatalogSeparation, on canonical lines) and records the merge
  under the matching service's account, as the worker's merges are (Unificados, origen automático), with who asked for it.
- CatalogMerge records, per source, the References it moved or absorbed and their ids, its former main, its codes, variants, images,
  favourites, links, specs, applications and dependents, so Separar gives back exactly that.
- After it (S6): the open matching cases that list a source list the target too (the worker checks them again), and the variants on
  the target take its status (mall.variants.recompute_status: matched on a catalog line when no case holds them). Codigos never change here (UNIQUE(sku) until S4): the wrappers
  run prefer_oem afterwards when they want to.

A split (split_line) brings back the lines a superuser picks, each with the References, variants and images assigned to it. A
variant goes where its Reference goes (S3): one assigned elsewhere re-hangs from that line's Reference of its key, and is refused when
that line has none. A line's
former main always comes back as its main: the row the merge recorded, or for a merge made before S2b the brandless alias of its
codigo, which was its main then; one absorbed comes back from retirada; either waits in disputa when another line keeps its key. A
code the merge put back to declarado returns to disputa while another line still holds its key. Favourites and dependents come back, the absorbed spellings and copied
sources leave the target, and a collapsed spelling (two codigos with one compact code) splits by the variant's own codigo (B16). The
lines brought back are kept apart from the target and from each other (CatalogSeparation).
"""
import json

from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import references

SERVICE_ACCOUNT = 'motionpartes-matching-service'  # the matching worker's actor (mall.matching_worker)
COMPACT_DUPLICATE = 'compact_duplicate'
SHARED_OEM_NUMBER = 'shared_oem_number'
MAIN_CODIGO = 'main_codigo'  # mall.references.open_main_case: a main whose key another line holds
KEEP_APART_METHODS = (COMPACT_DUPLICATE, SHARED_OEM_NUMBER, MAIN_CODIGO)  # catalog cases whose dismissal keeps their lines apart
MERGE_DISPUTE = 'caso de unificación'  # how the note of a code two lines hold ends until a merge decides (S2b)
METHODS = {'base_and_spec': 'Mismo código base y la misma descripción', 'catalog_reference': 'Su código era alterno de este SKU',
           'family_review': 'Familia revisada en Agrupación inteligente', COMPACT_DUPLICATE: 'El mismo código compacto (S2b)',
           'agrupar_sku': 'Agrupar SKU', 'family': 'Familia del servicio de coincidencias'}
SCALARS = ('description', 'aplicaciones_texto')  # filled on the target when its own is blank (the subgrupo: sheet_subgrupo)
CATALOG = ('publicada', 'retirada')
ESTADO_AFTER = ('retirada', 'rechazada', 'referencia')


def refuse(message):
    return ValidationError({'detail': message})


class SupplierImporting(ValidationError):
    """A merge or a split refused because a supplier of those lines is importing its inventory: temporary (try again when the import
    ends), so a batch caller (mall.merge_pass) stops instead of turning it into a case. A ValidationError like refuse()."""

    def __init__(self, message):
        super().__init__({'detail': message})


class MergeGuard(ValidationError):
    """A merge that waits for a person (S6): `guard` says why (subgrupo: the technical sheets of two subgrupos). A ValidationError with
    the message, so every caller that refuses a merge keeps working; a merge case records the guard."""

    def __init__(self, message, guard):
        super().__init__({'detail': message})
        self.guard = guard


def jsonable(value):
    """A record as the JSON columns store it (ids as text where they are UUIDs)."""
    return json.loads(json.dumps(value, default=str))


def _models():
    from . import models
    return models


# --- lines kept apart ----------------------------------------------------------------------------------------------------------------

def pair(a, b):
    """Two line ids in CatalogSeparation order."""
    return tuple(sorted((str(a), str(b))))


def separated_pairs(part_ids=None):
    """{(id, id): when} for the lines kept apart (CatalogSeparation: Separar SKU, or a compact_duplicate or shared_oem_number case
    dismissed), each pair also under its lines' canonical lines, so a pair kept apart stays apart after either joins another line (plan
    §2). With part_ids, only the pairs touching them. Reads only the pair columns, so it works on a database before 0075."""
    from .catalog_split_models import CatalogSeparation
    from .lines import canonical_ids
    rows = list(CatalogSeparation.objects.values_list('first_id', 'second_id', 'created_at'))
    canonical = canonical_ids({pk for first, second, _ in rows for pk in (first, second)}) if rows else {}
    found = {}
    for first, second, when in rows:
        for a, b in ((first, second), (canonical.get(str(first), first), canonical.get(str(second), second))):
            if str(a) != str(b):
                key = pair(a, b)
                found[key] = min(found[key], when) if key in found else when
    if part_ids is not None:
        ids = {str(pk) for pk in part_ids}
        found = {key: when for key, when in found.items() if ids & set(key)}
    return found


def apart(a, b, pairs=None):
    """Whether two lines are kept apart, on their canonical lines."""
    from .lines import canonical_id
    pairs = separated_pairs() if pairs is None else pairs
    return pair(a, b) in pairs or pair(canonical_id(a) or a, canonical_id(b) or b) in pairs


def kept_apart_among(ids, pairs=None):
    """The pairs of these lines that are kept apart (sorted), on their canonical lines."""
    from .lines import canonical_ids
    ids = sorted({str(pk) for pk in ids})
    pairs = separated_pairs() if pairs is None else pairs
    canonical = canonical_ids(ids)
    found = []
    for index, a in enumerate(ids):
        for b in ids[index + 1:]:
            if pair(a, b) in pairs or pair(canonical.get(a, a), canonical.get(b, b)) in pairs:
                found.append(pair(a, b))
    return found


def keep_apart(line_ids, *, actor=None, split=None, case=None, reason=''):
    """Write the CatalogSeparation pairs of these lines (each pair once; lines that are one line already are skipped): a Separar SKU
    split, or a case a person decided to keep apart. Returns how many pairs were written."""
    from .catalog_split_models import CatalogSeparation
    from .lines import canonical_ids
    ids = sorted({str(pk) for pk in line_ids if pk})
    canonical = canonical_ids(ids)
    known = set(CatalogSeparation.objects.filter(Q(first_id__in=ids) | Q(second_id__in=ids)).values_list('first_id', 'second_id'))
    known = {pair(a, b) for a, b in known}
    wanted = sorted({pair(a, b) for index, a in enumerate(ids) for b in ids[index + 1:]
                     if canonical.get(a, a) != canonical.get(b, b)} - known)
    rows = [CatalogSeparation(first_id=a, second_id=b, split=split, case=case, reason=(reason or '')[:40],
                              actor=actor if getattr(actor, 'pk', None) else None) for a, b in wanted]
    CatalogSeparation.objects.bulk_create(rows)
    return len(rows)


# --- locks ---------------------------------------------------------------------------------------------------------------------------

def importing(supplier_ids):
    from .supplier_import_models import SupplierInventoryImportJob
    return SupplierInventoryImportJob.objects.filter(supplier_id__in=supplier_ids, status='importing', expires_at__gt=timezone.now()).exists()


def lock_lines(ids, *, suppliers=True):
    """Take the locks of a merge or a split in the plan's order (§1.6.5): the supplier Accounts of every variant on these lines and on
    the lines merged into them, by id, then the lines by id. Refuses while one of those suppliers is importing. suppliers=False (a
    library merge: lines in referencia hold no variants, and the target's do not move) takes the lines only. Returns {id: Part}."""
    m = _models()
    ids = {getattr(pk, 'pk', pk) for pk in ids if pk}
    family = ids | set(m.Part.objects.filter(merged_into_id__in=ids).values_list('pk', flat=True))
    if suppliers:
        accounts = sorted(set(m.SupplierItem.objects.filter(part_id__in=family).values_list('supplier_id', flat=True)))
        list(m.Account.objects.select_for_update().filter(pk__in=accounts).order_by('pk').values_list('pk', flat=True))
        if importing(accounts):
            raise SupplierImporting('Un proveedor de estos SKU está importando su inventario. Espera a que termine y vuelve a intentarlo.')
    return {part.pk: part for part in m.Part.objects.select_for_update().filter(pk__in=family).order_by('pk')}


def service_actor():
    """The matching service's account (mall.matching_worker), created as the worker does when an automatic merge runs first: inactive
    and with no usable password, or the worker refuses to run ('Matching service identity must be disabled')."""
    from django.contrib.auth import get_user_model
    actor, created = get_user_model().objects.get_or_create(username=SERVICE_ACCOUNT, defaults={
        'email': 'matching-service@motionpartes.invalid', 'is_active': False})
    if created:
        actor.set_unusable_password()
        actor.save(update_fields=['password'])
    return actor


# --- absorb --------------------------------------------------------------------------------------------------------------------------

def same_key(ref, line_id):
    """The live Reference of the line with ref's key (clave, brand, supplier), or None."""
    m = _models()
    return (m.Reference.all_objects.select_for_update().filter(part_id=line_id, clave=references.clave(ref.code), brand=ref.brand,
                                                                supplier_id=ref.supplier_id, estado__in=references.LIVE)
            .exclude(pk=ref.pk).order_by('pk').first())


def absorb(ref, line, *, actor=None, reason=''):
    """A Reference (a source line's main, say) whose key the line already holds: it stays on its line as retirada, replaced by the
    line's Reference of that key (reemplazado_por), whose printed_forms gain its spelling and which gets copies of its sources; Separar
    brings it back (split_line). Its variants re-hang from that Reference (S3: they keep their status). Used by merge_lines, and from
    S4 by references.add for a key a referencia line holds. Returns {'ref', 'into', 'estado', 'dispute_note', 'spelling', 'sources',
    'variants'}."""
    m = _models()
    from .variants import rehang
    with transaction.atomic():
        ref = m.Reference.all_objects.select_for_update().get(pk=getattr(ref, 'pk', ref))
        kept = same_key(ref, getattr(line, 'pk', line))
        if kept is None:
            raise references.ReferenceConflict(f'{ref.code} no tiene en el SKU de destino una referencia con su clave.')
        record = {'ref': ref.pk, 'into': kept.pk, 'estado': ref.estado, 'dispute_note': ref.dispute_note, 'spelling': None, 'sources': []}
        record['variants'] = rehang([ref.pk], kept)
        library = references.is_library_number(ref)
        references.retire(ref, actor=actor, replaced_by=kept, reason=reason or 'Absorbida por otra línea', allow_main=True,
                          keep_library=False)
        if ref.code != kept.code and ref.code not in kept.printed_forms and len(kept.printed_forms) < references.MAX_PRINTED:
            references.add_spelling(kept, ref.code)
            record['spelling'] = ref.code
        for source in m.ReferenceSource.objects.filter(reference_id=ref.pk).order_by('pk'):
            copy, created = references.cite(kept, source.citation, actor, kind=source.kind, detail={**source.detail, 'absorbed_from': ref.pk})
            if created:
                record['sources'].append(copy.pk)
        # S4: the Reference that absorbs a library number becomes an OEM number too (from S10 nothing else hangs from it but its
        # sources: the aftermarket codes, tombstones and SKU links are archived).
        if library and kept.tipo != 'oem':
            record['into_tipo'] = kept.tipo
            references.update(kept, tipo='oem', actor=actor)
    return record










def return_library_rows(record, actor=None):
    """Separar: the kept Reference takes back the tipo it had. (The aftermarket codes, tombstones and links an absorb moved before S10,
    record['library_rows'], are archived with their tables: they stay where they were archived.)"""
    if record.get('into_tipo'):
        kept = _models().Reference.objects.filter(pk=record['into'], tipo='oem').first()
        if kept is not None:
            references.update(kept, tipo=record['into_tipo'], actor=actor)


# --- merge ---------------------------------------------------------------------------------------------------------------------------

def merged_estado(lines, with_variants):
    """The estado of a merge's target (the plan's matrix): publicada if any line is; else preparación if a line outside the catalog
    holds variants; else retirada, rechazada, referencia, in that order; else (lines in preparación without variants) preparación."""
    estados = {line.estado for line in lines}
    if 'publicada' in estados:
        return 'publicada'
    if any(line.estado not in CATALOG for line in with_variants):
        return 'preparacion'
    return next((estado for estado in ESTADO_AFTER if estado in estados), 'preparacion')


def merge_dispute(ref):
    """Whether a Reference waits in disputa for a merge (the note S2b's backfill and references.dispute_note_for write)."""
    return ref.estado == 'disputa' and (ref.dispute_note or '').rstrip(' .').endswith(MERGE_DISPUTE)


def _codigo_holder(code, participants, target):
    """Another catalog line (publicada or retirada), outside the merge, whose codigo is this code: from S4 the codigo is unique among
    those only, and lines in referencia may share one across makes."""
    from .lines import catalog_lines
    return (catalog_lines().filter(sku__iexact=references.code_key(code)).exclude(pk__in=participants).exclude(merged_into=target)
            .order_by('pk').first())


def merge_lines(target, sources, *, actor, reason='', automatic=False, assistant_job=None, target_created=False, library=False):
    """Merge `sources` (Parts, live, not merged) into `target` (a live Part): see the module's docstring. The caller has validated what
    it asked (Agrupar SKU's rules); this takes the locks again, refuses what would break the catalog (ValidationError with a detail)
    and returns the CatalogMerge, or None when every source was already merged into target. library=True is S4's rule 3f (a code
    absorbing library numbers' lines in referencia, mall.references.absorb_library_lines): every source must be in referencia without
    variants, no supplier is locked (the caller may already hold lines or References) and the target keeps its estado."""
    m = _models()
    from .lines import refuse_unificada
    source_ids = [getattr(source, 'pk', source) for source in sources]
    target_id = getattr(target, 'pk', target)
    if target_id in source_ids:
        raise refuse('El SKU principal no puede incluirse entre los SKU por unificar.')
    if len(set(source_ids)) != len(source_ids):
        raise refuse('No repitas los SKU por unificar.')
    with transaction.atomic():
        locked = lock_lines([target_id, *source_ids], suppliers=not library)
        target = locked.get(target_id)
        if target is None:
            raise refuse('El SKU principal ya no está en el catálogo.')
        refuse_unificada(target)
        sources, missing = [], []
        for pk in source_ids:
            source = locked.get(pk)
            if source is None:
                missing.append(str(pk))
            elif source.merged_into_id == target.pk:
                continue
            elif source.merged_into_id or source.estado == 'unificada':
                raise refuse(f'El SKU {source.sku} ya fue unificado con otro SKU. Revisa el catálogo actual.')
            else:
                sources.append(source)
        if missing:
            raise refuse('Uno de los SKU por unificar ya no está en el catálogo.')
        if not sources:
            return None
        if library and (any(source.estado != 'referencia' for source in sources)
                        or m.SupplierItem.objects.filter(part_id__in=[source.pk for source in sources]).exists()):
            raise refuse('Solo una línea de la Biblioteca en referencia, sin artículos, se une así.')
        ids = [target.pk, *(source.pk for source in sources)]
        if automatic:
            kept = kept_apart_among(ids)
            if kept:
                skus = {str(line.pk): line.sku for line in [target, *sources]}
                a, b = kept[0]
                raise refuse(f'{skus.get(a, a)} y {skus.get(b, b)} se mantienen separados (Separar SKU o un caso descartado): '
                             'una persona decide si agruparlos.')
            requested_by, actor = getattr(actor, 'username', '') or '', (actor if getattr(actor, 'username', '') == SERVICE_ACCOUNT
                                                                         else service_actor())
        else:
            requested_by = getattr(actor, 'username', '') or ''
        source_ids = {source.pk for source in sources}
        subgrupo = sheet_subgrupo(target, sources)  # MergeGuard when the sheets belong to another subgrupo
        subgrupo_before = target.part_type_id
        if subgrupo is not None and target.part_type_id is None:  # the sheets' subgrupo comes first, so they land on its template
            from .lines import set_subgrupo
            target.save(update_fields=set_subgrupo(target, subgrupo))
        children = [line for line in locked.values() if line.merged_into_id in source_ids]
        retired_ids = source_ids | {child.pk for child in children}
        participants = retired_ids | {target.pk}
        owner = {source.pk: source for source in sources} | {child.pk: locked[child.merged_into_id] for child in children}
        # An item on a source with no Reference yet (written before S3's backfill) hangs from its line's Reference for its code first,
        # so it moves with the rest and Separar gives it back.
        from .variants import ensure_attached
        for item in m.SupplierItem.objects.filter(part_id__in=retired_ids, reference__isnull=True).order_by('pk'):
            ensure_attached(item, actor=actor)
        # References (interno codes too: they go with their line), their claves' advisory locks, then the rows (plan §1.6.5).
        rows = m.Reference.all_objects.filter(part_id__in=participants, estado__in=references.LIVE)
        live = list(rows.order_by('pk'))
        references.lock(*{row.clave for row in live})
        live = list(rows.select_for_update().order_by('pk'))
        incoming = [row for row in live if row.part_id in retired_ids]
        # One Reference per key on the merged line: the target's, else the surest incoming one (a source's main before its other
        # codes); the others of that key are absorbed into it.
        claimed = {(row.clave, row.brand, row.supplier_id): row for row in live if row.part_id == target.pk}
        absorbed_rows, moving = [], []
        for row in sorted(incoming, key=lambda row: (references.ESTADO_RANK.get(row.estado, 9), not row.is_main, row.pk)):
            key = (row.clave, row.brand, row.supplier_id)
            (absorbed_rows if key in claimed else moving).append(row)
            claimed.setdefault(key, row)
        moving.sort(key=lambda row: row.pk)
        # The brandless reservation (I5): a key moving to the target must not clash with one another line holds; a code another
        # line's codigo is would make the merged line answer for that line.
        for row in moving:
            clash = (references.conflicts(row.code, row.brand, supplier=row.supplier_id, estado=row.estado)
                     .exclude(part_id__in=participants).order_by('pk').first())
            if clash is not None:
                raise refuse(f'El código {row.code} pertenece a otro SKU. Corrige su asignación antes de unificar.')
        for row in incoming:
            holder = _codigo_holder(row.code, participants, target)
            if holder is not None:
                raise refuse(f'El código {holder.sku} identifica otro SKU. Corrige su asignación antes de unificar.')
        for source in sources:
            holder = _codigo_holder(source.sku, participants, target)
            if holder is not None:  # a source's codigo another line holds as its codigo too (a spelling UNIQUE(sku) let through)
                raise refuse(f'El código {holder.sku} identifica otro SKU. Corrige su asignación antes de unificar.')
        # A library merge locks no variant: its sources hold none, and the target's stay where they are.
        items = list(m.SupplierItem.objects.select_for_update().filter(part_id__in=retired_ids if library else participants).order_by('pk'))

        brought = {source.pk: {'id': str(source.pk), 'sku': source.sku, 'estado': source.estado, 'codes': [], 'items': [], 'images': [],
                               'references': [], 'main': None, 'absorbed': [], 'undisputed': [], 'favorites': {'moved': [], 'dropped': []},
                               'links': [], 'children': [], 'dependents': {}, 'specs': [], 'applications': []} for source in sources}
        for child in children:
            brought[child.merged_into_id]['children'].append(str(child.pk))
        mains = {row.part_id: row for row in incoming if row.is_main}
        for row in incoming:
            entry = brought[owner[row.part_id].pk]
            if row.is_main and row.part_id in source_ids:
                entry['main'] = row.pk
            elif not row.is_main and row.tipo != 'interno':  # a supplier's interno code is no alterno (S3); it moves all the same
                entry['codes'].append({'brand': row.brand, 'code': row.code})
        summary = {'target_created': target_created, 'merged_skus': len(sources), 'codes_transferred': 0, 'sku_aliases_created': 0,
                   'supplier_items_relinked': 0, 'references_moved': 0, 'references_absorbed': 0, 'reason': reason or '', 'automatic': bool(automatic),
                   'requested_by': requested_by}
        if assistant_job:
            summary['assistant_job'] = str(assistant_job)

        # References: the moves, then the absorbs (into rows that may have just moved), then the disputes the merge settles.
        moved_ids = [row.pk for row in moving]
        came_from = {row.pk: owner[row.part_id].pk for row in moving}
        references.move(moved_ids, target, actor=actor)
        for row in absorbed_rows:
            record = absorb(row, target, actor=actor, reason=f'Agrupado en {target.sku}')
            kept = m.Reference.objects.filter(pk=record['into']).first()
            if kept is not None and merge_dispute(kept) and not references.conflicts(
                    kept.code, kept.brand, line=target.pk, supplier=kept.supplier_id, estado='declarado', exclude=kept.pk).exists():
                record['into_undisputed'] = {'estado': kept.estado, 'dispute_note': kept.dispute_note}  # it waited for the absorbed one
                references.update(kept, estado='declarado', dispute_note='', actor=actor)
            brought[owner[row.part_id].pk]['absorbed'].append(record)
        for row in moving:
            brought[owner[row.part_id].pk]['references'].append(row.pk)
            if not row.is_main:
                summary['codes_transferred'] += 1
        summary['sku_aliases_created'] = sum(1 for row in moving if row.is_main and row.part_id in source_ids)
        for row in m.Reference.objects.filter(pk__in=moved_ids).order_by('pk'):
            if merge_dispute(row) and not references.conflicts(row.code, row.brand, line=target.pk, supplier=row.supplier_id,
                                                               estado='declarado', exclude=row.pk).exists():
                before = {'ref': row.pk, 'estado': row.estado, 'dispute_note': row.dispute_note}
                references.update(row, estado='declarado', dispute_note='', actor=actor)
                brought[came_from[row.pk]]['undisputed'].append(before)
        for source in sources:  # a line inserted before the backfill has no main: its codigo stays a neutral alias of the target
            if source.pk not in mains:
                alias, created = references.add(target, source.sku, '', tipo='oem' if source.tipo == 'OEM' else 'otro', actor=actor)
                if created:
                    brought[source.pk]['references'].append(alias.pk)
                    summary['sku_aliases_created'] += 1
        summary['references_moved'] = len(moving)
        summary['references_absorbed'] = len(absorbed_rows)

        # Variants (they went with their References above, S3), images, favourites.
        for item in items:
            if item.part_id in retired_ids:
                brought[owner[item.part_id].pk]['items'].append(str(item.pk))
        summary['supplier_items_relinked'] = sum(1 for item in items if item.part_id in retired_ids)
        settle_variants(retired_ids, target, actor)
        summary['images_transferred'] = move_images(retired_ids, target, brought, owner)
        summary['favorites_moved'] = move_favorites(retired_ids, target, brought, owner)
        settle_dependents(sources, target, brought, actor)
        summary['specs_moved'], summary['applications_moved'] = move_sheets(retired_ids, target, brought, owner)

        # Scalars, estado, the sources.
        before_estado = target.estado
        filled = fill_scalars(target, sources)
        stocked = {item.part_id for item in items}
        estado = target.estado if library else merged_estado([target, *sources], [line for line in [target, *sources] if line.pk in stocked])
        stamp = timezone.now()
        m.Part.objects.filter(pk__in=retired_ids).update(merged_into=target, estado='unificada', updated_at=stamp)
        merge = m.CatalogMerge.objects.create(actor=actor, target=target, sources=jsonable([brought[source.pk] for source in sources]),
                                              summary=jsonable(summary))
        fields = []
        if subgrupo is not None and subgrupo_before is None:
            summary['subgrupo'] = {'before': None, 'after': str(subgrupo.pk)}
        if filled:
            target.field_sources = {**(target.field_sources or {}), **{field: {'source': 'merge', 'from': sku, 'merge': str(merge.pk)}
                                                                       for field, (_, sku) in filled.items()}}
            fields += [*filled, 'field_sources']
        if estado != target.estado:
            from .lines import stage_estado
            fields += stage_estado(target, estado, user=None if automatic else actor)
        if fields:
            target.save(update_fields=fields)
        summary['estado'] = {'before': before_estado, 'after': target.estado}
        summary['filled'] = {field: before for field, (before, _) in filled.items()}
        summary['target_version'] = target.version
        merge.summary = jsonable(summary)
        m.CatalogMerge.objects.filter(pk=merge.pk).update(summary=merge.summary)
        from .lines import touch_visible
        touch_visible(target, stamp)
        follow_cases(retired_ids, target)
        if not library:
            from .variants import recompute_status
            recompute_status(target)
        from .search_document import refresh
        refresh([target.pk, *retired_ids])  # the sources leave the storefront by a queryset update: their Search Documents go (S7)
    return merge


def sheet_subgrupo(target, sources):
    """The subgrupo the sources' technical sheets and applications belong to, when they may move to the target (S6): the target has none,
    or the same one as every source that carries them. None when no source carries any, or when they all share the target's. Raises
    MergeGuard (guard subgrupo) otherwise: two subgrupos' sheets wait for a person."""
    m = _models()
    ids = {source.pk for source in sources}
    carrying = (set(m.PartSpecification.objects.filter(part_id__in=ids).values_list('part_id', flat=True))
                | set(m.ItemApplication.objects.filter(part_id__in=ids).values_list('part_id', flat=True)))
    if not carrying:
        return None
    # A source's subgrupo, and the subgrupo whose template each of its specs fills (TechnicalTemplate's key is its PartType).
    kinds = {source.part_type_id for source in sources if source.pk in carrying}
    kinds |= set(m.PartSpecification.objects.filter(part_id__in=ids).values_list('field__template_id', flat=True))
    if target.part_type_id is not None:
        if kinds - {target.part_type_id, None}:
            raise MergeGuard('Los SKU de origen tienen fichas técnicas o aplicaciones de otro subgrupo. Corrige el subgrupo o vacía esas fichas '
                             'antes de unificarlos.', 'subgrupo')
        return None
    kinds.discard(None)
    if len(kinds) > 1:
        raise MergeGuard('Los SKU de origen tienen fichas técnicas o aplicaciones de subgrupos distintos. Elige el subgrupo del SKU principal '
                         'antes de unificarlos.', 'subgrupo')
    return m.PartType.objects.get(pk=next(iter(kinds))) if kinds else None


def move_sheets(retired_ids, target, brought, owner):
    """The sources' specs and applications onto the target (sheet_subgrupo allowed it): a field the target already has, or an
    application it already lists in that position, stays on its source. Recorded per source for Separar. Returns (specs, applications)."""
    m = _models()
    have = set(m.PartSpecification.objects.filter(part=target).values_list('field_id', flat=True))
    specs = 0
    for spec in m.PartSpecification.objects.filter(part_id__in=retired_ids).order_by('pk'):
        if spec.field_id in have:
            continue
        have.add(spec.field_id)
        m.PartSpecification.objects.filter(pk=spec.pk).update(part=target)
        brought[owner[spec.part_id].pk]['specs'].append(spec.pk)
        specs += 1
    listed = set(m.ItemApplication.objects.filter(part=target).values_list('application_id', 'position'))
    applications = 0
    for row in m.ItemApplication.objects.filter(part_id__in=retired_ids).order_by('pk'):
        if (row.application_id, row.position) in listed:
            continue
        listed.add((row.application_id, row.position))
        m.ItemApplication.objects.filter(pk=row.pk).update(part=target)
        brought[owner[row.part_id].pk]['applications'].append(row.pk)
        applications += 1
    if specs:
        m.Part.objects.filter(pk=target.pk).update(technical_revision=F('technical_revision') + 1)
    return specs, applications


def return_sheets(part, target, entry):
    """Separar: the specs and applications a merge moved from this line go back to it, where they still are on the target."""
    m = _models()
    if entry.get('specs'):
        m.PartSpecification.objects.filter(pk__in=entry['specs'], part=target).update(part=part)
    if entry.get('applications'):
        m.ItemApplication.objects.filter(pk__in=entry['applications'], part=target).update(part=part)


def follow_cases(retired_ids, target):
    """The open matching cases that list a merged line list the line it went into too: the worker checks them again (a line case whose
    lines are one line now attaches its item; a merge case whose lines are one is settled)."""
    from .matching_models import MatchingCase
    retired = list(retired_ids)
    cases = list(MatchingCase.objects.filter(status='review', lines__in=retired).distinct())
    for case in cases:
        if case.kind == 'variant':
            # A variant case is about its item on the line it hangs from, and the item went with its line: the case moves to the target
            # (lines and key, variant:<item>:<line>), so a load, the worker and a decision keep finding it.
            case.lines.remove(*retired)
            case.lines.add(target)
            key = f'variant:{case.item_id}:{target.pk}'
            if MatchingCase.objects.filter(key=key).exclude(pk=case.pk).exists():  # one already asks about the item on the target
                from .reference_matching import close_case
                close_case(case, 'applied', actor=None, note=f'La línea se unificó en {target.sku}.')
            else:
                MatchingCase.objects.filter(pk=case.pk).update(key=key)
            continue
        case.lines.add(target)
    if cases:
        from .matching_queue import enqueue_matching
        enqueue_matching()
    return len(cases)


def settle_variants(retired_ids, target, actor):
    """Variants still on a merged line once its References moved: one with no Reference yet (written before S3's backfill) hangs from
    the target's Reference for its code (mall.variants.vincular, its status kept); one whose Reference stayed behind follows the
    target the same way. A merge approves no code: a waiting code keeps waiting (rule 5b, OD7). Returns how many."""
    m = _models()
    from .variants import vincular
    left = list(m.SupplierItem.objects.select_for_update().filter(part_id__in=retired_ids).order_by('pk'))
    for item in left:
        vincular(item, target, status=item.matching_status, actor=actor, approve=False)
    return len(left)





def move_images(retired_ids, target, brought, owner):
    """The sources' photos go to the end of the target's gallery (their storage keys never change); one the target already shows
    (the same file) stays where it is."""
    m = _models()
    maximum = m.PartImage.objects.filter(part=target).aggregate(value=Max('position'))['value']
    position = maximum + 1 if maximum is not None else 0
    shown = set(m.PartImage.objects.filter(part=target).values_list('image', flat=True))
    images = [image for image in m.PartImage.objects.filter(part_id__in=retired_ids).order_by('part_id', 'position', 'created_at', 'id')
              if image.image.name not in shown]
    for offset, image in enumerate(images):
        brought[owner[image.part_id].pk]['images'].append(str(image.pk))
        image.part, image.position = target, position + offset
    m.PartImage.objects.bulk_update(images, ['part', 'position'])
    return len(images)


def move_favorites(retired_ids, target, brought, owner):
    """One favourite per user on the target, with its first save date (mall.wishlist.merge_wishlists); what moved and whose favourite
    was a duplicate are recorded, so Separar gives them back."""
    m = _models()
    from .wishlist import merge_wishlists
    rows = list(m.WishlistItem.objects.filter(part_id__in=retired_ids | {target.pk}).order_by('saved_at', 'pk').values_list('pk', 'user_id', 'part_id'))
    kept = {}
    for pk, user, part in rows:
        kept.setdefault(user, (pk, part))
    for pk, user, part in rows:
        if part in retired_ids:
            entry = brought[owner[part].pk]['favorites']
            if kept[user][0] == pk:
                entry['moved'].append(pk)
            else:
                entry['dropped'].append(user)
        elif kept[user][0] != pk and kept[user][1] in retired_ids:  # the target's own, younger than a source's that takes its place
            brought[owner[kept[user][1]].pk]['favorites'].setdefault('target_dropped', []).append(user)
    merge_wishlists(retired_ids, target)
    return sum(len(entry['favorites']['moved']) for entry in brought.values())





def settle_dependents(sources, target, brought, actor):
    """What hangs from a source and only means something on a line of its own: its open OEM review case is resolved ("línea
    unificada"), its pending automatic OEM candidate excluded, its pending assistant suggestions dismissed. The values before are
    recorded, so Separar puts them back."""
    from .catalog_assistant_models import CatalogAssistantSuggestion
    from .oem_finder_models import OEMAutoCandidate, OEMReviewCase
    now = timezone.now()
    for source in sources:
        entry = brought[source.pk]['dependents']
        case = OEMReviewCase.objects.filter(part=source, status='review').first()
        if case is not None:
            entry['oem_review'] = {'id': case.pk, 'status': case.status, 'decision': case.decision or {},
                                   'decided_at': case.decided_at.isoformat() if case.decided_at else None, 'decided_by': case.decided_by_id}
            case.status, case.decided_at = 'resolved', now
            case.decided_by = actor if getattr(actor, 'pk', None) else None
            case.decision = {**(case.decision or {}), 'action': 'resolved', 'reason': 'línea unificada', 'into': str(target.pk)}
            case.save(update_fields=['status', 'decided_at', 'decided_by', 'decision', 'updated_at'])
        candidate = OEMAutoCandidate.objects.filter(part=source, status='pending').first()
        if candidate is not None:
            entry['oem_auto'] = {'id': candidate.pk, 'status': candidate.status, 'reason': candidate.reason, 'note': candidate.note}
            candidate.status, candidate.reason, candidate.note = 'excluded', 'merged', f'Línea unificada en {target.sku}'[:300]
            candidate.save(update_fields=['status', 'reason', 'note', 'updated_at'])
        pending = list(CatalogAssistantSuggestion.objects.filter(source=source, status='pending').values_list('pk', flat=True))
        if pending:
            entry['suggestions'] = pending
            CatalogAssistantSuggestion.objects.filter(pk__in=pending).update(status='dismissed')


def restore_dependents(entry, line):
    """Put back what settle_dependents changed on a line Separar brings back, where nothing changed it since."""
    from .catalog_assistant_models import CatalogAssistantSuggestion
    from .oem_finder_models import OEMAutoCandidate, OEMReviewCase
    dependents = entry.get('dependents') or {}
    case = dependents.get('oem_review')
    if case:  # what it held before the merge (a merge recorded before S2b's review fixes kept only id and status)
        from django.utils.dateparse import parse_datetime
        OEMReviewCase.objects.filter(pk=case['id'], part=line, status='resolved', decision__reason='línea unificada').update(
            status=case['status'], decided_at=parse_datetime(case['decided_at']) if case.get('decided_at') else None,
            decided_by_id=case.get('decided_by'), decision=case.get('decision') or {}, updated_at=timezone.now())
    candidate = dependents.get('oem_auto')
    if candidate:
        OEMAutoCandidate.objects.filter(pk=candidate['id'], part=line, status='excluded', reason='merged').update(
            status=candidate['status'], reason=candidate['reason'], note=candidate['note'], updated_at=timezone.now())
    if dependents.get('suggestions'):
        CatalogAssistantSuggestion.objects.filter(pk__in=dependents['suggestions'], source=line, status='dismissed').update(status='pending')


def fill_scalars(target, sources):
    """{field: (target's blank before, the source sku it came from)} for the target's blank SCALARS the sources agree on; set on
    target (unsaved)."""
    filled = {}
    for field in SCALARS:
        if (getattr(target, field) or '').strip():
            continue
        values = {}
        for source in sources:
            if (getattr(source, field) or '').strip():
                values.setdefault((getattr(source, field) or '').strip(), source.sku)
        if len(values) == 1:
            value, sku = next(iter(values.items()))
            filled[field] = (getattr(target, field) or '', sku)
            setattr(target, field, value)
    return filled


# --- what each row most likely belonged to (Separar SKU's plan) ----------------------------------------------------------------------

def merge_history(target, children):
    """({merged line id: what its merge recorded}, merges): when, by whom, automatic or reviewed, why, and what the line brought: the
    alternos, stock rows and photos the merge recorded (merges since Separar SKU), or the alternos in the matching worker's snapshot
    (codes None when nothing recorded them); since S2b also the References it moved (references), its former main (main), those
    absorbed (absorbed), the codes put back to declarado (undisputed), favourites, links, dependents and its estado. The latest merge
    of each line is the one that counts (merged, split, merged again)."""
    m = _models()
    ids = {str(child.pk) for child in children}
    merges = list(m.CatalogMerge.objects.filter(target_id__in=[target.pk, *ids]).select_related('actor').order_by('created_at', 'pk'))
    decisions = {}
    if merges:
        for decision in m.MatchingDecision.objects.filter(evidence__merge_id__in=[str(merge.pk) for merge in merges]):
            decisions[str(decision.evidence.get('merge_id'))] = decision
    history = {}
    for merge in merges:
        decision = decisions.get(str(merge.pk))
        snapshots = {str(row.get('id')): row for row in decision.evidence.get('sources') or []} if decision else {}
        method = (decision.evidence.get('method') or '') if decision else ((merge.summary or {}).get('reason') or '')
        automatic = merge.actor.username == SERVICE_ACCOUNT
        for source in merge.sources:
            pk = str(source.get('id'))
            if pk not in ids:
                continue
            recorded = source if 'codes' in source else snapshots.get(pk)
            history[pk] = {'merged_at': merge.created_at, 'merged_by': merge.actor.username, 'automatic': automatic,
                           'method': METHODS.get(method, method) if method else 'Agrupar SKU', 'merge': str(merge.pk),
                           'codes': [{'brand': code.get('brand', ''), 'code': code.get('code', '')} for code in recorded.get('codes', [])] if recorded else None,
                           'items': [str(item) for item in source.get('items', [])], 'images': [str(image) for image in source.get('images', [])],
                           's2b': 'references' in source, 'references': [int(pk) for pk in source.get('references', [])],
                           'main': source.get('main'), 'absorbed': list(source.get('absorbed') or []),
                           'undisputed': list(source.get('undisputed') or []), 'favorites': source.get('favorites') or {},
                           'links': list(source.get('links') or []), 'dependents': source.get('dependents') or {},
                           'specs': list(source.get('specs') or []), 'applications': list(source.get('applications') or []),
                           'estado': source.get('estado') or 'publicada'}
    return history, merges


def code_returns(code, child, history):
    """Whether a grouped SKU's own code, filed on the canonical SKU, goes back to that SKU when it is restored: when the SKU had it before
    the merge (a brand-suffixed SKU files its code as a brand alterno). Otherwise it is retired: the SKU code identifies the part again.
    The SKU's former main always goes back, as its main (main_row)."""
    from .catalog_identity import company_reference
    snapshot = history.get(str(child.pk), {}).get('codes')
    return {'brand': code.brand, 'code': code.code} in snapshot if snapshot is not None else bool(company_reference(child.sku))


def main_row(child, codes, history):
    """The target's Reference (of codes, {id: row}, the target's non-main ones) that comes back as the child's main: the one its merge
    recorded (S2b; None when the merge absorbed it: it comes back from retirada on the child), or, for a merge made before S2b, the
    brandless alias of the child's codigo, which was its main then (the exact spelling first)."""
    entry = history.get(str(child.pk), {})
    if entry.get('s2b'):
        if entry.get('main') is not None:
            return codes.get(entry['main'])
        recorded = [codes[pk] for pk in entry.get('references', []) if pk in codes]  # the alias a line without a main left
        found = [code for code in recorded if code.clave == references.clave(child.sku) and not code.brand]
        return found[0] if found else None
    key = references.clave(child.sku)
    found = sorted((code for code in codes.values() if code.clave == key and not code.brand and code.supplier_id is None),
                   key=lambda code: (code.code != child.sku, code.pk))
    return found[0] if found else None


class Owners:
    """The restored line a row most likely came from ('' for the canonical line). A row the merge recorded goes to the line that brought
    it, and a code a snapshot lists to the line merged first (its first owner); otherwise a stock row goes to the line its supplier code
    names and a photo to the line in its storage path. A collapsed spelling (the target and a child, or two children, with one compact
    code: 13780-75F00 and 13780-75F00_) splits by the exact codigo (B16)."""

    def __init__(self, children, history, target=None):
        from .catalog_families import normalized_reference
        from .catalog_identity import company_reference
        late = timezone.now()
        ordered = sorted(children, key=lambda child: (history.get(str(child.pk), {}).get('merged_at') or late, child.sku))
        self.ids = [str(child.pk) for child in ordered]
        numbers = [normalized_reference(child.sku) for child in ordered] + ([normalized_reference(target.sku)] if target is not None else [])
        self.collapsed = {number for number in numbers if numbers.count(number) > 1}
        self.exact = {references.code_key(child.sku): str(child.pk) for child in ordered}
        self.skus = {}
        for child in ordered:
            self.skus.setdefault(normalized_reference(child.sku), str(child.pk))
        self.snapshot, self.numbers, self.company, self.company_numbers, self.items, self.images, self.refs = {}, {}, {}, {}, {}, {}, {}
        for child in ordered:
            pk = str(child.pk)
            entry = history.get(pk, {})
            for item in entry.get('items') or []:
                self.items.setdefault(item, pk)
            for image in entry.get('images') or []:
                self.images.setdefault(image, pk)
            for ref in entry.get('references') or []:
                self.refs.setdefault(int(ref), pk)
            for code in entry.get('codes') or []:
                self.snapshot.setdefault((code['brand'], code['code']), pk)
                self.numbers.setdefault(normalized_reference(code['code']), pk)
            # A supplier-suffixed SKU code (0110-ACA30-SPK) names its brand's own code: the merge filed it as a brand alterno.
            info = company_reference(child.sku)
            if info:
                number = normalized_reference(info['code'])
                self.company.setdefault((info['brand'], number), pk)
                self.company_numbers[number] = pk if number not in self.company_numbers else ''

    def by_number(self, number, code):
        """The child a code names through its compact code; a collapsed one only by its exact spelling."""
        if number in self.collapsed:
            return self.exact.get(references.code_key(code), '')
        return self.skus.get(number, '')

    def code(self, code):
        """(owner, alias_of): alias_of is the restored line whose own code this alterno is."""
        from .catalog_families import normalized_reference
        number = normalized_reference(code.code)
        alias = self.by_number(number, code.code)
        if alias:
            return alias, alias
        return self.refs.get(code.pk) or self.snapshot.get((code.brand, code.code)) or self.company.get((code.brand, number), ''), ''

    def item(self, item):
        from .catalog_families import normalized_reference
        if str(item.pk) in self.items:
            return self.items[str(item.pk)]
        number = normalized_reference(item.codigo)
        brand = (item.brand or '').strip().upper()
        if number in self.collapsed:
            return self.exact.get(references.code_key(item.codigo), '')
        return (self.skus.get(number) or self.numbers.get(number) or self.company.get((brand, number))
                or ('' if brand else self.company_numbers.get(number, '')))

    def image(self, image):
        name = image.image.name or ''
        return self.images.get(str(image.pk)) or next((pk for pk in self.ids if f'/{pk}/' in name), '')





# --- split ---------------------------------------------------------------------------------------------------------------------------

def split_line(target_id, restore, *, actor, reason=''):
    """Separar SKU: bring back the lines merged into target that restore names ([{'id', 'codes': [Reference ids], 'items': [variant
    ids], 'images': [image ids]}]), each with the rows assigned to it and its former main (see the module's docstring), all or nothing.
    Returns {'split', 'target', 'restored': [Part], 'summary'}."""
    m = _models()
    from .catalog_families import normalized_reference
    from .catalog_split_models import CatalogSplit
    with transaction.atomic():
        # Supplier accounts first, then the catalog rows: the lock order of stock imports and merges.
        suppliers = sorted(set(m.SupplierItem.objects.filter(part_id=target_id).values_list('supplier_id', flat=True)))
        list(m.Account.objects.select_for_update().filter(pk__in=suppliers).order_by('pk').values_list('pk', flat=True))
        if importing(suppliers):
            raise SupplierImporting('Un proveedor de este SKU está importando su inventario. Espera a que termine y vuelve a intentarlo.')
        target = m.Part.objects.select_for_update().filter(pk=target_id).first()
        if target is None or target.merged_into_id:
            raise refuse('Este SKU ya no está en el catálogo o se agrupó en otro. Vuelve a cargar el inventario.')
        children = {str(part.pk): part for part in m.Part.objects.select_for_update().filter(merged_into=target).order_by('pk')}
        wanted = {}
        for entry in restore:
            pk = str(entry['id'])
            if pk not in children:
                raise refuse(f'Uno de los SKU ya no está agrupado en {target.sku}. Vuelve a cargar la separación.')
            if pk in wanted:
                raise refuse('No repitas un SKU por separar.')
            wanted[pk] = entry
        # The claves of the target's codes before their rows (mall.references' advisory locks); the target's main is its SKU: it stays.
        references.lock(*m.Reference.objects.filter(part=target, is_main=False).values_list('clave', flat=True),
                        *(references.clave(children[pk].sku) for pk in wanted))
        codes = {code.pk: code for code in m.Reference.objects.select_for_update().filter(part=target, is_main=False).order_by('pk')}
        items = {str(item.pk): item for item in m.SupplierItem.objects.select_for_update().filter(part=target).order_by('pk')}
        images = {str(image.pk): image for image in m.PartImage.objects.select_for_update().filter(part=target).order_by('pk')}
        plan = {pk: {'codes': [], 'items': [], 'images': []} for pk in wanted}
        taken = {}
        for pk, entry in wanted.items():
            for kind, rows, keys in (('codes', codes, entry.get('codes', [])), ('items', items, map(str, entry.get('items', []))),
                                     ('images', images, map(str, entry.get('images', [])))):
                for key in keys:
                    if key not in rows:
                        raise refuse(f'{target.sku} cambió desde que abriste la separación. Vuelve a cargarla.')
                    if taken.setdefault((kind, key), pk) != pk:
                        raise refuse('Cada alterno, existencia o imagen va a un solo SKU.')
                    if key not in plan[pk][kind]:
                        plan[pk][kind].append(key)
        history, merges = merge_history(target, list(children.values()))
        owners = Owners(list(children.values()), history, target)
        mains = {pk: main_row(children[pk], codes, history) for pk in wanted}
        main_ids = {row.pk: pk for pk, row in mains.items() if row is not None}
        target_number, removed = normalized_reference(target.sku), []
        for code in codes.values():
            number, claimed = normalized_reference(code.code), taken.get(('codes', code.pk))
            alias = main_ids.get(code.pk) or owners.by_number(number, code.code)
            if alias and code.pk not in main_ids and code.pk not in owners.refs and history.get(alias, {}).get('s2b'):
                # A merge that recorded what each line brought (S2b on): a code with a restored line's number that it did not bring
                # was the target's before the merge (CHEVROLET 90499401 on 90499401-K, VAUXHALL 90499401 merged in by S9) and stays.
                # A merge from before S2b recorded no References: its restored line's own code is retired as before (code_returns).
                alias = ''
            if alias:
                # A grouped line's own code is that line's and never passes to another. It stays on the canonical line while its line
                # stays grouped; its former main goes back as its main, any other alias of it goes back when it had it (code_returns)
                # and is retired otherwise, so the worker never sees it as an alterno of the canonical line.
                if claimed and claimed != alias:
                    raise refuse(f'{code.code} es el código de {children[alias].sku}: no puede pasar a otro SKU.')
                if alias not in wanted:
                    continue
                if main_ids.get(code.pk) == alias or code_returns(code, children[alias], history):
                    if not claimed:
                        plan[alias]['codes'].append(code.pk)
                else:
                    if claimed:
                        plan[alias]['codes'].remove(code.pk)
                    removed.append(code)
            elif claimed and number == target_number and number not in owners.collapsed:
                raise refuse(f'{code.code} es el código de {target.sku}: cámbialo en Editar antes de pasarlo a otro SKU.')
        for pk in wanted:
            clash = (m.Reference.objects.filter(code=children[pk].sku, estado__in=references.EXCLUSIVE)
                     .exclude(part_id__in=[target.pk, *wanted]).select_related('part').first())
            if clash:
                raise refuse(f'{children[pk].sku} es alterno de {clash.part.sku}. Corrige esa asignación antes de separarlo.')

        restored = [children[pk] for pk in wanted]
        destination = {code_pk: children[pk].sku for pk in wanted for code_pk in plan[pk]['codes']}
        # Former mains and the codes their merge put back to declarado come back all the same, in disputa when another line keeps
        # their key (settle_returned).
        returning = set(main_ids) | {record['ref'] for pk in wanted for record in history.get(pk, {}).get('undisputed') or []}
        for part in restored:  # a moved code must not clash on its new line with a code left elsewhere (the brandless reservation, I5)
            moving = [codes[pk] for pk in plan[str(part.pk)]['codes']]
            for code in moving:
                if code.pk in returning:
                    continue
                clash = (references.conflicts(code.code, code.brand, line=part.pk, supplier=code.supplier_id, estado=code.estado)
                         .exclude(pk__in=[row.pk for row in [*moving, *removed]]).select_related('part').first())  # removed ones retire
                if clash:
                    where = destination.get(clash.pk) or clash.part.sku
                    raise refuse(f'{code.code} quedaría en {part.sku} con la clave de {clash}, que queda en {where} (un código sin marca vale '
                                 'para todas las marcas). Pasa los dos al mismo SKU o corrige uno en Editar antes de separar.')

        summary = {'restored_skus': len(restored), 'codes_moved': 0, 'aliases_removed': len(removed), 'items_moved': 0, 'images_moved': 0,
                   'target_retired': False, 'mains_returned': 0, 'references_revived': 0, 'favorites_returned': 0}
        # A supplier's interno codes (left out of the plan's code list, S3) go where every variant hanging from them goes.
        assigned = {key: pk for pk in plan for key in plan[pk]['items']}
        for ref in m.Reference.all_objects.filter(part=target, tipo='interno', is_main=False, estado__in=references.LIVE).order_by('pk'):
            hung = {str(pk) for pk in m.SupplierItem.objects.filter(reference_id=ref.pk).values_list('pk', flat=True)}
            places = {assigned.get(key) for key in hung}
            if hung and len(places) == 1 and None not in places:
                plan[places.pop()]['codes'].append(ref.pk)
        for part in restored:
            moves = plan[str(part.pk)]
            references.move(moves['codes'], part, actor=actor)  # their variants go with them (S3)
            m.PartImage.objects.filter(pk__in=moves['images']).update(part=part)
            summary['codes_moved'] += len(moves['codes'])
            summary['items_moved'] += len(moves['items'])
            summary['images_moved'] += len(moves['images'])
        if removed:  # retired, never deleted (mall.references)
            references.retire(removed, actor=actor, reason='Separar SKU: un alias que la unificación creó para un SKU restaurado',
                              allow_variants=True)  # their variants re-hang below (place_split_variants)
        for part in [target, *restored]:
            gallery = list(m.PartImage.objects.filter(part=part).order_by('position', 'created_at', 'pk'))
            for position, image in enumerate(gallery):
                image.position = position
            m.PartImage.objects.bulk_update(gallery, ['position'])
        target_main = references.main_of(target, lock=True)
        for part in restored:
            entry = history.get(str(part.pk), {})
            if target_main is not None and part.sku in target_main.printed_forms:  # a collapsed spelling goes back with its line (B16)
                target_main = references.update(target_main, printed_forms=[form for form in target_main.printed_forms if form != part.sku],
                                                actor=actor)
            summary['references_revived'] += revive_absorbed(part, entry, actor)
            estado = entry.get('estado') if entry.get('estado') in references_live_estados() else 'publicada'
            part.merged_into, part.estado = None, estado
            part._main_followed = True  # its main is set just below
            part.save(update_fields=['merged_into', 'estado'])
            summary['mains_returned'] += settle_returned(part, entry, mains[str(part.pk)], actor)
            summary['favorites_returned'] += return_favorites(part, target, entry)
            restore_dependents(entry, part)
            return_sheets(part, target, entry)
        place_split_variants(target, restored, items, plan)

        # A parent SKU the grouping created and the split leaves empty has nothing to identify: it retires, which also keeps the worker
        # from proposing the same parent again (mall.matching_engine.catalog_families blocks inactive bases).
        created = any(merge.target_id == target.pk and merge.summary.get('target_created') for merge in merges)
        retired = bool(created and target.estado == 'publicada' and not m.Part.objects.filter(merged_into=target).exists()
                       and not m.Reference.objects.filter(part=target, is_main=False).exists()
                       and not m.SupplierItem.objects.filter(part=target).exists() and not m.PartImage.objects.filter(part=target).exists())
        if retired:
            target.estado = 'retirada'
            target.save(update_fields=['estado'])
        summary['target_retired'] = retired
        split = CatalogSplit.objects.create(target=target, actor=actor, reason=(reason or '').strip(), summary=summary,
                                            restored=[{'id': str(part.pk), 'sku': part.sku} for part in restored],
                                            moved={'parts': {pk: {'codes': plan[pk]['codes'], 'items': plan[pk]['items'], 'images': plan[pk]['images']} for pk in plan},
                                                   'aliases_removed': [{'brand': code.brand, 'code': code.code} for code in removed],
                                                   'mains': {pk: (row.pk if row is not None else None) for pk, row in mains.items()}})
        keep_apart([str(target.pk), *wanted], actor=actor, split=split, reason='separar')
        from .lines import touch_visible
        touch_visible([target, *restored])
        from .search_document import refresh
        refresh([target, *restored])  # S7
    return {'split': split, 'target': target, 'restored': restored, 'summary': summary}


def place_split_variants(target, restored, items, plan):
    """S3: a variant goes where its Reference went. One assigned to a line its Reference did not go to, or whose Reference the split
    retired (an alias that does not return), re-hangs from that line's Reference of its key (an absorbed one that came back, a former
    main), and the split is refused when that line has none: the Reference has to go with it."""
    m = _models()
    from .variants import hang, reference_for
    wanted = {key: pk for pk in plan for key in plan[pk]['items']}
    lines = {str(target.pk): target, **{str(part.pk): part for part in restored}}
    now = {str(row['pk']): row for row in m.SupplierItem.objects.filter(pk__in=list(items)).values('pk', 'part_id', 'reference_id',
                                                                                                   'reference__estado')}
    for key, item in items.items():
        destination = lines[wanted.get(key, str(target.pk))]
        row = now.get(key)
        if row is None or (row['part_id'] == destination.pk and row['reference__estado'] != 'retirada'):
            continue
        ref, _ = reference_for(destination, item.codigo, item.brand, item.supplier_id)
        if ref is None:
            where = lines.get(str(row['part_id']))
            raise refuse(f'El artículo {item.codigo} ({item.supplier_invent_id}) cuelga de una referencia que queda en '
                         f'{where.sku if where else "otro SKU"}: pasa esa referencia con él a {destination.sku}, o deja el artículo con ella.')
        hang([row['pk']], ref)


def references_live_estados():
    from .lines import LIVE
    return LIVE


def revive_absorbed(part, entry, actor):
    """The References a merge absorbed from this line come back from retirada (the estado they had, or disputa when another line
    keeps their key), their spellings leave the target's Reference and the sources copied onto it go. Returns how many came back."""
    m = _models()
    count = 0
    for record in entry.get('absorbed') or []:
        ref = m.Reference.all_objects.select_for_update().filter(pk=record['ref'], part=part, estado='retirada').first()
        if ref is None:
            continue
        kept = m.Reference.all_objects.select_for_update().filter(pk=record['into']).first()
        if kept is not None and record.get('spelling') and record['spelling'] in kept.printed_forms:
            kept = references.update(kept, printed_forms=[form for form in kept.printed_forms if form != record['spelling']], actor=actor)
        if kept is not None and record.get('into_undisputed') and kept.estado in references.EXCLUSIVE:
            kept = references.update(kept, estado='disputa', dispute_note=record['into_undisputed']['dispute_note'], actor=actor)
        if record.get('sources'):
            m.ReferenceSource.objects.filter(pk__in=record['sources'], reference_id=record['into']).delete()
        return_library_rows(record, actor)
        if record.get('estado') == 'disputa' and record.get('dispute_note'):  # it waited in disputa: it does again, with its note
            references.update(ref, estado='disputa', dispute_note=record['dispute_note'], actor=actor)
            count += 1
            continue
        estado = record.get('estado') if record.get('estado') in references.EXCLUSIVE else 'declarado'
        try:
            with transaction.atomic():
                references.update(ref, estado=estado, actor=actor)
        except references.ReferenceConflict as error:
            holder = error.reference or kept
            references.update(ref, estado='disputa', dispute_note=references.dispute_note_for(ref.code, holder), actor=actor)
        count += 1
    return count


def dispute_if_held(ref, actor, note=''):
    """A Reference back on its line that another line keeps the key of (the brandless reservation, I5) waits in disputa, with `note`
    or one naming that line. Returns the Reference."""
    if ref.estado not in references.EXCLUSIVE:
        return ref
    clash = (references.conflicts(ref.code, ref.brand, line=ref.part_id, supplier=ref.supplier_id, estado=ref.estado, exclude=ref.pk)
             .select_related('part').order_by('pk').first())
    if clash is None:
        return ref
    return references.update(ref, estado='disputa', dispute_note=note or references.dispute_note_for(ref.code, clash), actor=actor)


def settle_returned(part, entry, row, actor):
    """After Separar moved a line's rows back: the codes its merge put back to declarado wait in disputa again (with their note) when
    another line still holds their key, and its former main is its main again (return_main). Returns 1 when a former main came back."""
    m = _models()
    notes = {record['ref']: record.get('dispute_note') or '' for record in entry.get('undisputed') or []}
    for ref in m.Reference.objects.filter(pk__in=list(notes), part=part, estado__in=references.EXCLUSIVE).order_by('pk'):
        dispute_if_held(ref, actor, notes[ref.pk])
    return return_main(part, row, actor)


def return_main(part, row, actor):
    """The line's former main is its main again (a row moved back, or one revived from retirada), in disputa when another line keeps
    its key; a line without one gets a new one (mall.references.ensure_main). Returns 1 when a former main came back."""
    m = _models()
    if row is not None:
        row = m.Reference.all_objects.get(pk=row.pk)
    if row is None or row.estado == 'retirada' or row.part_id != part.pk:
        row = (m.Reference.objects.filter(part=part, clave=references.clave(part.sku), supplier__isnull=True)
               .order_by(references.exact_first(part.sku), 'pk').first())
    if row is not None:
        row = dispute_if_held(row, actor)
    if row is not None and row.code == part.sku:
        references.set_main(part, row, actor=actor, reason='Separar SKU')
        return 1
    references.ensure_main(part, actor=actor)
    return int(row is not None)


def return_favorites(part, target, entry):
    """The favourites the merge moved go back to the line, and those it dropped as a user's duplicate come back. Returns how many."""
    m = _models()
    favorites = entry.get('favorites') or {}
    count = 0
    for row in m.WishlistItem.objects.filter(pk__in=favorites.get('moved') or [], part=target):
        if not m.WishlistItem.objects.filter(user_id=row.user_id, part=part).exists():
            m.WishlistItem.objects.filter(pk=row.pk).update(part=part)
            count += 1
    for user in favorites.get('dropped') or []:
        _, created = m.WishlistItem.objects.get_or_create(user_id=user, part=part)
        count += int(created)
    for user in favorites.get('target_dropped') or []:  # the target's own favourite, which the source's took the place of
        m.WishlistItem.objects.get_or_create(user_id=user, part=target)
    return count





# --- the S2b backfill's compact duplicates (OD4) -------------------------------------------------------------------------------------










def detail_text(error):
    detail = getattr(error, 'detail', None)
    if isinstance(detail, dict):
        detail = detail.get('detail', detail)
    if isinstance(detail, (list, tuple)):
        detail = ' '.join(str(item) for item in detail)
    return str(detail if detail is not None else error)








