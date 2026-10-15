"""Productos nuevos as lines in preparación (inventory refactor S6.2, plan §3 S6.2): the only writer of a new product. It replaced the
write side of mall.sku_proposals, whose SkuProposal rows are history from S6 (mall.refactor.s6_new_products turned the pending ones into
lines with their ids kept).

A supplier's item that no line takes (mall.reference_matching.resolve answers no_line: rule 4) gets a line of its own in preparación,
origen proveedor, with its printed code as the codigo and main Reference, and the item hangs from that main as pending. Items with
the same key share the line (rule 1: resolve finds the main; place() joins it too when it is there). Nothing reaches the storefront
until a person decides, here or in Revisión IA (mall.sku_review):

    place(variant)                      rule 4: a new line in preparación for an item no line takes (or the line that has its key);
                                        mall.reference_matching.place calls it for no_line (loads, the worker's backlog, Separar)
    join(variant, ref)                  an item onto a Reference of a line outside the catalog: pending; rule 7, a line in referencia
                                        moves to preparación; a line rechazada takes it with no notice (OD13)
    joined(line, item)                  what an item hung on a line outside the catalog changes there (the engine's attach calls it)
    main_plan(code, brand, supplier)    the main a product's code makes: (brand, tipo, supplier of an interno code), as
                                        mall.reference_matching.placement_of decides it
    publish(line, user, version)        Aprobar: publicada (mall.lines.publish), its items matched
    reject(line, user, version, ...)    Rechazar: rechazada, its References and pending items kept (OD13)
    link(line, user, version, target)   Vincular: merged into a catalog line (mall.line_merge.merge_lines), its items matched there
    edit(line, user, version, data)     an administrator's corrections (codigo, description, OEM number, subgrupo, alternos, código
                                        interno), each field theirs from then on (field_sources), changed_at stamped
    ai_fill(line, fill, ...)            Revisión IA's fills: never changed_at (the storefront reminder is about new products, not the AI)
    refresh(line)                       the supplier-derived description follows the items while no person or AI fix owns it
    declared_cases(item, line)          rule 5b: one reference case per code an item declares that its line does not hold
                                        (mall.reference_matching.sync_declared, the writer of the S6 cases)

The backlog (items no line holds: D1's, a load whose place() failed) is the matching worker's: mall.reference_matching.backlog resolves
each one and calls place() for no_line.

The main (plan §3 S6.1 step 7, S6.2; mall.reference_matching.placement_of decides it): a supplier's own code is interno, scoped to that
supplier (Revisión IA's internal_code; a brand that is the supplier's own name, OD20; a brandless or placeholder-brand code too short to
be an OEM number, OD6). Any other code is public under its brand's canonical name: oem for a vehicle make (a dealer's genuine part is
that make's OEM number), aftermarket for a brand of parts or one the brand table does not know yet, otro for a brand of no tipo; an
OEM-like code with no brand is shared under '' (any brand), otro.

How the screens read a line (mall.sku_proposal_views keeps the JSON of the proposals): its id, its codigo (sku), its main's brand,
its OEM number (a non-main oem Reference, else the main when oem), its other public References (alternos) and its variants (items).
Its status maps from estado: preparación with items is pending (without, resolved), publicada approved, unificada linked, rechazada
rejected. A line's version is the CAS every decision checks; every write here moves it.

Lock order (plan §1.6.5): the supplier Accounts of the line's items (and of the target's, for Vincular) by id, then the lines by id,
then References (the clave's advisory lock first), then the items, then cases.
"""
import logging
import re

from django.db import connection, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from . import lines, references, variants
from .lines import LineStateError, all_lines, storefront_lines
from .sku_proposals import (PROPOSAL_LOCK, DuplicateProposal, ProposalConflict, catalog_owners, clean,
                            merge_references, owner_label, vehicle_makes)

logger = logging.getLogger(__name__)
TAG = 's6'                      # Reference.detail key on the mains S6 made: mall.refactor.s6_new_products --rollback finds them
PLACED_REASON = 'Colocado en Productos nuevos'  # a legacy supplier case of an item placed on a line (A5)
DECLARED_METHOD = 'referencia_declarada'      # a reference case of a declared code (rule 5b: mall.reference_matching.METHODS)
CASE_KIND = 'reference'
REASONS = lines.REASONS  # Part.REASONS (SkuProposal.REJECT_REASONS)
EDITABLE = {'sku', 'description', 'oem_code', 'oem_brand', 'part_type', 'references', 'internal_code'}
AUTOMATIC = ('supplier', 'oem_library')  # field_sources the suppliers' data may replace


class PlaceRefused(Exception):
    """place() could not give the item a line: the key sits on a catalog line (the matching attaches it there), on two or more lines
    (rule 2: a line case decides), or the item has no code. The item stays pending, without a Reference."""


def _models():
    from .matching_models import MatchingCase, MatchingDecision
    from .models import Account, Part, Reference, SupplierItem
    return Account, Part, Reference, SupplierItem, MatchingCase, MatchingDecision


def compact(code):
    return re.sub(r'[^A-Z0-9]', '', references.code_key(code))


