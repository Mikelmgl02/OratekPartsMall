"""The catalog's taxonomy (inventory refactor S5): a Grupo has subgrupos (PartType), and a line (Part) takes one subgrupo. Grupos and
subgrupos are found by name ignoring case and created on first use, a subgrupo with its TechnicalTemplate. Until S10 the text columns
follow the FK: PartType.category is its grupo's nombre, and Part.category / subcategory are the line's grupo and subgrupo
(mall.lines.set_subgrupo writes them). A line with no subgrupo keeps the partial classification its text holds (a grupo typed without
a subgrupo yet), which the readers of S5 (classification(), the storefront facets) show until S10 takes the text columns away."""
from django.db.models import Q
from django.db.models.functions import Lower
from rest_framework.exceptions import ValidationError


def normalized_label(value):
    return ' '.join((value or '').strip().upper().split())


def grupos_for(names, using='default'):
    """{normalized name: Grupo} for grupo names, each found ignoring case (UNIQUE Lower(nombre)) or created under its normalized name."""
    from .models import Grupo
    wanted = {normalized_label(name) for name in names} - {''}
    if not wanted:
        return {}
    grupos = Grupo.objects.using(using)

    def read():
        rows = {grupo.nombre.lower(): grupo for grupo in grupos.alias(key=Lower('nombre')).filter(key__in=[n.lower() for n in wanted])}
        return {name: rows[name.lower()] for name in wanted if name.lower() in rows}

    found = read()
    missing = wanted - found.keys()
    if missing:  # a concurrent writer creating the same grupo leaves one row: the conflict is ignored and the row read back
        grupos.bulk_create([Grupo(nombre=name) for name in sorted(missing)], ignore_conflicts=True)
        found = read()
    return found


def grupo_for(name, using='default'):
    """The Grupo of a grupo name, created on first use."""
    nombre = normalized_label(name)
    if not nombre:
        raise ValueError('Un grupo necesita un nombre.')
    return grupos_for([nombre], using)[nombre]


def grupo_nombre(part_type):
    """A subgrupo's grupo by its FK; until 0082 makes the FK required, the text of a subgrupo that has none yet."""
    return part_type.grupo.nombre if part_type.grupo_id else part_type.category


def pair_grupo(part_type, using='default'):
    """Put a subgrupo's grupo and its text in step before it is saved (PartType.save). The FK wins, unless only the text changed since
    the subgrupo was loaded (the code before S5 moves a subgrupo by its text) or there is no grupo yet: then the text names the grupo,
    found or created. The text then becomes that grupo's nombre."""
    loaded_category, loaded_grupo = getattr(part_type, '_loaded_grupo', (None, None))
    text = normalized_label(part_type.category)
    moved = (part_type.grupo_id is not None and part_type.grupo_id == loaded_grupo and loaded_category is not None
             and text.lower() != normalized_label(loaded_category).lower())
    if text and (part_type.grupo_id is None or moved):
        part_type.grupo = grupo_for(text, using)
    if part_type.grupo_id is not None:
        part_type.category = part_type.grupo.nombre
    return part_type


