"""The catalog's taxonomy (inventory refactor S5): a Grupo has subgrupos (PartType), and a line (Part) takes one subgrupo. Grupos and
subgrupos are found by name ignoring case and created on first use, a subgrupo with its TechnicalTemplate. From S10 the FK is the only
record: the text columns that followed it until then (PartType.category, Part.category and subcategory) are no longer read or written, and
a line's grupo and subgrupo are its subgrupo's grupo nombre and name (classification(); Part.category and subcategory return them).
Writers that start from text (the catalog import's GRUPO/SUBGRUPO, the bulk edit, the assistant) resolve it here (subgrupos_for,
part_type_for) and give the line its subgrupo with mall.lines.set_subgrupo."""
from django.db.models import Q
from django.db.models.functions import Lower
from rest_framework.exceptions import ValidationError

SHEET_KEEPS_SUBGRUPO = ('Este SKU tiene datos técnicos. Vacía su ficha técnica antes de cambiar el subgrupo para no asignar medidas a otra '
                        'plantilla.')


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
    """A subgrupo's grupo by its FK ('' for no subgrupo). Load it with select_related('grupo') (from a line: 'part_type__grupo')."""
    return part_type.grupo.nombre if part_type is not None and part_type.grupo_id else ''


def project_grupo(grupo, using='default'):
    """After a grupo was saved (Grupo.save, a rename): the Search Documents of its subgrupos' lines read the new nombre
    (mall.lines.project_subgrupos). Returns how many lines are refreshed."""
    from .lines import project_subgrupos
    from .models import PartType
    return project_subgrupos(PartType.objects.using(using).filter(grupo=grupo).select_related('grupo'))


def subgrupos_for(pairs, using='default'):
    """{(grupo, subgrupo): PartType} for pairs of normalized names (pairs with a blank half are left out). Each grupo is found or created
    (grupos_for) and each subgrupo found in it ignoring case; the rest are created, each with its TechnicalTemplate."""
    from .models import PartType, TechnicalTemplate
    pairs = {(category, name) for category, name in pairs if category and name}
    if not pairs:
        return {}
    grupos = grupos_for({category for category, _ in pairs}, using)
    grupo_by_id = {grupo.pk: grupo for grupo in grupos.values()}
    by_grupo = {(grupos[category].pk, name.lower()): (category, name) for category, name in pairs}
    types = PartType.objects.using(using)

    def read():
        query = Q(pk__in=[])
        for category, name in pairs:
            query |= Q(grupo=grupos[category], name_key=name.lower())
        found = {}
        for row in types.alias(name_key=Lower('name')).filter(query):
            row.grupo = grupo_by_id[row.grupo_id]  # the grupos are already at hand: no join
            pair = by_grupo.get((row.grupo_id, row.name.lower()))
            if pair:
                found.setdefault(pair, row)
        return found

    found = read()
    missing = pairs - found.keys()
    if missing:
        types.bulk_create([PartType(grupo=grupos[category], name=name) for category, name in sorted(missing)], ignore_conflicts=True)
        found = read()
        TechnicalTemplate.objects.using(using).bulk_create([TechnicalTemplate(part_type=row) for row in found.values()], ignore_conflicts=True)
    return found


def part_type_for(category, name):
    """The PartType of a grupo/subgrupo pair, its grupo and itself (with its TechnicalTemplate) created on first use."""
    pair = (normalized_label(category), normalized_label(name))
    if not all(pair):
        raise ValueError('Un subgrupo necesita su grupo y su nombre.')
    return subgrupos_for([pair])[pair]


def text_subgrupo(category, subcategory, using='default'):
    """The subgrupo a grupo/subgrupo text names (found or created), None for a blank pair; a pair with only one half refuses
    (ValidationError): from S10 a line has a subgrupo or none, never half a classification."""
    pair = (normalized_label(category), normalized_label(subcategory))
    if not any(pair):
        return None
    if not all(pair):
        raise ValidationError('Indica el grupo y el subgrupo juntos.')
    return subgrupos_for([pair], using)[pair]


def classification(line):
    """(grupo, subgrupo) of a loaded line: its subgrupo's grupo nombre and name, ('', '') with no subgrupo. Load it with
    select_related('part_type__grupo')."""
    if line.part_type_id:
        return grupo_nombre(line.part_type), line.part_type.name
    return '', ''


def refuse_subgrupo_change(lines, using='default'):
    """Refuse (ValidationError) giving another subgrupo to a stored line with a technical sheet: its values belong to its template.
    lines: loaded Parts about to be written with their part_type."""
    from .models import Part, PartSpecification
    stored = [line for line in lines if not line._state.adding and 'part_type_id' in line.__dict__]
    if not stored:
        return
    old = dict(Part.objects.using(using).filter(pk__in=[line.pk for line in stored]).values_list('pk', 'part_type_id'))
    changed = [line.pk for line in stored if line.pk in old and old[line.pk] != line.part_type_id]
    if changed and PartSpecification.objects.using(using).filter(part_id__in=changed).exists():
        raise ValidationError(SHEET_KEEPS_SUBGRUPO)
