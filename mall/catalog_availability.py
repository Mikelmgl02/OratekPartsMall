"""Public availability bands, calculated once for the current catalog page."""
from django.conf import settings
from django.db.models import Count, F, Max, Q, Sum, Value
from django.db.models.functions import Greatest
from .models import Account, SupplierItem


def catalog_availability(parts):
    eligible_suppliers = Account.objects.filter(active=True, roles__capability='supplier').values('pk')
    rows = (SupplierItem.objects.filter(part_id__in=[part.pk for part in parts],
                                       matching_status='matched', supplier_id__in=eligible_suppliers)
            .values('part_id').annotate(
                units=Sum(Greatest(F('reported_quantity') - F('reserved_quantity'), Value(0))),
                suppliers=Count('supplier_id', distinct=True, filter=Q(reported_quantity__gt=F('reserved_quantity'))),
                updated_at=Max('updated_at')))
    return {row['part_id']: {
        'status': 'sold_out' if row['units'] == 0 else 'low' if row['units'] <= settings.CATALOG_LOW_STOCK_THRESHOLD else 'high',
        'supplier_count': row['suppliers'],
        'updated_at': row['updated_at'],
    } for row in rows}