def subgrupos_for(pairs, using='default'):
    """{(grupo, subgrupo): PartType} for pairs of normalized names (pairs with a blank half are left out). Each grupo is found or created
    (grupos_for) and each subgrupo found in it ignoring case; until 0082, a subgrupo with no grupo yet whose text names that grupo is
    found too, and takes its grupo now. The rest are created, each with its TechnicalTemplate."""
    from .models import PartType, TechnicalTemplate
    pairs = {(category, name) for category, name in pairs if category and name}
    if not pairs:
        return {}
    grupos = grupos_for({category for category, _ in pairs}, using)
    grupo_by_id = {grupo.pk: grupo for grupo in grupos.values()}
    by_grupo = {(grupos[category].pk, name.lower()): (category, name) for category, name in pairs}
    by_text = {(category.lower(), name.lower()): (category, name) for category, name in pairs}
    types = PartType.objects.using(using)

    def read():
        query = Q(pk__in=[])
        for category, name in pairs:
            query |= Q(grupo=grupos[category], name_key=name.lower())
            query |= Q(grupo__isnull=True, category_key=category.lower(), name_key=name.lower())
        # No join on the grupo (the models say it is required, and a join would leave out a subgrupo with none yet before 0082): the
        # grupos are already at hand.
        rows = list(types.alias(name_key=Lower('name'), category_key=Lower('category')).filter(query))
        found, orphans = {}, {}
        for row in sorted(rows, key=lambda row: row.grupo_id is None):  # a subgrupo in its grupo before one without a grupo
            if row.grupo_id:
                row.grupo = grupo_by_id[row.grupo_id]
                pair = by_grupo.get((row.grupo_id, row.name.lower()))
                if pair:
                    found.setdefault(pair, row)
            else:
                pair = by_text.get((row.category.lower(), row.name.lower()))
                if pair and pair not in found:
                    found[pair] = orphans[pair] = row
        return found, orphans

    found, orphans = read()
    for pair, row in orphans.items():  # before 0082 only: the subgrupo takes the grupo its text names
        grupo = grupos[pair[0]]
        types.filter(pk=row.pk, grupo__isnull=True).update(grupo=grupo, category=grupo.nombre)
        row.grupo, row.category = grupo, grupo.nombre
        row._loaded_grupo = (row.category, row.grupo_id)
    missing = pairs - found.keys()
    if missing:
        types.bulk_create([PartType(grupo=grupos[category], category=grupos[category].nombre, name=name) for category, name in sorted(missing)],
                          ignore_conflicts=True)
        found, _ = read()
        TechnicalTemplate.objects.using(using).bulk_create([TechnicalTemplate(part_type=row) for row in found.values()], ignore_conflicts=True)
    return found


def part_type_for(category, name):
    """The PartType of a grupo/subgrupo pair, its grupo and itself (with its TechnicalTemplate) created on first use, as bind_part_types
    does on a save."""
    pair = (normalized_label(category), normalized_label(name))
    if not all(pair):
        raise ValueError('Un subgrupo necesita su grupo y su nombre.')
    return subgrupos_for([pair])[pair]


def classification(line):
    """(grupo, subgrupo) of a loaded line as the taxonomy readers take it: its subgrupo's, by the FK (its grupo's nombre and its name),
    or, with no subgrupo, the partial classification its text holds. Load it with select_related('part_type__grupo')."""
    if line.part_type_id:
        return grupo_nombre(line.part_type), line.part_type.name
    return line.category, line.subcategory


def bind_part_types(parts, using='default'):
    """Shared by normal saves and batched Excel writes: each line's grupo/subgrupo text names its subgrupo, found or created
    (subgrupos_for), and mall.lines.set_subgrupo gives it the subgrupo and its text. A line whose text names only half a pair has no
    subgrupo and keeps that text."""
    from .lines import set_subgrupo
    from .models import Part, PartSpecification
    if not parts:
        return
    pairs = set()
    for part in parts:
        part.category = normalized_label(part.category)
        part.subcategory = normalized_label(part.subcategory)
        if part.category and part.subcategory:
            pairs.add((part.category, part.subcategory))
    found = subgrupos_for(pairs, using)
    persisted = [p.pk for p in parts if not p._state.adding]
    old = dict(Part.objects.using(using).filter(pk__in=persisted).values_list('pk', 'part_type_id'))
    changed = [p.pk for p in parts if p.pk in old and old[p.pk] != getattr(found.get((p.category, p.subcategory)), 'pk', None)]
    if changed and PartSpecification.objects.using(using).filter(part_id__in=changed).exists():
        raise ValidationError('Este SKU tiene datos técnicos. Vacía su ficha técnica antes de cambiar el subgrupo para no asignar medidas a otra plantilla.')
    for part in parts:
        set_subgrupo(part, found.get((part.category, part.subcategory)), grupo=part.category, subgrupo=part.subcategory)
