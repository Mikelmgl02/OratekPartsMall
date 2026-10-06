from django.db.models import Q
from rest_framework.exceptions import ValidationError


def normalized_label(value):
    return ' '.join((value or '').strip().upper().split())


def bind_part_types(parts, using='default'):
    """Shared by normal saves and batched Excel writes; blank classifications stay untyped."""
    from .models import Part, PartType, TechnicalTemplate, PartSpecification
    if not parts:
        return
    pairs = set()
    for part in parts:
        part.category = normalized_label(part.category)
        part.subcategory = normalized_label(part.subcategory)
        if part.category and part.subcategory:
            pairs.add((part.category, part.subcategory))
    types = PartType.objects.using(using)
    query = Q(pk__in=[])
    for category, name in pairs:
        query |= Q(category=category, name=name)
    found = {(t.category, t.name): t for t in types.filter(query)} if pairs else {}
    missing = pairs - found.keys()
    if missing:
        types.bulk_create([PartType(category=c, name=n) for c, n in sorted(missing)], ignore_conflicts=True)
        found = {(t.category, t.name): t for t in types.filter(query)}
        TechnicalTemplate.objects.using(using).bulk_create([TechnicalTemplate(part_type=t) for t in found.values()], ignore_conflicts=True)
    persisted = [p.pk for p in parts if not p._state.adding]
    old = dict(Part.objects.using(using).filter(pk__in=persisted).values_list('pk', 'part_type_id'))
    changed = [p.pk for p in parts if p.pk in old and old[p.pk] != getattr(found.get((p.category, p.subcategory)), 'pk', None)]
    if changed and PartSpecification.objects.using(using).filter(part_id__in=changed).exists():
        raise ValidationError('Este SKU tiene datos técnicos. Vacía su ficha técnica antes de cambiar el subgrupo para no asignar medidas a otra plantilla.')
    for part in parts:
        part.part_type = found.get((part.category, part.subcategory))
