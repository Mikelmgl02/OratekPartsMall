"""Public availability bands, calculated once for the current catalog page."""
from django.conf import settings
from django.db.models import Count, F, Max, Q, Sum, Value
from django.db.models.functions import Greatest
from .models import Account, SupplierItem


def stock_band(units):
    low, high = settings.CATALOG_LOW_STOCK_THRESHOLD, settings.CATALOG_HIGH_STOCK_THRESHOLD
    return 'sold_out' if units == 0 else 'low' if units <= low else 'medium' if units <= high else 'high'


def catalog_availability(parts, own=None):
    """{part id: band, suppliers with stock, last update}; only the own suppliers' items in a supplier's view of the catalog."""
    eligible_suppliers = Account.objects.filter(active=True, roles__capability='supplier')
    if own is not None:
        eligible_suppliers = eligible_suppliers.filter(pk__in=own)
    eligible_suppliers = eligible_suppliers.values('pk')
    rows = (SupplierItem.objects.filter(part_id__in=[part.pk for part in parts],
                                       matching_status='matched', supplier_id__in=eligible_suppliers)
            .values('part_id').annotate(
                units=Sum(Greatest(F('reported_quantity') - F('reserved_quantity'), Value(0))),
                suppliers=Count('supplier_id', distinct=True, filter=Q(reported_quantity__gt=F('reserved_quantity'))),
                updated_at=Max('updated_at')))
    return {row['part_id']: {
        'status': stock_band(row['units']),
        'supplier_count': row['suppliers'],
        'updated_at': row['updated_at'],
    } for row in rows}