def stamp(how, **extra):
    return {'como': how, 'at': timezone.now().isoformat(), **extra}


def citation(item=None, line=None, user=None):
    """The reference_source text of a code a new product brings: who declared it (a proveedor source, plan A3) or who set it (admin)."""
    tag = f' · producto nuevo {str(line.pk)[:8]}' if line is not None else ''
    if user is not None:
        return f'Indicado por {getattr(user, "username", "") or "un administrador"}{tag}'[:500]
    name = getattr(getattr(item, 'supplier', None), 'name', '') or ''
    return f'Declarado por {name}{tag}'.strip()[:500]


# --- the main a product's code makes ------------------------------------------------------------------------------------------------

def canonical_brand(spelling):
    """The canonical spelling of a brand (Brand.nombre for a known spelling or alias: TOY → TOYOTA; a vehicle make the brand table
    lacks yet by mall.sku_proposals.vehicle_makes), else the normalized spelling (it becomes a brand when the Reference is written:
    mall.references.ensure_brand)."""
    from . import brands
    found = brands.lookup(spelling)
    if found is not None:
        return found.nombre
    spelling = brands.normalize(spelling)
    return vehicle_makes().get(spelling, spelling)


def public_tipo(brand):
    """The tipo of a public main under this brand (mall.reference_matching.tipo_for): oem for a vehicle make (Brand.tipo vehiculo, or a
    make of vehicle_makes the brand table lacks), aftermarket for a brand of parts or an unknown one, otro for a brand of no tipo or no
    brand."""
    from . import reference_matching
    return reference_matching.tipo_for(brand, reference_matching.Lookups())


def main_plan(code, brand, supplier, *, internal=False, oem_brand='', item=None):
    """(brand, tipo, supplier id or None) of the main Reference a product's code makes (the module's docstring), as rule 4 decides it
    (mall.reference_matching.placement_of: OD6, OD20, Revisión IA's internal_code for `item`). oem_brand: the code is that make's OEM
    number (a proposal's oem_code equal to its SKU, S6's data step)."""
    from . import reference_matching
    if oem_brand and not internal:
        return canonical_brand(oem_brand), 'oem', None
    subject = reference_matching.subject_of(item, {'codigo': code, 'brand': brand}, supplier)
    spec = reference_matching.placement_of(subject, reference_matching.Lookups(), interno=internal)
    return spec['brand'], spec['tipo'], spec['supplier']


# --- placing an item ----------------------------------------------------------------------------------------------------------------

