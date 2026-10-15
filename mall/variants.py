"""Variants (inventory refactor S3): a supplier's item (SupplierItem) hangs from one Reference of its line, and its part is always that
Reference's line. This module is the only writer of SupplierItem.reference and part:

    attach(item, ref, status)           hang the item from a Reference (its part follows; I4), and say whether it counts in stock
    vincular(item, line, status)        Vincular: the line's Reference for the item's code (found or created), a waiting code applied
                                        only by an approval (status matched)
    detach(item)                        no line, no Reference, pending (a waiting code becomes its code)
    reference_for(line, code, brand, supplier, create)   the Reference of a line a variant with this code hangs from
    choose(rows, line_sku, code, brand, supplier)        that choice, on rows already read (the S3 backfill's dry run uses it too)
    outcome(item, data, match), carry_out(item, plan)    what a load does to an item: services.ingest_inventory and the Excel preview
    apply_pending(item)                 the code waiting in codigo_pendiente/brand_pendiente becomes the item's code
    follow(refs)                        the variants of References that changed line go with them (0078's composite key does it too)
    rehang(refs, to)                    the variants of some References onto another (a merge's absorb)
    hang(items, to)                     some variants onto a Reference (Separar: the line that holds their key)
    hanging(refs)                       the variants hanging from References (mall.references.retire refuses to retire those)
    settle_status(line_ids)             matched variants a move left on a line outside the catalog become pending (follow, rehang, hang)
    recompute_status(line)              a line that entered the catalog: its pending variants with no open case become matched; review
                                        is left alone
    follow_item(item, save kwargs)      SupplierItem.save's transition shim (until S10)

Which Reference (rules 1 and 2 on the variant's own line). The item's key is (clave of its code, its brand; brands compared with
their spaces collapsed, same_brand). On the line's live
References: the same key first (the supplier's own interno code before a public one; the main before the others); a brandless item
joins a branded Reference of its clave (rule 2: GRP sends no brand, the line's main says TOYOTA); a branded item joins the brandless
Reference of its clave (a code with no brand is every brand's). With nothing of that clave on the line, a new declarado Reference is
made, with a proveedor source, interno: scoped to the supplier, hidden from every reader but its own items (a code reviewed onto a
line is the supplier's own name for it, not a universal alias: B19). The S3 backfill alone (choose's 'nueva', public=True) makes it
public when the code is the line's own clave under another brand, or its clave once trailing auto-eligible TAGs are stripped is on the
line (rule 3: 43512-16070-RAZ on 43512-16070), unless that clashes with another line's key; from S6 the engine decides that
(mall.reference_matching: its loads and a person's decisions on its cases).

Code changes (services.ingest_inventory, the one path of Excel, the API and apiag). An item not attached takes the new code. For an
attached item whose code or brand changes:
    the same clave and the brand unchanged or blank → X     applied on its own: the codigo changes and the item re-attaches to (clave,
                                                           X) on its line, created declarado (proveedor source) when missing
    mall.references.find places the new key on its line    applied on its own (OD5)
    anything else                                          codigo_pendiente/brand_pendiente wait, status review (out of stock: OD7);
                                                           codigo and brand stay, so order and cesta identity checks hold
Without the first rule the 23,358 brandless GRP items would go offline the day GRP sends brands.

matching_status: matched counts in its line's stock (a line in the catalog: publicada or retirada); review is attached but out of stock
(a code change waiting); pending has no Reference yet, or its line is not in the catalog. Lock order (plan §1.6.5): the supplier's
Account → Parts → References → SupplierItems.
"""
import copy
from collections import namedtuple

from django.db import transaction
from django.db.models import Exists, OuterRef, Subquery
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import references

CATALOG = ('publicada', 'retirada')  # lines whose variants count in stock (mall.lines.CATALOG)
HOW = {'principal': 'su código principal', 'exacta': 'otra referencia con su código y marca',
       'marca_de_la_linea': 'la referencia con marca de su código (el artículo no trae marca)',
       'sin_marca': 'la referencia sin marca de su código', 'interna': 'su código interno', 'nueva': 'una referencia nueva',
       'nueva_interna': 'un código interno nuevo'}
PLAN_RULES = ('directo', 'igual', 'misma_clave', 'misma_linea', 'pendiente')


