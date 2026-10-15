"""Catalog filters and counts across all canonical SKUs, before pagination. In a supplier's own view (values['own'], see
mall.catalog_scope) the stock and its bands come from that supplier's items alone, and there is no supplier filter or facet.

The grupo and subgrupo filters and facets keep their text parameters (category, subcategory) but read each line's subgrupo by its FK
(inventory refactor S5): its grupo's nombre and its name. A line with no subgrupo counts by the partial classification its text holds
(mall.technical_data), until S10."""
from uuid import UUID

from django.conf import settings
from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from rest_framework.exceptions import ValidationError

from .models import Account, SupplierItem
from .catalog_technical_filters import apply_technical_filter, technical_filter


AVAILABILITY_LABELS = {
    'in_stock': 'Con existencias', 'high': 'Altas existencias', 'medium': 'Existencias medias', 'low': 'Bajas existencias',
    'sold_out': 'Agotado', 'unknown': 'Sin existencias reportadas',
}


def stock_band_q(band):
    low, high = settings.CATALOG_LOW_STOCK_THRESHOLD, settings.CATALOG_HIGH_STOCK_THRESHOLD
    return {'unknown': Q(_stock_units__isnull=True), 'sold_out': Q(_stock_units=0), 'in_stock': Q(_stock_units__gt=0),
            'low': Q(_stock_units__gt=0, _stock_units__lte=low), 'medium': Q(_stock_units__gt=low, _stock_units__lte=high),
            'high': Q(_stock_units__gt=high)}[band]


def eligible_items(own=None):
    suppliers = Account.objects.filter(active=True, roles__capability='supplier')
    if own is not None:
        suppliers = suppliers.filter(pk__in=own)
    return SupplierItem.objects.filter(matching_status='matched', supplier_id__in=suppliers.values('pk'))


def filter_values(params, own=None):
    values = {key: list(dict.fromkeys(params.getlist(key))) for key in ('category', 'subcategory', 'supplier')}
    if any(len(items) > 100 or any(len(value) > 120 for value in items) for items in values.values()):
        raise ValidationError('Hay demasiados filtros o un valor no es válido.')
    try:
        values['supplier'] = [UUID(value) for value in values['supplier']]
    except (ValueError, AttributeError):
        raise ValidationError('El filtro de proveedor no es válido.')
    values.update(variant_filter_values(params))
    values['availability'] = params.get('availability', '')
    if values['availability'] and values['availability'] not in AVAILABILITY_LABELS:
        raise ValidationError('El filtro de existencias no es válido.')
    values['own'] = own
    values['technical'] = technical_filter(params.get('technical', ''))
    if own is not None:
        values['supplier'] = []
    return values


def with_stock_units(query, own=None):
    totals = (eligible_items(own).filter(part_id=OuterRef('pk')).order_by().values('part_id')
              .annotate(units=Sum(Greatest(F('reported_quantity') - F('reserved_quantity'), Value(0))))
              .values('units'))
    return query.annotate(_stock_units=Subquery(totals, output_field=IntegerField()))


# What a line is filtered and counted by: its subgrupo's grupo and name (the FK), else its text (a partial classification, until S10).
TAXONOMY = {'category': lambda: Coalesce(F('part_type__grupo__nombre'), F('category')),
            'subcategory': lambda: Coalesce(F('part_type__name'), F('subcategory'))}


def apply_filters(query, values, omit=None):
    for key, expression in TAXONOMY.items():
        if key != omit and values[key]:
            query = query.alias(**{f'_{key}': expression()}).filter(**{f'_{key}__in': values[key]})
    if omit != 'supplier' and values['supplier']:
        query = query.filter(pk__in=eligible_items().filter(supplier_id__in=values['supplier']).values('part_id'))
    query = apply_variant_filters(query, values, omit)
    band = values['availability'] if omit != 'availability' else ''
    if band:
        query = with_stock_units(query, values.get('own')).filter(stock_band_q(band))
    return apply_technical_filter(query, values.get('technical'))


