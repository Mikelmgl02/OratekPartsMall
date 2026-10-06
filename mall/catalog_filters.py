"""Catalog filters and counts across all canonical SKUs, before pagination."""
from uuid import UUID

from django.conf import settings
from django.db.models import Count, F, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Greatest
from rest_framework.exceptions import ValidationError

from .models import Account, SupplierItem


AVAILABILITY_LABELS = {
    'in_stock': 'Con existencias', 'high': 'Altas existencias', 'low': 'Bajas existencias',
    'sold_out': 'Agotado', 'unknown': 'Sin existencias reportadas',
}


def eligible_items():
    suppliers = Account.objects.filter(active=True, roles__capability='supplier').values('pk')
    return SupplierItem.objects.filter(matching_status='matched', supplier_id__in=suppliers)


def filter_values(params):
    values = {key: list(dict.fromkeys(params.getlist(key))) for key in ('category', 'subcategory', 'supplier')}
    if any(len(items) > 100 or any(len(value) > 120 for value in items) for items in values.values()):
        raise ValidationError('Hay demasiados filtros o un valor no es válido.')
    try:
        values['supplier'] = [UUID(value) for value in values['supplier']]
    except (ValueError, AttributeError):
        raise ValidationError('El filtro de proveedor no es válido.')
    values['availability'] = params.get('availability', '')
    if values['availability'] and values['availability'] not in AVAILABILITY_LABELS:
        raise ValidationError('El filtro de existencias no es válido.')
    return values


def with_stock_units(query):
    totals = (eligible_items().filter(part_id=OuterRef('pk')).order_by().values('part_id')
              .annotate(units=Sum(Greatest(F('reported_quantity') - F('reserved_quantity'), Value(0))))
              .values('units'))
    return query.annotate(_stock_units=Subquery(totals, output_field=IntegerField()))


def apply_filters(query, values, omit=None):
    for key in ('category', 'subcategory'):
        if key != omit and values[key]:
            query = query.filter(**{f'{key}__in': values[key]})
    if omit != 'supplier' and values['supplier']:
        query = query.filter(pk__in=eligible_items().filter(supplier_id__in=values['supplier']).values('part_id'))
    band = values['availability'] if omit != 'availability' else ''
    if band:
        query = with_stock_units(query)
        if band == 'unknown':
            query = query.filter(_stock_units__isnull=True)
        elif band == 'sold_out':
            query = query.filter(_stock_units=0)
        elif band == 'in_stock':
            query = query.filter(_stock_units__gt=0)
        elif band == 'low':
            query = query.filter(_stock_units__gt=0, _stock_units__lte=settings.CATALOG_LOW_STOCK_THRESHOLD)
        else:
            query = query.filter(_stock_units__gt=settings.CATALOG_LOW_STOCK_THRESHOLD)
    return query


def catalog_facets(base, values):
    facets = {}
    for field, blank_label in [('category', 'Sin clasificar'), ('subcategory', 'Sin subgrupo')]:
        # Keep all groups available; subgroups follow the chosen groups.
        scope_values = {**values, 'subcategory': []} if field == 'category' else values
        scope = apply_filters(base, scope_values, omit=field).order_by()
        rows = scope.values(field).annotate(count=Count('pk', distinct=True)).order_by(field)
        facets[field] = [{'value': row[field], 'label': row[field] or blank_label, 'count': row['count']} for row in rows]
    stock_scope = with_stock_units(apply_filters(base, values, omit='availability')).order_by()
    counts = stock_scope.aggregate(
        in_stock=Count('pk', distinct=True, filter=Q(_stock_units__gt=0)),
        high=Count('pk', distinct=True, filter=Q(_stock_units__gt=settings.CATALOG_LOW_STOCK_THRESHOLD)),
        low=Count('pk', distinct=True, filter=Q(_stock_units__gt=0, _stock_units__lte=settings.CATALOG_LOW_STOCK_THRESHOLD)),
        sold_out=Count('pk', distinct=True, filter=Q(_stock_units=0)),
        unknown=Count('pk', distinct=True, filter=Q(_stock_units__isnull=True)),
    )
    facets['availability'] = [{'value': value, 'label': label, 'count': counts[value]} for value, label in AVAILABILITY_LABELS.items()]
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