class VariantConflict(ValidationError):
    """A variant this write cannot place: the Reference is retired, belongs to another supplier, or its line was merged. A DRF
    ValidationError (400) with the message."""

    @property
    def message(self):
        return ' '.join(str(item) for item in self.detail) if isinstance(self.detail, list) else str(self.detail)


def _models():
    from .models import Part, Reference, SupplierItem
    return Part, Reference, SupplierItem


def _ids(refs):
    if refs is None:
        return []
    if hasattr(refs, 'values_list'):
        return list(refs.values_list('pk', flat=True))
    return [getattr(ref, 'pk', ref) for ref in (refs if isinstance(refs, (list, tuple, set)) else [refs])]


def in_catalog(line):
    return line is not None and not line.merged_into_id and line.estado in CATALOG


def status_for(line, wanted=None):
    """The status of a variant attached to `line`: matched only on a line in the catalog (pending elsewhere); review when asked."""
    if wanted == 'review':
        return 'review'
    if wanted == 'pending':
        return 'pending'
    return 'matched' if in_catalog(line) else 'pending'


def declared_by(item):
    """The reference_source text of a Reference a supplier's item brings: a proveedor source (plan A3, 'Declarado por')."""
    name = getattr(getattr(item, 'supplier', None), 'name', '') or ''
    return f'Declarado por {name} · artículo {item.supplier_invent_id}'.strip()[:500]


# --- which Reference -----------------------------------------------------------------------------------------------------------------

def tag_base(code, table=None):
    """The code once its trailing TAGs are stripped while each is auto-eligible (rule 3), or None when none is: 43512-16070-RAZ →
    43512-16070; a position or size token (-L, -RH: D4) is never a TAG, so it stops the strip."""
    from . import catalog_suffixes
    try:
        base, chain, _ = catalog_suffixes.strip_tags(code, table=table)
    except Exception:  # a code the suffix parser cannot read has no TAG to strip
        return None
    stripped = 0
    for link in reversed(chain):
        entry = link['entry']
        if entry.cls != 'TAG' or not entry.auto_eligible:
            break
        stripped += 1
    if not stripped:
        return None
    kept = chain[:len(chain) - stripped]
    return base + ''.join(f"{link['sep']}{link['tok']}" for link in kept)


def _get(row, name, default=None):
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def same_brand(a, b):
    """Two brand spellings the same brand: compared upper case with single spaces (MOTION  MOTORS is MOTION MOTORS: S1 seeded the
    whitespace variants as aliases); other aliases (TOY → TOYOTA) count from S6."""
    return ' '.join((a or '').upper().split()) == ' '.join((b or '').upper().split())


def choose(rows, line_sku, code, brand, supplier_id, *, table=None):
    """(row, how) of the line's live References (rows: Reference objects or dicts with pk, clave, brand, supplier_id, is_main,
    estado) a variant with this code and brand hangs from, how being a HOW key; or (None, 'nueva' | 'nueva_interna'): nothing on the
    line takes it, and the Reference to make is public or interno (the module's docstring)."""
    key, brand = references.clave(references.code_key(code)), references.brand_key(brand)
    live = [row for row in rows if _get(row, 'estado') in references.LIVE]
    usable = [row for row in live if _get(row, 'clave') == key and _get(row, 'supplier_id') in (None, supplier_id)]

    def rank(row):
        return (_get(row, 'supplier_id') is None, not _get(row, 'is_main'), references.ESTADO_RANK.get(_get(row, 'estado'), 9),
                _get(row, 'pk') or 0)
    exact = sorted((row for row in usable if same_brand(_get(row, 'brand'), brand)), key=rank)
    if exact:
        row = exact[0]
        return row, 'interna' if _get(row, 'supplier_id') else 'principal' if _get(row, 'is_main') else 'exacta'
    if not brand and usable:
        return sorted(usable, key=rank)[0], 'marca_de_la_linea'
    blank = sorted((row for row in usable if not _get(row, 'brand')), key=rank)
    if blank:
        return blank[0], 'sin_marca'
    public = [row for row in live if _get(row, 'supplier_id') is None]
    claves = {references.clave(references.code_key(line_sku))} | {_get(row, 'clave') for row in public}
    if key in claves:
        return None, 'nueva'
    base = tag_base(references.code_key(code), table=table)
    if base and references.clave(base) in claves:
        return None, 'nueva'
    return None, 'nueva_interna'


