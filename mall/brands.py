"""Brands (inventory refactor S1): how a spelling names a brand, the familia rule, and the only writers of Brand.nombre.

normalize() is the spelling the table keeps; lookup() finds the brand a spelling names (its nombre or one of its aliases);
familia_root() and same_familia() read the familia (KAIYI and CHERY are one familia: the same code is the same part across it, never
the same brand alone); get_or_create() adds a brand for a new spelling. rename() and merge() are the only code that changes a nombre:
they re-key the codes that carry it (Reference.brand, which stays text until S10) in the same transaction, keep
the old spelling as an alias, and open a merge case when a code would end up twice under one brand. resolve() (every spelling to its
canonical brand, everywhere) came with S6: the matching compares a variant's brand with a Reference's through it.
"""
from django.db import transaction
from django.db.models import Q

from .brand_models import Brand, BrandAlias, normalize_name


class BrandConflict(ValueError):
    """A brand change that would clash with another brand or alias; the message says what to do instead."""


def normalize(name):
    """Upper case, trimmed, single spaces: the spelling Brand.nombre keeps (CENTURY  / HKT → CENTURY / HKT)."""
    return normalize_name(name)


def spellings(name):
    """The forms a spelling is looked up by: as written (trimmed, upper case, as aliases keep whitespace variants) and normalized."""
    written = (name or '').strip().upper()
    return list(dict.fromkeys(form for form in (written, normalize(name)) if form))


def lookup(name):
    """The brand a spelling names, by nombre or alias, or None (also for a blank spelling)."""
    forms = spellings(name)
    if not forms:
        return None
    brand = Brand.objects.filter(nombre__in=forms).select_related('familia').first()
    if brand is None:
        alias = BrandAlias.objects.filter(alias__in=forms).select_related('brand__familia').first()
        brand = alias.brand if alias else None
    return brand


def resolve(name):
    """The canonical spelling of a brand (S6, rules 1 and 2): the nombre of the brand a spelling names, by nombre or alias (TOY →
    TOYOTA, MOTION  MOTORS → MOTION MOTORS), else the spelling normalized; '' for a blank one ('' means any brand)."""
    brand = lookup(name)
    return brand.nombre if brand is not None else normalize(name)


def familia_map(names):
    """{spelling: the nombre heading its familia} for the spellings that name a brand (by nombre or alias): a familia member maps to
    its familia's brand, any other brand to itself. For callers comparing many brands at once."""
    forms = {form: name for name in names for form in spellings(name)}
    roots = {}
    for brand in Brand.objects.filter(nombre__in=forms).select_related('familia'):
        roots[forms[brand.nombre]] = (brand.familia or brand).nombre
    for alias in BrandAlias.objects.filter(alias__in=forms).select_related('brand__familia'):
        roots.setdefault(forms[alias.alias], (alias.brand.familia or alias.brand).nombre)
    return roots


def familia_root(name):
    """The nombre of the brand heading a spelling's familia (KAIYI → CHERY), the brand's own nombre outside any familia, or the
    normalized spelling when no brand has it ('' stays '')."""
    brand = lookup(name)
    return (brand.familia or brand).nombre if brand else normalize(name)


def same_familia(a, b):
    """Whether two spellings name one brand or two brands of one familia (KAIYI and CHERY, TOY and LEXUS). A blank spelling matches
    nothing: '' means any brand, which is a different rule (the brandless reservation)."""
    if not normalize(a) or not normalize(b):
        return False
    return familia_root(a) == familia_root(b)


def get_or_create(name, *, tipo=''):
    """(brand, created) for a spelling: the brand it names (nombre or alias), else a new brand under the normalized spelling, its tipo
    filled when blank. (None, False) for a blank spelling."""
    nombre = normalize(name)
    if not nombre:
        return None, False
    if tipo and tipo not in dict(Brand.TIPOS):
        raise BrandConflict(f'Tipo de marca desconocido: {tipo}.')
    brand = lookup(nombre)
    created = False
    if brand is None:
        brand, created = Brand.objects.get_or_create(nombre=nombre, defaults={'tipo': tipo})
    if tipo and not brand.tipo:
        brand.tipo = tipo
        brand.save(update_fields=['tipo', 'updated_at'])
    return brand, created