def catalog_facets(base, values):
    facets = {}
    for field, blank_label in [('category', 'Sin clasificar'), ('subcategory', 'Sin subgrupo')]:
        # Keep all groups available; subgroups follow the chosen groups.
        scope_values = {**values, 'subcategory': []} if field == 'category' else values
        scope = apply_filters(base, scope_values, omit=field).order_by()
        rows = scope.annotate(_value=TAXONOMY[field]()).values('_value').annotate(count=Count('pk', distinct=True)).order_by('_value')
        facets[field] = [{'value': row['_value'], 'label': row['_value'] or blank_label, 'count': row['count']} for row in rows]
    stock_scope = with_stock_units(apply_filters(base, values, omit='availability'), values.get('own')).order_by()
    counts = stock_scope.aggregate(**{value: Count('pk', distinct=True, filter=stock_band_q(value)) for value in AVAILABILITY_LABELS})
    facets['availability'] = [{'value': value, 'label': label, 'count': counts[value]} for value, label in AVAILABILITY_LABELS.items()]
    facets.update(variant_facets(base, values))
    if values.get('own') is not None:
        facets['supplier'] = []
        return facets
    supplier_scope = apply_filters(base, values, omit='supplier').order_by().values('pk')
    rows = (eligible_items().filter(part_id__in=supplier_scope)
            .values('supplier_id', 'supplier__name').annotate(count=Count('part_id', distinct=True)).order_by('supplier__name', 'supplier_id'))
    facets['supplier'] = [{'value': str(row['supplier_id']), 'label': row['supplier__name'], 'count': row['count']} for row in rows]
    present = {row['value'] for row in facets['supplier']}
    missing = [value for value in values['supplier'] if str(value) not in present]
    if missing:
        selected = Account.objects.filter(pk__in=missing, active=True, roles__capability='supplier').distinct()
        facets['supplier'].extend({'value': str(supplier.pk), 'label': supplier.name, 'count': 0} for supplier in selected)
        facets['supplier'].sort(key=lambda option: (option['label'], option['value']))
    return facets


# --- Marca and Calidad (inventory refactor S7) -----------------------------------------------------------------------------------------
# brand: the line's main Reference's brand (its maker), any spelling of it (mall.brands.resolve); calidad: a matched item of the line
# (from a live supplier, or the own ones in a supplier's view) of that calidad. Both read the catalog live, whatever
# CATALOG_SEARCH_SOURCE says. Their facets list the brands with lines (the most lines first, BRAND_FACET_LIMIT at most) and the three
# calidades.
CALIDAD_LABELS = dict(SupplierItem.CALIDADES)
BRAND_FACET_LIMIT = 50


def variant_filter_values(params):
    from .brands import resolve
    brands = [value for value in dict.fromkeys(params.getlist('brand')) if value.strip()]
    calidades = list(dict.fromkeys(params.getlist('calidad')))
    if len(brands) > 100 or any(len(value) > 120 for value in brands):
        raise ValidationError('El filtro de marca no es válido.')
    if any(value not in CALIDAD_LABELS for value in calidades):
        raise ValidationError('El filtro de calidad no es válido: usa original, oes o alternativo.')
    return {'brand': list(dict.fromkeys(resolve(value) for value in brands)), 'calidad': calidades}


def main_brand_lines(brands):
    from .models import Reference
    return Reference.objects.filter(is_main=True, brand__in=brands).values('part_id')


def apply_variant_filters(query, values, omit=None):
    if omit != 'brand' and values.get('brand'):
        query = query.filter(pk__in=main_brand_lines(values['brand']))
    if omit != 'calidad' and values.get('calidad'):
        query = query.filter(pk__in=eligible_items(values.get('own')).filter(calidad__in=values['calidad']).values('part_id'))
    return query


def variant_facets(base, values):
    from .models import Reference
    scope = apply_filters(base, values, omit='brand').order_by().values('pk')
    rows = (Reference.objects.filter(is_main=True, part_id__in=scope).exclude(brand='').values('brand')
            .annotate(count=Count('part_id', distinct=True)).order_by('-count', 'brand')[:BRAND_FACET_LIMIT])
    brands = [{'value': row['brand'], 'label': row['brand'], 'count': row['count']} for row in rows]
    present = {row['value'] for row in brands}
    brands += [{'value': value, 'label': value, 'count': 0} for value in values.get('brand', []) if value not in present]
    scope = apply_filters(base, values, omit='calidad').order_by().values('pk')
    counts = dict(eligible_items(values.get('own')).filter(part_id__in=scope).exclude(calidad='').values('calidad')
                  .annotate(count=Count('part_id', distinct=True)).values_list('calidad', 'count'))
    return {'brand': brands, 'calidad': [{'value': value, 'label': label, 'count': counts.get(value, 0)} for value, label in CALIDAD_LABELS.items()]}