def new_reference_tipo(brand):
    """The tipo of a public Reference a supplier's code makes: aftermarket under a brand of parts (Brand.tipo repuesto), otro
    otherwise. Never oem: a supplier's word does not make an OEM number (rule 5b; the OEM library mirror would file it)."""
    from .brands import lookup
    found = lookup(brand) if brand else None
    return 'aftermarket' if found is not None and found.tipo == 'repuesto' else 'otro'


def reference_for(line, code, brand, supplier, *, create=False, actor=None, source='', exact=False, public=False, item=None):
    """(Reference, how) of `line` a variant with this code and brand hangs from (choose), or (None, how) when none does and create is
    False. create: the missing Reference is made declarado with `source` (a proveedor source): interno for the supplier, or public
    where choose says so when `public` (the S3 backfill's B19 rules; a person or the matching linking an item never publishes its code:
    only catalog management approves a universal code until S6), and interno too when a public one would clash with another line's
    key. A supplier's interno code held by another line with no other variant goes with `item` (Vincular moves it). exact: only the
    (clave, brand) Reference itself, or a new public one (a same-key code change re-attaches to its own brand: the module's
    docstring); (None, None) when another line holds it."""
    Part, Reference, _ = _models()
    supplier_id = getattr(supplier, 'pk', supplier)
    rows = list(Reference.all_objects.filter(part_id=line.pk, estado__in=references.LIVE).order_by('pk'))  # interno codes too
    if exact:
        key, wanted = references.clave(references.code_key(code)), references.brand_key(brand)
        found = sorted((row for row in rows if row.clave == key and same_brand(row.brand, wanted) and row.supplier_id in (None, supplier_id)
                        and row.estado in references.LIVE),
                       key=lambda row: (row.supplier_id is None, not row.is_main, row.pk))
        if found:
            return found[0], 'interna' if found[0].supplier_id else 'principal' if found[0].is_main else 'exacta'
        how = 'nueva'
    else:
        row, how = choose(rows, line.sku, code, brand, supplier_id)
        if row is not None:
            return row, how
        if how == 'nueva' and not public:
            how = 'nueva_interna'
    if not create:
        return None, how
    if not references.is_live(line):
        raise VariantConflict(f'El SKU {line.sku} se agrupó en otro: vincula el artículo al SKU en el que se agrupó.')
    if how == 'nueva':
        try:
            with transaction.atomic():
                ref, added = references.add(line, code, brand, tipo=new_reference_tipo(references.brand_key(brand)), estado='declarado',
                                            source=source, actor=actor, detail={TAG: stamp('creada', item)})
            return tagged(ref, added, item, actor), how
        except references.ReferenceConflict:
            if exact:
                return None, None
            how = 'nueva_interna'
    try:
        with transaction.atomic():
            ref, added = references.add(line, code, brand, tipo='interno', supplier=supplier_id, estado='declarado', source=source,
                                        actor=actor, detail={TAG: stamp('creada', item)})
        return tagged(ref, added, item, actor), how
    except references.ReferenceConflict as error:
        held = error.reference
        if (held is None or held.tipo != 'interno' or held.supplier_id != supplier_id or item is None or held.part_id == line.pk
                or hanging([held]).exclude(pk=item.pk).exists()):
            return None, how
        # The supplier's own code for its only item, on the line the item leaves: it goes along (its variants follow it).
        held = Reference.all_objects.get(pk=held.pk)
        if held.is_main:
            # S6: the main of the line in preparación made for this item alone (Productos nuevos, rule 4): that line joins `line`
            # (mall.line_merge.merge_lines, Separar undoes it), its code with it.
            moved = absorb_own_line(held, line, item, actor)
            return (moved, 'interna') if moved is not None else (None, how)
        moved = references.update(held, part=line, actor=actor,
                                  detail={**(held.detail or {}), TAG: {**stamp('movida', item), 'desde': str(held.part_id)}})
        return moved, 'interna'


