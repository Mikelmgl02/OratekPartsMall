"""Live catalog identity for OEM research; never persisted with the web-research cache."""
from . import references
from .models import Part


def equivalents(rows):
    from .oem_lookup import safe_url
    return [row for row in rows if row.get('relationship') == 'equivalent' and safe_url(row.get('source_url'))]


def owners_for(row):
    brand = references.brand_name(row['brand'])
    matches = list(references.conflicts(row['code'], brand, line=None).select_related('part').order_by('pk'))
    owners = {ref.part_id: ref.part for ref in matches}
    # Also respect the visible SKU reservation, including a different brand that needs human review.
    for part in Part.objects.filter(sku__iexact=references.code_key(row['code']), estado__in=('publicada', 'retirada')):
        owners[part.pk] = part
    result = []
    for part in sorted(owners.values(), key=lambda part: str(part.pk)):
        main = references.main_of(part)
        result.append({'id': str(part.pk), 'sku': part.sku, 'description': part.description, 'estado': part.estado,
                       'main': {'code': main.code, 'brand': main.brand, 'tipo': main.tipo, 'estado': main.estado} if main else None,
                       'references': [{'id': ref.pk, 'code': ref.code, 'brand': ref.brand, 'tipo': ref.tipo, 'is_main': ref.is_main}
                                      for ref in matches if ref.part_id == part.pk]})
    return result


def enrich(result):
    rows = [{**row, 'catalog_matches': owners_for(row)} for row in result.get('references', [])]
    owners = {owner['id'] for row in equivalents(rows) for owner in row['catalog_matches']}
    return {**result, 'references': rows, 'catalog_conflict': len(owners) > 1}


def resolve(line, rows):
    from .line_views import Conflict
    found = [{**row, 'catalog_matches': owners_for(row)} for row in equivalents(rows)]
    ids = {owner['id'] for row in found for owner in row['catalog_matches']}
    if len(ids) > 1:
        names = sorted({owner['sku'] for row in found for owner in row['catalog_matches']})
        raise Conflict(f'Las equivalencias OEM apuntan a fichas distintas: {", ".join(names)}. Revisa Agrupar SKU; no se aplicó ningún cambio.')
    target = Part.objects.get(pk=next(iter(ids))) if ids else line
    return target, found