@transaction.atomic
def set_familia(brand, familia):
    """Put a brand under a familia (or take it out with None). One level: the familia's brand heads no familia itself and the
    brand heads none."""
    brand = Brand.objects.select_for_update().get(pk=brand.pk)
    if familia is not None:
        familia = Brand.objects.select_for_update().get(pk=familia.pk)
        if familia.pk == brand.pk:
            raise BrandConflict('Una marca no puede ser su propia familia.')
        if familia.familia_id:
            raise BrandConflict(f'{familia.nombre} ya pertenece a la familia {familia.familia.nombre}: elige esa.')
        if brand.miembros.exists():
            raise BrandConflict(f'{brand.nombre} encabeza una familia: no puede pertenecer a otra.')
    brand.familia = familia
    brand.save(update_fields=['familia', 'updated_at'])
    return brand


def rekey(old, new, *, reason):
    """Move the codes that carry the spelling `old` to `new` (Reference.brand), retired ones too, so every brand a code carries stays
    a brand or an alias. A code already held under `new` stays where it is: when both are live and on two lines it opens a merge case
    (the two lines may be one part, MatchingCase kind catalog_reference, reviewed in Coincidencias), otherwise it is only listed. OEM
    alternos are saved one by one so the OEM library mirror follows them; the rest move in one UPDATE. Returns {'moved', 'kept':
    [{id, code, part}], 'cases': [case ids]}."""
    from .matching_queue import enqueue_matching
    from .models import Reference
    rows = list(Reference.all_objects.select_for_update(of=('self',)).filter(brand=old).order_by('pk'))  # the codes only: lines stay unlocked
    if not rows:
        return {'moved': 0, 'kept': [], 'cases': []}
    held = list(Reference.all_objects.select_for_update(of=('self',)).filter(brand=new)
                .filter(Q(code__in=[r.code for r in rows]) | Q(clave__in=[r.clave for r in rows])).order_by('pk'))
    by_code = {row.code: row for row in held}  # the printed code under `new`, whatever its estado (unique_brand_code until 0074)
    by_key = {row.clave: row for row in held if row.estado in Reference.EXCLUSIVE}  # its key, held by a live one
    moving, kept, cases = [], [], []
    for row in rows:
        other = by_code.get(row.code) or (by_key.get(row.clave) if row.estado in Reference.EXCLUSIVE else None)
        if other is None:
            moving.append(row)
            continue
        kept.append({'id': row.pk, 'code': row.code, 'part': str(row.part_id)})
        if other.part_id != row.part_id and 'retirada' not in (row.estado, other.estado):
            cases.append(open_merge_case(row, other, old, new, reason))
    Reference.all_objects.filter(pk__in=[row.pk for row in moving if row.ref_type != 'oem']).update(brand=new)
    for row in moving:
        if row.ref_type == 'oem':
            row.brand = new
            row.save(update_fields=['brand'])
    from .search_document import refresh
    refresh({row.part_id for row in moving})  # the brands in their lines' Search Documents (S7)
    enqueue_matching()  # codes are matched by (code, brand): the matching index reads the new spelling
    return {'moved': len(moving), 'kept': kept, 'cases': cases}


def open_merge_case(code, other, old, new, reason):
    """A catalog_reference case asking whether the line holding `code` (brand `old`) is the line holding the same code under `new`."""
    from .matching_engine import part_row, save_case
    from .models import Part
    source, target = (Part.objects.prefetch_related('codes').get(pk=pk) for pk in (code.part_id, other.part_id))
    case = save_case(key=f'brand-merge:{code.pk}:{new}'[:240], kind='catalog_reference', sources=[part_row(source)],
                     candidates=[part_row(target)], target=target.sku, method='catalog_reference',
                     reason=f'{reason}: el código {code.code} queda como {old} en {source.sku} y como {new} en {target.sku}. '
                            'Si son el mismo repuesto, agrúpalos; si no, mantenlos separados.')
    return str(case.pk)