def absorb_own_line(main, line, item, actor=None):
    """The line in preparación whose main is `main` (an interno code of the item's supplier) merged into `line` when it holds nothing but
    this item (S6: Vincular of a new product's only variant); the main comes along as one of line's References. Returns it, or None
    when the line holds more than the item or is not in preparación."""
    Part, Reference, SupplierItem = _models()
    own = Part._base_manager.filter(pk=main.part_id).first()
    if own is None or own.estado != 'preparacion' or own.merged_into_id or own.pk == line.pk:
        return None
    if SupplierItem.objects.filter(part_id=own.pk).exclude(pk=item.pk).exists():
        return None
    from .line_merge import merge_lines, service_actor
    merge_lines(line, [own], actor=actor if getattr(actor, 'pk', None) else service_actor(), reason='Vincular un producto nuevo')
    return Reference.all_objects.filter(pk=main.pk, part_id=line.pk).exclude(estado='retirada').first()


# What the S3 code makes or brings back (detail['s3']), so python -m mall.refactor.s3_variants --rollback finds it outside its manifest:
# the code before S3 reads every interno code as a plain alterno, so none it made may outlive a rollback.
TAG = 's3'


def stamp(how, item=None):
    return {'como': how, 'articulo': str(item.pk) if getattr(item, 'pk', None) else None, 'at': timezone.now().isoformat()}


def tagged(ref, added, item, actor=None):
    """ref as references.add returned it: a row it brought back from retirada (added, without this module's tag) gets the tag too."""
    if added and TAG not in (ref.detail or {}):
        ref = references.update(ref, detail={**(ref.detail or {}), TAG: stamp('revivida', item)}, actor=actor)
    return ref



# --- attach ----------------------------------------------------------------------------------------------------------------------------

def attach(item, ref, *, status=None, save=True):
    """Hang the item from `ref` (a live Reference): its part becomes ref's line (I4) and its status is `status` (matched only on a line
    in the catalog, pending elsewhere; review when asked) or, when None, what its line gives. An interno Reference takes only its
    own supplier's items (I7); a unificada line takes none. save: written with update_fields (no shim work)."""
    Part, Reference, _ = _models()
    if not isinstance(ref, Reference):
        ref = Reference.all_objects.get(pk=ref)
    if ref.estado == 'retirada':
        raise VariantConflict(f'La referencia {ref} está retirada: elige otra del SKU.')
    if ref.tipo == 'interno' and ref.supplier_id != item.supplier_id:
        raise VariantConflict(f'{ref.code} es un código interno de otro proveedor.')
    line = ref.part
    if not references.is_live(line):
        raise VariantConflict(f'El SKU {line.sku} se agrupó en otro: vincula el artículo al SKU en el que se agrupó.')
    item.reference, item.part = ref, line
    item.matching_status = status_for(line, status)
    item._attached = True  # SupplierItem.save's shim has nothing to do
    if save:
        item.save(update_fields=['reference', 'part', 'matching_status', 'codigo', 'brand', 'codigo_pendiente', 'brand_pendiente',
                                 'updated_at'])
    return item


def waiting(item):
    """Whether a code change waits for review on the item (codigo_pendiente or brand_pendiente)."""
    return item.codigo_pendiente is not None or item.brand_pendiente is not None


def apply_pending(item):
    """The code waiting for review becomes the item's code (not saved). Returns whether one waited."""
    if not waiting(item):
        return False
    item.codigo = item.codigo_pendiente or item.codigo
    item.brand = item.brand if item.brand_pendiente is None else item.brand_pendiente
    item.codigo_pendiente = item.brand_pendiente = None
    return True


def vincular(item, line, *, status='matched', actor=None, save=True, approve=None):
    """Vincular: the item hangs from `line`'s Reference for its code, found or created (reference_for), as a person or an approved case
    decided. approve (by default: status matched, an approval) applies a waiting code first (rule 5b: only review makes a supplier's
    changed code a Reference); otherwise the item hangs by the code it has and the waiting code keeps waiting (a merge carrying it, a
    person who leaves it in review). Refuses (VariantConflict) a unificada line or a code no Reference can take."""
    if not references.is_live(line):
        raise VariantConflict(f'El SKU {line.sku} se agrupó en otro: vincula el artículo al SKU en el que se agrupó.')
    if approve is None:
        approve = status == 'matched'
    if approve:
        apply_pending(item)
    elif waiting(item) and status == 'matched':
        status = 'review'  # a code change still waits: out of stock (OD7)
    ref, _ = reference_for(line, item.codigo, item.brand, item.supplier_id, create=True, actor=actor, source=declared_by(item), item=item)
    if ref is None:
        raise VariantConflict(f'El código {item.codigo} es un código interno de este proveedor en otro SKU: corrígelo allí antes de vincularlo aquí.')
    return attach(item, ref, status=status, save=save)