def lock_accounts(supplier_ids):
    """The suppliers' Accounts by id (the first lock every writer of items takes); none may be importing."""
    from .supplier_import_models import SupplierInventoryImportJob
    Account = _models()[0]
    ids = sorted({pk for pk in supplier_ids if pk}, key=str)
    accounts = list(Account.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    busy = (SupplierInventoryImportJob.objects.filter(supplier_id__in=ids, status='importing', expires_at__gt=timezone.now())
            .select_related('supplier').first())
    if busy:
        raise ValidationError({'detail': f'Espera a que termine la importación de {busy.supplier.name}.'})
    return accounts


def place(variant, *, actor=None, internal=False):
    """Rule 4: the item (a SupplierItem or its id) gets a line of its own in preparación, origen proveedor, whose main is its code as
    main_plan says (internal, or the item's _placement_interno: Separar referencia asks for an interno main), and hangs from it as
    pending. When a live line already has that key (rule 1: same-key items share the line) the item joins it instead (join); an interno
    main is one supplier's code, so only that supplier's own code is its key. Returns (line, created). Refuses (PlaceRefused) an item
    with no code, a key on a catalog line or a key the brandless reservation gives to other lines; the item then stays pending, without
    a Reference. A SupplierItem given is the one written: mall.reference_matching hands over a load's item with its new code, brand and
    description still unsaved. Lock order: the supplier's Account (the caller usually holds it), the item read, the clave's advisory
    lock, the line and its References, the item locked."""
    Account, Part, Reference, SupplierItem, _, _ = _models()
    given = variant if isinstance(variant, SupplierItem) else None
    item_id = getattr(variant, 'pk', variant)
    internal = bool(internal or getattr(given, '_placement_interno', False))
    with transaction.atomic():
        supplier_id = SupplierItem.objects.filter(pk=item_id).values_list('supplier_id', flat=True).first()
        if supplier_id is None:
            raise PlaceRefused('El artículo ya no existe.')
        list(Account.objects.select_for_update().filter(pk=supplier_id).values_list('pk', flat=True))
        item = SupplierItem.objects.select_related('supplier', 'part').get(pk=item_id)
        if item.reference_id:
            return item.part, False
        source = given if given is not None else item
        code = references.code_key(source.codigo_pendiente or source.codigo)  # an item no line holds takes a waiting code at once
        printed_brand = source.brand if source.brand_pendiente is None else source.brand_pendiente
        if not re.search(r'[0-9A-Z]', code):
            raise PlaceRefused(f'El artículo {item.supplier_invent_id} no tiene código.')
        if len(code) > 120:
            raise PlaceRefused(f'El código {code[:40]}… supera 120 caracteres.')
        brand, tipo, supplier = main_plan(code, printed_brand, item.supplier, internal=internal, item=item)
        references.lock(references.clave(code))
        held = references.find(code, brand, supplier=item.supplier_id)  # rule 1: the supplier's own code first, then the public key
        if held is not None and supplier is not None and held.supplier_id != supplier:
            held = None  # another line's public code is no key of this supplier's own code (Separar referencia)
        if held is not None:
            return join(source, held, actor=actor), False
        try:
            line = lines.create_line(code, estado='preparacion', origen='proveedor', description=(source.description or '').strip(),
                                     main_brand=brand, main_tipo=tipo, main_supplier=supplier, main_source=citation(item), actor=actor,
                                     strict=True, main_detail={TAG: stamp('place', articulo=str(item.pk))},
                                     field_sources={'sku': {'source': 'supplier'}, 'description': {'source': 'supplier'}},
                                     changed_at=timezone.now())
        except references.ReferenceConflict as error:
            raise PlaceRefused(error.message) from error
        main = references.main_of(line)
        locked = SupplierItem.objects.select_for_update().select_related('supplier').get(pk=item.pk)
        item = given if given is not None else locked
        variants.apply_pending(item)
        variants.attach(item, main, status='pending')
        placed([item])
        declared_cases(item, line)
    return line, True


def join(variant, ref, *, actor=None):
    """The item onto `ref`, a live Reference of a line outside the catalog, as pending (Productos nuevos): rule 7 moves a line in
    referencia to preparación; a line in preparación counts the change (version + 1, changed_at, its description refreshed); a line
    rechazada takes it with no notice (OD13). A catalog line refuses (PlaceRefused: the matching attaches there, its status by its own
    rules), and so does a unificada one. The engine calls it for an attach onto a line outside the catalog. Returns the line."""
    Account, Part, Reference, SupplierItem, _, _ = _models()
    given = variant if isinstance(variant, SupplierItem) else None
    item_id = getattr(variant, 'pk', variant)
    with transaction.atomic():
        supplier_id = SupplierItem.objects.filter(pk=item_id).values_list('supplier_id', flat=True).first()
        list(Account.objects.select_for_update().filter(pk=supplier_id).values_list('pk', flat=True))  # held already by place()
        ref = Reference.all_objects.get(pk=getattr(ref, 'pk', ref))
        line = all_lines(Part.objects.select_for_update()).get(pk=ref.part_id)
        if not references.is_live(line):
            raise PlaceRefused(f'El SKU {line.sku} se agrupó en otro.')
        if line.estado in lines.CATALOG:
            raise PlaceRefused(f'El código {ref.code} ya es del SKU {line.sku}: el emparejamiento vincula ahí el artículo.')
        prepared = line.estado == 'referencia'
        if prepared:
            lines.prepare(line)
        locked = SupplierItem.objects.select_for_update().select_related('supplier').get(pk=item_id)
        item = given if given is not None else locked
        variants.apply_pending(item)
        try:
            variants.attach(item, ref, status='pending')
        except variants.VariantConflict as error:
            raise PlaceRefused(error.message) from error
        declared_cases(item, line)
        line = joined(line, item, prepared=prepared)
    return line


def joined(line, item, *, prepared=False):
    """What an item just hung on `line`, a line outside the catalog, changes there (join, and mall.reference_matching's attach): the
    item's legacy supplier cases are placed; a line in preparación counts the change (version + 1, changed_at; not when rule 7 just
    moved it there) and its description follows the items (refresh, the item's own text included while unsaved); a line rechazada takes
    it with no notice (OD13). The caller holds the line locked. Returns the line as written."""
    _, Part, _, _, _, _ = _models()
    placed([item])
    line = all_lines(Part.objects.select_for_update()).get(pk=line.pk)
    if line.estado != 'preparacion':
        return line
    fields = ['description', 'field_sources'] if refresh(line, save=False, extra=[item.description]) else []
    if not prepared:
        line.version, line.changed_at = (line.version or 0) + 1, timezone.now()
        fields += ['version', 'changed_at']
    if fields:
        line.save(update_fields=fields)
    return line


LEGACY_OPEN = ('unmatched', 'review')


def placed(items):
    """The legacy supplier cases (kind supplier) of items placed on a line outside the catalog: an unmatched one (D1: no code
    evidence) or one waiting for review becomes dismissed, 'Colocado en Productos nuevos' (plan A5); one an administrator dismissed
    stays as it was."""
    MatchingCase = _models()[4]
    ids = [item.pk for item in items]
    if ids:
        MatchingCase.objects.filter(item_id__in=ids, kind='supplier', status__in=LEGACY_OPEN).update(status='dismissed', reason=PLACED_REASON)


def refresh(line, *, save=True, extra=()):
    """The supplier-derived description of a line in preparación follows its items (the most detailed one), unless a person wrote it
    or a Revisión IA fix of the same supplier text owns it (a fix of an older text gives way). save: written with version + 1 (the
    engine, after an item's description changed); otherwise the caller saves description and field_sources. extra: the texts of items
    not saved yet. Returns whether it changed."""
    _, Part, _, SupplierItem, _, _ = _models()
    if line.estado != 'preparacion':
        return False
    texts = [text.strip() for text in SupplierItem.objects.filter(part_id=line.pk).values_list('description', flat=True)]
    texts += [(text or '').strip() for text in extra]
    best = max(texts, key=len, default='')
    source = (line.field_sources or {}).get('description') or {}
    owner = source.get('source')
    if not best or (owner and owner not in AUTOMATIC and not (owner == 'ai' and source.get('from') != best)):
        return False
    if best == line.description and owner in (None, 'supplier'):
        return False
    line.description = best
    line.field_sources = {**(line.field_sources or {}), 'description': {'source': 'supplier'}}
    if save:
        line.version = (line.version or 0) + 1
        line.save(update_fields=['description', 'field_sources', 'version'])
    return True


# --- rule 5b: declared codes ---------------------------------------------------------------------------------------------------------

def declared_cases(item, line=None):
    """Rule 5b (plan §3 S6.1 step 6): one reference case per code the item declares (its references) that its line does not hold,
    keyed reference:<item>:<clave>|<brand> (§2), so writing it again changes nothing and a dismissed case stays dismissed. The S6 cases
    have one writer, mall.reference_matching (sync_declared); nothing is stored on the item, and its status does not change: a declared
    code is evidence to review, not a code change. Returns the cases opened now."""
    from .reference_matching import sync_declared
    MatchingCase = _models()[4]
    if item.reference_id is None or (line is not None and item.part_id != line.pk):
        item.refresh_from_db(fields=['reference', 'part'])  # hung in bulk (the data step): the row says where
    before = set(MatchingCase.objects.filter(item_id=item.pk, kind=CASE_KIND).values_list('pk', flat=True))
    sync_declared(item)
    return list(MatchingCase.objects.filter(item_id=item.pk, kind=CASE_KIND).exclude(pk__in=before).order_by('key'))


# --- decisions -----------------------------------------------------------------------------------------------------------------------

def locked(line_id, expected_version, *, also=()):
    """(line, its items) for a decision: the Accounts of the items on the line and on the lines in `also` first, then those lines by id,
    then the items. Refuses a line that is not in preparación or whose version moved (ProposalConflict, 409)."""
    _, Part, _, SupplierItem, _, _ = _models()
    ids = [line_id, *also]
    lock_accounts(SupplierItem.objects.filter(part_id__in=ids).values_list('supplier_id', flat=True))
    found = {line.pk: line for line in all_lines(Part.objects.select_for_update()).filter(pk__in=ids).order_by('pk')}
    line = next((row for pk, row in found.items() if str(pk) == str(line_id)), None)
    if line is None:
        raise NotFound('No existe ese producto.')
    if line.estado != 'preparacion' or line.merged_into_id:
        raise ProposalConflict('Este producto ya se decidió o se resolvió: actualiza la lista.')
    if expected_version != line.version:
        raise ProposalConflict()
    items = list(SupplierItem.objects.select_for_update().filter(part_id=line.pk).select_related('supplier').order_by('pk'))
    return line, items


def refuse_empty(items):
    if not items:
        raise ProposalConflict('El producto ya no tiene artículos: actualiza la lista.')


def oem_of(line, codes=None):
    """The line's OEM number (a Reference): a non-main live oem one first, else the main when it is oem; None otherwise. codes: the
    line's live public References already loaded (Reference.objects: the main included even when interno)."""
    Reference = _models()[2]
    rows = list(codes) if codes is not None else list(Reference.objects.filter(part_id=line.pk).order_by('pk'))
    rows = [ref for ref in rows if ref.estado in references.LIVE and ref.tipo == 'oem']
    return next((ref for ref in rows if not ref.is_main), None) or next((ref for ref in rows if ref.is_main), None)


def duplicate_checks(line, main=None, oem=None):
    """(code, brands) approving must find nowhere in the catalog: the codigo under the main's brand (any brand when it has none) and,
    when it is another number, the OEM number under its maker."""
    main = main if main is not None else references.main_of(line)
    oem = oem if oem is not None else oem_of(line)
    checks = [(line.sku, {main.brand} - {''} if main is not None else set())]
    if oem is not None and references.clave(oem.code) != references.clave(line.sku):
        checks.append((oem.code, {oem.brand} - {''}))
    return checks


def catalog_match(line, main=None, oem=None):
    """The catalog lines the codigo or the OEM number already belongs to (mall.sku_proposals.catalog_owners): its items are variants of
    one of them (Vincular)."""
    seen = {}
    for code, brands in duplicate_checks(line, main, oem):
        for pk, sku, active in catalog_owners(code, brands):
            if str(pk) != str(line.pk):
                seen.setdefault(pk, {'id': str(pk), 'sku': sku, 'code': code, 'active': active})
    return list(seen.values())


def refuse_duplicates(line, main):
    """Approving never puts on sale a second line for a code the catalog already has under another spelling, nor one whose OEM number
    another line carries: the refusal names those lines, so the drawer offers Vincular (DuplicateProposal, 400). A main in disputa
    (another line holds its key) is refused too."""
    if main is not None and main.estado == 'disputa':
        raise ValidationError({'detail': f'{main.code} está en disputa ({main.dispute_note}): vincula sus artículos a la línea que lo '
                                         'tiene o resuelve la disputa antes de aprobar.'})
    checks = duplicate_checks(line, main)
    if connection.vendor == 'postgresql':  # two approvals of the same code at once: the second sees the first's line
        with connection.cursor() as cursor:
            for key in sorted({compact(code) for code, _ in checks} - {''}):
                cursor.execute('SELECT pg_advisory_xact_lock(%s, hashtext(%s))', [PROPOSAL_LOCK, key])
    for code, brands in checks:
        owners = [owner for owner in catalog_owners(code, brands) if str(owner[0]) != str(line.pk)]
        if owners:
            usable = any(active for _, _, active in owners)
            raise DuplicateProposal(f'{code} ya está en el catálogo como SKU {", ".join(owner_label(owner) for owner in owners[:3])}: '
                                    + ('usa «Vincular a SKU existente».' if usable else 'reactívalo para vincular ahí sus artículos.'),
                                    [{'id': str(pk), 'sku': sku, 'active': active} for pk, sku, active in owners[:3]])


def settle(target, item_ids, user, action, *, source=None):
    """After a decision that put items on a catalog line: their legacy supplier cases are applied with the line's codigo (plan A5),
    each item's decision is recorded (MatchingDecision), and the pending items with no code waiting and no case that takes them out
    of stock (a code change or a variant case; a declared code does not) count in its stock: matched. Returns how many."""
    from .matching_engine import item_row, part_row
    _, Part, _, SupplierItem, MatchingCase, MatchingDecision = _models()
    item_ids = list(item_ids)
    if not item_ids:
        return 0
    legacy = {case.item_id: case for case in MatchingCase.objects.select_for_update().filter(item_id__in=item_ids, kind='supplier').order_by('pk')}
    MatchingCase.objects.filter(pk__in=[case.pk for case in legacy.values()]).exclude(status='applied').update(status='applied',
                                                                                                              target_sku=target.sku)
    blocking = MatchingCase.objects.filter(item_id=OuterRef('pk'), status='review').exclude(kind__in=(CASE_KIND, 'supplier'))
    now = timezone.now()
    count = (SupplierItem.objects.filter(pk__in=item_ids, part_id=target.pk, reference__isnull=False, matching_status='pending',
                                         codigo_pendiente__isnull=True, brand_pendiente__isnull=True)
             .exclude(Exists(blocking)).update(matching_status='matched', updated_at=now))
    row = part_row(Part.objects.prefetch_related('codes').get(pk=target.pk))
    MatchingDecision.objects.bulk_create([
        MatchingDecision(case=legacy.get(item.pk), actor=user, action=action, evidence={
            'line': str(source.pk if source is not None else target.pk), 'sources': [item_row(item)], 'target': row})
        for item in SupplierItem.objects.filter(pk__in=item_ids).order_by('pk')])
    return count


def publish(line_id, user, expected_version):
    """Aprobar: the line in preparación becomes publicada (mall.lines.publish: decided by and at, changed_at) and its items count in its
    stock (settle). Refuses a codigo or OEM number the catalog already has (DuplicateProposal) and a stale version (409). Returns the
    line."""
    from .matching_queue import enqueue_matching, matching_work
    with matching_work(), transaction.atomic():
        line, items = locked(line_id, expected_version)
        refuse_empty(items)
        main = references.main_of(line)
        refuse_duplicates(line, main)
        try:
            lines.publish(line, user=user)
        except LineStateError as error:
            raise ValidationError({'detail': str(error)}) from error
        settle(line, [item.pk for item in items], user, 'proposal_approved')
    enqueue_matching()  # other suppliers' items with the same code can attach now
    return line


def reject(line_id, user, expected_version, reason, note=''):
    """Rechazar: the line becomes rechazada with a reason (REASONS) and a note; it keeps its References and its pending items, and the
    same code joins it again with no notice (OD13). Returns the line."""
    if reason not in REASONS:
        raise ValidationError({'reason': 'Elige un motivo.'})
    with transaction.atomic():
        line, _ = locked(line_id, expected_version)
        lines.reject(line, user=user, reason=reason, note=(note or '').strip()[:2000])
    return line


def link(line_id, user, expected_version, target_id):
    """Vincular: the line is merged into a catalog line on sale (mall.line_merge.merge_lines: its References and items move there, the
    line becomes unificada) and its items count in that line's stock (settle). Returns the line (unificada)."""
    from .line_merge import merge_lines
    from .matching_queue import enqueue_matching, matching_work
    _, Part, _, _, _, _ = _models()
    with matching_work(), transaction.atomic():
        line, items = locked(line_id, expected_version, also=[target_id])
        refuse_empty(items)
        target = storefront_lines(Part.objects.select_for_update()).filter(pk=target_id).first()
        if target is None:
            raise ValidationError({'part': 'Elige un SKU activo del catálogo.'})
        merge_lines(target, [line], actor=user, reason='Productos nuevos: vinculado')
        settle(target, [item.pk for item in items], user, 'proposal_linked', source=line)
        line.refresh_from_db()
    enqueue_matching()
    return line


# --- an administrator's corrections ---------------------------------------------------------------------------------------------------

def edit(line_id, user, expected_version, data):
    """An administrator's corrections before deciding. A field whose value changed is theirs from now on (field_sources admin) and
    changed_at moves; one sent unchanged keeps its source, so the suppliers' data (and later the AI) can still fill it. Fields: sku (the
    codigo: the main's code), description, oem_code/oem_brand (the OEM number), part_type (the subgrupo), references (the alternos),
    internal_code (the main is the supplier's own code: interno). Returns the line."""
    from .part_references import normalize_references
    unknown = set(data) - EDITABLE
    if unknown:
        raise ValidationError({'detail': f'Solo se pueden cambiar: {", ".join(sorted(EDITABLE))}.'})
    if 'internal_code' in data:
        if not isinstance(data['internal_code'], bool):
            raise ValidationError({'internal_code': 'Indica sí o no.'})
        if data['internal_code'] and clean(str(data.get('oem_code') or '')):
            raise ValidationError({'internal_code': 'Un código interno no lleva número OEM: quita el número o desmarca «Código interno».'})
    with transaction.atomic():
        line, _ = locked(line_id, expected_version)
        main = references.main_of(line, lock=True) or references.ensure_main(line, actor=user)
        changed, fields, cleared = set(), set(), None
        try:
            if 'sku' in data:
                sku = clean(str(data['sku'] or ''))
                if not sku or len(sku) > 120:  # the main Reference holds 120, as Crear SKU says
                    raise ValidationError({'sku': 'Escribe el SKU (hasta 120 caracteres).'})
                if sku != line.sku:
                    main = set_codigo(line, main, sku, user)
                    changed.add('sku')
            if 'description' in data:
                description = ' '.join(str(data['description'] or '').split()).upper()
                if description != ' '.join(line.description.split()).upper():
                    line.description = description
                    changed.add('description')
                    fields.add('description')
            if 'part_type' in data:
                part_type = None
                if data['part_type']:
                    from .models import PartType
                    part_type = PartType.objects.filter(pk=data['part_type']).first()
                    if part_type is None:
                        raise ValidationError({'part_type': 'Elige un grupo y subgrupo existentes.'})
                if (part_type.pk if part_type else None) != line.part_type_id:
                    fields.update(lines.set_subgrupo(line, part_type))
                    changed.add('part_type')
            if 'internal_code' in data and data['internal_code'] != (main.tipo == 'interno'):
                if data['internal_code']:
                    was_oem = oem_of(line) is not None
                    main, cleared = make_interno(line, main, user)
                    if was_oem:
                        changed.add('oem')
                else:
                    remembered = ((line.field_sources or {}).get('internal_code') or {}).get('oem') or []
                    main = make_public(line, main, user)
                    if remembered and 'oem_code' not in data:  # not an internal code after all: its OEM numbers come back
                        restore_oem(line, remembered, user)
                        changed.add('oem')
                changed.add('internal_code')
            if 'oem_code' in data or 'oem_brand' in data:
                current = oem_of(line)
                code = clean(str(data.get('oem_code', current.code if current else '') or ''))
                brand = clean(str(data.get('oem_brand', current.brand if current else '') or ''))
                if bool(code) != bool(brand) or len(code) > 120 or len(brand) > 120:
                    raise ValidationError({'oem_code': 'El número OEM y su fabricante van juntos (o ninguno).'})
                if (code, brand) != ((current.code, current.brand) if current else ('', '')):
                    set_oem(line, references.main_of(line), code, brand, user)
                    changed.add('oem')
            if 'references' in data:
                if set_references(line, references.main_of(line), normalize_references(data['references'] or []), user):
                    changed.add('references')
        except references.ReferenceConflict as error:
            if isinstance(error, references.ReferenceHasVariants):
                raise
            raise ValidationError({'detail': error.message}) from error
        except variants.VariantConflict as error:
            raise ValidationError({'detail': error.message}) from error
        if changed:
            mark = {'source': 'admin', 'by': user.username, 'at': timezone.now().isoformat()}
            sources = dict(line.field_sources or {})
            for field in changed:
                sources[field] = dict(mark)
            if cleared:  # the OEM numbers «código interno» took off, which unticking brings back
                sources['internal_code']['oem'] = cleared
            line.field_sources = sources
            line.version, line.changed_at = (line.version or 0) + 1, timezone.now()
            line.save(update_fields=sorted(fields | {'field_sources', 'version', 'changed_at'}))
    return line


def set_codigo(line, main, sku, user):
    """The codigo an administrator gives a line in preparación: the main respelled when the key is the same (its former spelling joins
    printed_forms), else a new main of that code with the main's brand, tipo and supplier (references.set_main; the former main stays
    one of the line's References, as the supplier's printed code its items hang from). Returns the new main."""
    _, Part, Reference, _, _, _ = _models()
    if references.clave(sku) == main.clave:
        Part.objects.filter(pk=line.pk).update(sku=sku)  # the codigo first: references.update respells a main only to its line's codigo
        line.sku = sku
        return references.update(Reference.all_objects.get(pk=main.pk), code=sku, actor=user)
    ref, _ = references.add(line, sku, main.brand, tipo=main.tipo, supplier=main.supplier_id, source=citation(line=line, user=user),
                            actor=user)
    references.set_main(line, ref, user, reason='Productos nuevos: SKU editado')
    line.refresh_from_db()
    return Reference.all_objects.get(pk=ref.pk)


def hung_by_supplier(refs):
    """{supplier id: [item ids]} of the items hanging from these References, the supplier with most items first."""
    SupplierItem = _models()[3]
    found = {}
    for pk, supplier in SupplierItem.objects.filter(reference_id__in=[ref.pk for ref in refs]).order_by('pk').values_list('pk', 'supplier_id'):
        found.setdefault(supplier, []).append(pk)
    return dict(sorted(found.items(), key=lambda pair: (-len(pair[1]), str(pair[0]))))


def make_interno(line, main, user):
    """The main becomes the supplier's own code (interno; «código interno», Revisión IA's internal_code): an interno Reference of the
    same code for each supplier whose items hang from the main (the one with most items becomes the main), the items re-hung, the
    public main retired, and the line's non-main OEM numbers without items retired (an internal code has none). Returns (the new main,
    the OEM numbers taken off as [{code, brand}], which unticking brings back)."""
    Reference = _models()[2]
    hung = hung_by_supplier([main])
    if not hung:
        raise ValidationError({'internal_code': 'Un código interno es de un proveedor, y ningún artículo cuelga de este código.'})
    new_main, made = None, {}
    for supplier, item_ids in hung.items():
        ref, _ = references.add(line, main.code, main.brand, tipo='interno', supplier=supplier, estado='declarado',
                                source=citation(line=line, user=user) if user else '', actor=user)
        variants.hang(item_ids, ref)
        made[supplier] = ref
        new_main = new_main or ref
    references.set_main(line, new_main, user, reason='Productos nuevos: código interno del proveedor')
    references.retire(main, actor=user, replaced_by=new_main, reason='Código interno del proveedor')
    cleared = []
    for ref in Reference.objects.filter(part_id=line.pk, tipo='oem', is_main=False).order_by('pk'):
        if not variants.hanging([ref]).exists():
            cleared.append({'code': ref.code, 'brand': ref.brand})
            references.retire(ref, actor=user, reason='Un código interno no lleva número OEM')
    return Reference.all_objects.get(pk=new_main.pk), cleared


def make_public(line, main, user, tipo=None):
    """Not a supplier's own code after all: a public main of the same code and brand (tipo by its brand: oem for a vehicle make), the
    items of every interno Reference of that key on the line re-hung from it, those retired. Refuses (ReferenceConflict) a key another
    line holds. Returns the new main."""
    Reference = _models()[2]
    internos = list(Reference.all_objects.filter(part_id=line.pk, clave=main.clave, brand=main.brand, tipo='interno',
                                                 estado__in=references.LIVE).order_by('pk'))
    ref, _ = references.add(line, main.code, main.brand, tipo=tipo or public_tipo(main.brand), estado='declarado',
                            source=citation(line=line, user=user) if user else '', actor=user)
    for interno in internos:
        variants.hang(variants.hanging([interno]).values_list('pk', flat=True), ref)
    references.set_main(line, ref, user, reason='Productos nuevos: no es un código interno')
    references.retire(internos, actor=user, replaced_by=ref, reason='No es un código interno')
    return Reference.all_objects.get(pk=ref.pk)


def restore_oem(line, remembered, user):
    for row in remembered:
        if row.get('code') and row.get('brand'):
            set_oem(line, references.main_of(line), row['code'], row['brand'], user)


def set_oem(line, main, code, brand, user):
    """The line's OEM number as an administrator gives it: (code, brand) the main's own key makes the main oem under that maker
    (references.retype_main); another number becomes a non-main oem Reference replacing the ones the line had without items. ('', '')
    takes the OEM number away: the main loses tipo oem (its brand's public tipo, never oem) and the non-main ones go."""
    Reference = _models()[2]
    others = [ref for ref in Reference.objects.filter(part_id=line.pk, tipo='oem', is_main=False).order_by('pk')
              if references.clave(ref.code) != references.clave(code) or ref.brand != references.brand_key(brand)]
    for ref in others:
        if not variants.hanging([ref]).exists():
            references.retire(ref, actor=user, reason='Otro número OEM en Productos nuevos')
    if not code:
        if main.tipo == 'oem':
            fallback = public_tipo(main.brand)
            references.retype_main(line, 'aftermarket' if fallback == 'oem' else fallback, actor=user)
        return
    if references.clave(code) == main.clave:
        if main.tipo == 'interno':
            raise ValidationError({'oem_code': 'Un código interno no lleva número OEM: desmarca «Código interno».'})
        references.retype_main(line, 'oem', brand=brand, actor=user)
        return
    references.add(line, code, brand, tipo='oem', source=citation(line=line, user=user), actor=user)


def set_references(line, main, rows, user):
    """The line's alternos as an administrator lists them (its public non-main References but the OEM number and the codes its items
    hang from, which the items show): new ones are added with an admin source, ones left out are retired, a changed ref_type retypes.
    Returns whether anything changed."""
    Reference, SupplierItem = _models()[2], _models()[3]
    oem = oem_of(line)
    hung = set(SupplierItem.objects.filter(part_id=line.pk).values_list('reference_id', flat=True))
    current = {(ref.clave, ref.brand): ref for ref in Reference.objects.filter(part_id=line.pk, is_main=False).order_by('pk')
               if ref.tipo != 'interno' and (oem is None or ref.pk != oem.pk) and ref.pk not in hung}
    wanted = {(references.clave(row['code']), references.brand_key(row.get('brand', ''))): row for row in rows}
    changed = False
    for key, ref in current.items():
        if key not in wanted:
            references.retire(ref, actor=user, reason='Quitada en Productos nuevos')
            changed = True
    held = {(ref.clave, ref.brand) for ref in Reference.objects.filter(part_id=line.pk).order_by('pk')}
    for key, row in wanted.items():
        tipo = references.tipo_of(row.get('ref_type', 'unknown'))
        ref = current.get(key)
        if ref is None and key in held:  # the line holds it already (its main, its OEM number, an item's code)
            continue
        if ref is None:
            references.add(line, row['code'], row.get('brand', ''), tipo=tipo, source=citation(line=line, user=user), actor=user)
            changed = True
        elif row.get('ref_type') and ref.tipo != tipo:
            references.update(ref, tipo=tipo, actor=user)
            changed = True
    return changed


# --- Revisión IA's fills -------------------------------------------------------------------------------------------------------------

AI_FIELDS = {'part_type': ['part_type', 'category', 'subcategory'], 'description': ['description'], 'internal_code': [], 'oem': []}


def ai_fill(line, fill, *, note, run_id, model):
    """The Revisión IA's fills on a line in preparación the caller holds locked (mall.sku_review): grupo/subgrupo (a pair of the
    taxonomy, its PartType created on first use), a description with obvious typos fixed, and «código interno» (the main becomes the
    supplier's own code, its OEM numbers go). fill maps a field to (value, confidence 0-1[, note]); part_type's value is (category,
    name). A field an administrator set is never touched. The version moves only on a real change, and changed_at does not: the
    storefront reminder is about new products waiting, not about the AI's work on them. Returns {'written': [...], 'kept': [...]}."""
    from .technical_data import part_type_for
    written, kept, at = [], [], timezone.now().isoformat()
    sources = dict(line.field_sources or {})

    def owned(*fields):
        return any((sources.get(field) or {}).get('source') == 'admin' for field in fields)

    def mark(field, confidence, why=None):
        sources[field] = {'source': 'ai', 'confidence': round(float(confidence), 2), 'note': (why if why is not None else note or '')[:300],
                          'at': at, 'run': str(run_id), 'model': model}
        written.append(field)

    def unpack(field):
        value, confidence, *why = fill[field]
        return value, confidence, (why[0] if why else None)

    fields = set()
    if 'part_type' in fill:
        (category, name), confidence, why = unpack('part_type')
        if owned('part_type'):
            kept.append('part_type')
        else:
            part_type = part_type_for(category, name)
            if part_type.pk != line.part_type_id:
                fields.update(lines.set_subgrupo(line, part_type))
                mark('part_type', confidence, why)
    if 'description' in fill:
        value, confidence, why = unpack('description')
        value = ' '.join(str(value or '').split()).upper()
        if owned('description'):
            kept.append('description')
        elif value and value != ' '.join(line.description.split()).upper():
            previous = sources.get('description') or {}
            before = previous.get('from', line.description) if previous.get('source') == 'ai' else line.description
            line.description = value
            fields.add('description')
            mark('description', confidence, why)
            sources['description']['from'] = before   # the supplier's text it corrected: a new one replaces the fix
    if 'internal_code' in fill:
        _, confidence, why = unpack('internal_code')
        main = references.main_of(line)
        if owned('internal_code', 'oem'):
            kept.append('internal_code')
        elif main is not None and main.tipo != 'interno':
            was_oem = oem_of(line) is not None
            try:
                with transaction.atomic():
                    _, cleared = make_interno(line, main, None)
            except (ValidationError, references.ReferenceConflict, variants.VariantConflict):
                kept.append('internal_code')
            else:
                mark('internal_code', confidence, why)
                if cleared:
                    sources['internal_code']['oem'] = cleared
                if was_oem:
                    mark('oem', confidence, why)
    if written:
        line.field_sources = sources
        line.version = (line.version or 0) + 1
        line.save(update_fields=sorted(fields | {'field_sources', 'version'}))
    return {'written': written, 'kept': kept}