@transaction.atomic
def rename(brand, new_name):
    """Give a brand a new nombre: the codes that carry the old one take it in the same transaction (rekey) and the old spelling stays
    as an alias. A nombre another brand has is a merge (merge()); an alias of another brand is refused. Returns
    {'brand', 'moved', 'kept', 'cases'}."""
    brand = Brand.objects.select_for_update().get(pk=brand.pk)
    new, old = normalize(new_name), brand.nombre
    if not new:
        raise BrandConflict('Escribe el nombre nuevo de la marca.')
    if new == old:
        return {'brand': brand, 'moved': 0, 'kept': [], 'cases': []}
    if Brand.objects.filter(nombre=new).exclude(pk=brand.pk).exists():
        raise BrandConflict(f'Ya existe la marca {new}: une las dos marcas en vez de renombrar.')
    alias = BrandAlias.objects.select_for_update().filter(alias__in=spellings(new_name)).first()
    if alias is not None and alias.brand_id != brand.pk:
        raise BrandConflict(f'{new} es un alias de {alias.brand.nombre}: une las dos marcas en vez de renombrar.')
    if alias is not None:
        alias.delete()  # the spelling becomes the nombre
    moved = rekey(old, new, reason=f'La marca {old} pasó a llamarse {new}')
    brand.nombre = new
    brand.save(update_fields=['nombre', 'updated_at'])
    BrandAlias.objects.get_or_create(alias=old, defaults={'brand': brand})
    from .search_document import refresh_text_containing
    refresh_text_containing([old, *brand.aliases.values_list('alias', flat=True)])  # the brand their texts expand to (S7)
    return {'brand': brand, **moved}


@transaction.atomic
def merge(source, target):
    """Fold `source` into `target`: the codes that carry source's nombre take target's (rekey, collisions open merge cases), source's
    nombre and aliases become target's aliases, its familia members join target's familia, a blank tipo or notes of target take
    source's, and source goes. Returns {'brand', 'moved', 'kept', 'cases'}."""
    first, second = sorted([source.pk, target.pk])
    locked = Brand.objects.select_for_update().in_bulk([first, second])
    source, target = locked.get(source.pk), locked.get(target.pk)
    if source is None or target is None:
        raise BrandConflict('La marca ya no existe.')
    if source.pk == target.pk:
        raise BrandConflict('Elige dos marcas distintas para unirlas.')
    moved = rekey(source.nombre, target.nombre, reason=f'La marca {source.nombre} se unió a {target.nombre}')
    if target.familia_id == source.pk:  # target was in source's familia: it heads what is left of it
        target.familia = None
    head = target.familia or target
    Brand.objects.filter(familia=source).exclude(pk=head.pk).update(familia=head)
    BrandAlias.objects.filter(brand=source).update(brand=target)
    target.tipo = target.tipo or source.tipo
    target.notas = '\n'.join(note for note in (target.notas, source.notas) if note.strip())
    target.save(update_fields=['familia', 'tipo', 'notas', 'updated_at'])
    nombre = source.nombre
    source.delete()
    BrandAlias.objects.get_or_create(alias=nombre, defaults={'brand': target})
    from .search_document import refresh_text_containing
    refresh_text_containing([nombre, *target.aliases.values_list('alias', flat=True)])  # the brand their texts expand to (S7)
    return {'brand': target, **moved}


@transaction.atomic
def add_alias(brand, spelling):
    """Another spelling of a brand (management/brands/<id>/aliases, S7): trimmed and upper case as aliases keep it. Refused (BrandConflict)
    when it is a brand's nombre (merge the two brands instead) or another brand's alias. The codes that carry the spelling take the
    brand's nombre (rekey, collisions open merge cases), so from S6 no Reference carries an alias (I12); the Search Documents whose text
    holds the word expand it to the brand (the alias's signal: mall.search_document.alias_changed). Returns (alias, the rekey's result)."""
    brand = Brand.objects.select_for_update().get(pk=brand.pk)
    written = (spelling or '').strip().upper()
    if not written:
        raise BrandConflict('Escribe el alias.')
    if len(written) > 120:
        raise BrandConflict('El alias no puede superar 120 caracteres.')
    other = Brand.objects.filter(nombre__in=spellings(written)).first()
    if other is not None:
        raise BrandConflict(f'{other.nombre} es una marca: une las dos marcas en vez de crear el alias.')
    existing = BrandAlias.objects.select_for_update().filter(alias=written).select_related('brand').first()
    if existing is not None:
        if existing.brand_id == brand.pk:
            return existing, {'moved': 0, 'kept': [], 'cases': []}
        raise BrandConflict(f'{written} ya es un alias de {existing.brand.nombre}.')
    alias = BrandAlias.objects.create(alias=written, brand=brand)
    moved = rekey(written, brand.nombre, reason=f'{written} es un alias de {brand.nombre}')
    return alias, moved


@transaction.atomic
def remove_alias(alias):
    """Take a spelling off its brand (the codes already re-keyed keep the brand's nombre). The Search Documents whose text expanded it
    follow (the alias's signal: mall.search_document.alias_changed). Returns the alias's spelling."""
    alias = BrandAlias.objects.select_for_update().get(pk=alias.pk)
    spelling = alias.alias
    alias.delete()
    return spelling