def detach(item, *, save=True):
    """No line and no Reference: pending (a waiting code becomes its code, since no order line holds its identity on a line)."""
    apply_pending(item)
    item.reference, item.part, item.matching_status = None, None, 'pending'
    item._attached = True
    if save:
        item.save(update_fields=['reference', 'part', 'matching_status', 'codigo', 'brand', 'codigo_pendiente', 'brand_pendiente',
                                 'updated_at'])
    return item


def ensure_attached(item, *, actor=None):
    """An item on a line with no Reference yet (written before S3's backfill, or by a queryset update), or hanging from a Reference
    that was retired or sits on another line: attach it to its line's Reference for its code, its status kept. Returns whether it
    was attached."""
    Part, Reference, _ = _models()
    if not item.part_id:
        return False
    if item.reference_id:
        held = Reference.all_objects.select_related('part').filter(pk=item.reference_id).first()
        if held and held.estado != 'retirada' and held.part_id == item.part_id:
            item.reference = held  # one read: a load's outcome() and attach() use it and its line from here
            return False
    line = Part._base_manager.get(pk=item.part_id)
    if not references.is_live(line):
        return False
    ref, _ = reference_for(line, item.codigo, item.brand, item.supplier_id, create=True, actor=actor, source=declared_by(item), item=item)
    if ref is None:
        return False
    attach(item, ref, status=item.matching_status if item.matching_status in ('review', 'pending') else None, save=True)
    return True


# --- moves -----------------------------------------------------------------------------------------------------------------------------

def follow(refs):
    """The variants of these References onto each Reference's line (one UPDATE, no signals). From 0078 the composite foreign key
    moves them with the Reference already (ON UPDATE CASCADE), so this changes nothing then; before it, it keeps I4. Returns how many
    rows it wrote."""
    _, Reference, SupplierItem = _models()
    ids = _ids(refs)
    if not ids:
        return 0
    line = Subquery(Reference.all_objects.filter(pk=OuterRef('reference_id')).values('part_id')[:1])
    moved = SupplierItem.objects.filter(reference_id__in=ids).update(part_id=line)
    settle_status(Reference.all_objects.filter(pk__in=ids).values_list('part_id', flat=True))
    return moved


def rehang(refs, to):
    """The variants hanging from `refs` onto Reference `to` and its line (a merge's absorb: the same key on the line it joins), their
    status kept. Returns their ids."""
    _, Reference, SupplierItem = _models()
    ids = _ids(refs)
    if not ids:
        return []
    if not isinstance(to, Reference):
        to = Reference.all_objects.get(pk=to)
    moved = list(SupplierItem.objects.filter(reference_id__in=ids).exclude(reference_id=to.pk).order_by('pk').values_list('pk', 'supplier_id'))
    if to.tipo == 'interno' and any(supplier != to.supplier_id for _, supplier in moved):
        raise VariantConflict(f'{to.code} es un código interno de otro proveedor.')
    if moved:
        SupplierItem.objects.filter(pk__in=[pk for pk, _ in moved]).update(reference_id=to.pk, part_id=to.part_id, updated_at=timezone.now())
        settle_status([to.part_id])
    return [str(pk) for pk, _ in moved]


def hang(items, to):
    """Supplier items (ids or rows) onto Reference `to` and its line, their status kept (Separar: a variant assigned to the line that
    holds its key). Refuses another supplier's interno code. Returns how many."""
    _, Reference, SupplierItem = _models()
    if not isinstance(to, Reference):
        to = Reference.all_objects.get(pk=to)
    rows = SupplierItem.objects.filter(pk__in=_ids(items))
    if to.tipo == 'interno' and rows.exclude(supplier_id=to.supplier_id).exists():
        raise VariantConflict(f'{to.code} es un código interno de otro proveedor.')
    count = rows.update(reference_id=to.pk, part_id=to.part_id, updated_at=timezone.now())
    settle_status([to.part_id])
    return count


def hanging(refs):
    """The variants hanging from these References (a queryset)."""
    _, _, SupplierItem = _models()
    return SupplierItem.objects.filter(reference_id__in=_ids(refs))


def settle_status(line_ids):
    """Matched variants that a move left on a line outside the catalog (a Reference taken to a line in referencia or preparación, S4 and
    S6) leave its stock: pending. review stays review; nothing is promoted here (recompute_status does that when a line enters the
    catalog). Returns how many changed."""
    from django.db.models import Q
    Part, _, SupplierItem = _models()
    ids = {pk for pk in line_ids if pk is not None}
    if not ids:
        return 0
    outside = Part._base_manager.filter(pk__in=ids).exclude(Q(estado__in=CATALOG) & Q(merged_into__isnull=True))
    return (SupplierItem.objects.filter(part__in=outside, matching_status='matched')
            .update(matching_status='pending', updated_at=timezone.now()))


def recompute_status(line):
    """The status of the line's attached variants (a line that entered or left the catalog: S6's publish): matched on a line in the
    catalog for the pending ones with no open review case, pending on any other line; review is left alone (a code change waiting).
    Returns how many changed."""
    from .matching_models import MatchingCase
    _, _, SupplierItem = _models()
    if not in_catalog(line):
        return settle_status([line.pk])
    # A case about the item's placement holds it (a variant or code change, S6; a legacy supplier case); a declared code's does not.
    open_case = MatchingCase.objects.filter(item_id=OuterRef('pk'), status='review').exclude(kind__in=('reference', 'merge'))
    return (SupplierItem.objects.filter(part_id=line.pk, reference__isnull=False, matching_status='pending', codigo_pendiente__isnull=True,
                                        brand_pendiente__isnull=True).exclude(Exists(open_case))
            .update(matching_status='matched', updated_at=timezone.now()))


# --- a load's outcome ------------------------------------------------------------------------------------------------------------------

Plan = namedtuple('Plan', 'part_id status codigo brand pending reference rule')


def status_after(item, data, line):
    """The status of an attached item whose code change applies on its own: review when its declared references or its description
    changed, when it contradicts its line, or when it already waited in review for another reason; otherwise its line's."""
    from .matching_engine import description, spec_conflict
    declared = data.get('references', item.references)
    text = data.get('description', item.description)
    if item.references != declared or declared:
        return 'review'  # declared references are resolved together after the load (initial_match)
    if description(item.description) != description(text) or spec_conflict(text, line.description):
        return 'review'
    if item.matching_status == 'review' and item.codigo_pendiente is None and item.brand_pendiente is None:
        return 'review'
    return status_for(line)


def outcome(item, data, match):
    """What a load does to an item (Plan: part_id, status, codigo, brand, pending (codigo, brand) or (None, None), the Reference when
    known, and its rule), read-only, so the Excel preview shows what the import will do. item None: a new item. match: the line
    services.matching_part found for the new code. A new or unattached item follows initial_match; an attached one keeps its line,
    and a code change applies on its own or waits (the module's docstring)."""
    from .services import initial_match
    codigo, brand = data['codigo'], data['brand']
    if item is None or item.reference_id is None:
        part_id, status = initial_match(item, data, match)
        return Plan(part_id, status, codigo, brand, (None, None), None, 'directo')
    if (codigo, brand) == (item.codigo, item.brand):
        part_id, status = initial_match(item, data, match)
        return Plan(part_id, status, codigo, brand, (None, None), item.reference, 'igual')
    if (codigo, brand) == (item.codigo_pendiente or item.codigo, item.brand if item.brand_pendiente is None else item.brand_pendiente):
        return Plan(item.part_id, 'review', item.codigo, item.brand, (item.codigo_pendiente, item.brand_pendiente), item.reference, 'pendiente')
    line = item.part
    if references.clave(codigo) == references.clave(item.codigo) and (brand == item.brand or not item.brand):
        if brand == item.brand:
            return Plan(item.part_id, status_after(item, data, line), codigo, brand, (None, None), item.reference, 'misma_clave')
        found, _ = reference_for(line, codigo, brand, item.supplier_id, exact=True)
        free = found is not None or not references.conflicts(codigo, brand, line=line.pk, estado='declarado').exclude(part_id=line.pk).exists()
        if free:
            return Plan(item.part_id, status_after(item, data, line), codigo, brand, (None, None), found, 'misma_clave')
    found = references.find(codigo, brand, supplier=item.supplier_id)
    if found is not None and found.part_id == item.part_id:
        return Plan(item.part_id, status_after(item, data, line), codigo, brand, (None, None), found, 'misma_linea')
    return Plan(item.part_id, 'review', item.codigo, item.brand, (codigo, brand), item.reference, 'pendiente')


def carry_out(item, plan, *, actor=None):
    """Write a Plan on the item (not saved: the caller saves it whole): its code, a waiting code, its Reference (found or created) and
    status. A Reference that cannot be found or made leaves the item unattached and pending (the matching worker looks again); a
    same-key change whose key another line took meanwhile waits instead."""
    Part, _, _ = _models()
    old = (item.codigo, item.brand)
    item.codigo, item.brand = plan.codigo, plan.brand
    item.codigo_pendiente, item.brand_pendiente = plan.pending
    item._attached = True
    if plan.rule == 'pendiente':
        item.matching_status = 'review'
        return item
    if plan.part_id is None:
        item.reference, item.part, item.matching_status = None, None, 'pending'
        return item
    ref = plan.reference
    if ref is None or ref.part_id != plan.part_id:
        line = Part._base_manager.get(pk=plan.part_id)
        exact = plan.rule == 'misma_clave'
        ref, _ = reference_for(line, item.codigo, item.brand, item.supplier_id, create=True, actor=actor, source=declared_by(item),
                               exact=exact, item=item)
        if ref is None and exact and item.reference_id is not None:
            item.codigo_pendiente, item.brand_pendiente = plan.codigo, plan.brand
            item.codigo, item.brand = old
            item.matching_status = 'review'
            return item
    if ref is None:
        item.reference, item.part, item.matching_status = None, None, 'pending'
        return item
    if plan.rule == 'misma_clave' and item.codigo != ref.code:
        references.add_spelling(ref, item.codigo)  # another printed spelling of the same key
    attach(item, ref, status=plan.status, save=False)
    return item


# --- the save() shim -------------------------------------------------------------------------------------------------------------------

HUNG = {'part', 'part_id', 'reference', 'reference_id'}


def follow_item(item, kwargs):
    """SupplierItem.save's transition shim (until S10), before the write: code that sets part without a Reference (old code, tests,
    the admin) gets the line's Reference for the item's code attached (created declarado when missing: reference_for); a part changed
    under an unchanged Reference is a Vincular onto the new line; a Reference set by code moves part to its line; part cleared
    clears the Reference (and leaves the item pending). This module's writers set item._attached, so their saves are left alone."""
    values = item.__dict__
    if values.pop('_attached', False) or kwargs.get('using') not in (None, 'default'):
        return
    fields = kwargs.get('update_fields')
    if fields is not None and not (HUNG & set(fields)):
        return
    part_id, ref_id = values.get('part_id'), values.get('reference_id')
    loaded_part, loaded_ref = (None, None) if item._state.adding else getattr(item, '_hung', (None, None))
    written = set()
    if part_id is None:
        if ref_id is not None:
            item.reference = None
            written.add('reference')
        if item.matching_status != 'pending' and (ref_id is not None or loaded_part is not None):
            item.matching_status = 'pending'
            written.add('matching_status')
    elif ref_id is not None and ref_id != loaded_ref:
        Part, Reference, _ = _models()
        line_id = Reference.all_objects.filter(pk=ref_id).values_list('part_id', flat=True).first()
        if line_id is not None and line_id != part_id:
            item.part_id = line_id
            written.add('part')
    elif ref_id is None or part_id != loaded_part:
        Part, _, _ = _models()
        line = Part._base_manager.get(pk=part_id)
        if not references.is_live(line):
            raise VariantConflict(f'El SKU {line.sku} se agrupó en otro: vincula el artículo al SKU en el que se agrupó.')
        ref, _ = reference_for(line, item.codigo, item.brand, item.supplier_id, create=True, source=declared_by(item), item=item)
        if ref is None:
            raise VariantConflict(f'El código {item.codigo} es un código interno de este proveedor en otro SKU.')
        item.reference = ref
        written.add('reference')
    if written and fields is not None:
        kwargs['update_fields'] = set(fields) | written


def pending_view(item):
    """The item as the matching engine before S6 resolves it: its waiting code when one waits (codigo_pendiente), else itself."""
    if item.codigo_pendiente is None and item.brand_pendiente is None:
        return item
    view = copy.copy(item)
    view.codigo = item.codigo_pendiente or item.codigo
    view.brand = item.brand if item.brand_pendiente is None else item.brand_pendiente
    return view
