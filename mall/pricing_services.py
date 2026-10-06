"""Every supplier price write goes through set_prices(): compare-and-set, all-or-nothing, append-only history.

Lock order: the supplier Account first (the same global writer order as ingest_inventory and the stock import), then the touched
PriceList rows, then entries and item-pricing rows in item pk order. SupplierItem is never locked or saved, so its updated_at, the
catalog's availability.updated_at and staged stock-import fingerprints never move, and prices never touch InventoryUpdate, StockEntry
or the matching queue.
"""
from collections import Counter

from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Account, SupplierItem
from .pricing_models import PriceChange, PriceList, PriceListEntry, SupplierItemPricing, record_pricing_event

FOREIGN_ITEM = 'El artículo no pertenece a tu inventario.'
ITEM_FIELDS = ('discount_group', 'floor_price')


class PriceConflict(Exception):
    """Another writer changed some rows: nothing was written; conflicts carry each row's current value and revision."""
    def __init__(self, conflicts):
        super().__init__('price conflict')
        self.conflicts = conflicts


def amount(value):
    return None if value is None else f'{value:.2f}'


def item_values(row):
    return {'discount_group': row.discount_group if row else '', 'floor_price': row.floor_price if row else None}


@transaction.atomic
def set_prices(supplier, actor, *, changes=(), items=(), source='manual', reference='', audit_kind='prices_edited'):
    """changes: [{list_id, supplier_item_id, unit_price: Decimal|None (None removes), expected_revision: int|None}];
    items: [{supplier_item_id, discount_group?, floor_price?, expected_revision: int|None}] with validated, normalized values.

    A row already at its target is a no-op, so a retry after a lost response is safe without an operation table. Any other revision
    mismatch raises PriceConflict and nothing is written. Each real change bumps that row's revision and appends a PriceChange; each
    touched list's version moves once per call, and one PricingAuditEvent summarises the call. Returns {summary, item_ids}.
    """
    Account.objects.select_for_update().get(pk=supplier.pk)
    list_ids = sorted({change['list_id'] for change in changes}, key=str)
    lists = {row.pk: row for row in PriceList.objects.select_for_update().filter(supplier=supplier, pk__in=list_ids).order_by('pk')}
    if len(lists) != len(list_ids):
        raise ValidationError('Una de las listas de precios no existe.')
    item_ids = {change['supplier_item_id'] for change in changes} | {row['supplier_item_id'] for row in items}
    if set(SupplierItem.objects.filter(supplier=supplier, pk__in=item_ids).values_list('pk', flat=True)) != item_ids:
        raise ValidationError(FOREIGN_ITEM)
    entries = {(entry.price_list_id, entry.item_id): entry for entry in PriceListEntry.objects.select_for_update().filter(
        price_list__in=list(lists.values()), item_id__in=item_ids).order_by('item_id', 'price_list_id')}
    pricing = {row.item_id: row for row in SupplierItemPricing.objects.select_for_update().filter(
        supplier=supplier, item_id__in=item_ids).order_by('item_id')}
    conflicts, price_plan, item_plan, unchanged = [], [], [], 0
    for change in changes:
        entry = entries.get((change['list_id'], change['supplier_item_id']))
        if (entry.unit_price if entry else None) == change['unit_price']:
            unchanged += 1
        elif change['expected_revision'] != (entry.revision if entry else None):
            conflicts.append({'supplier_item_id': str(change['supplier_item_id']), 'list_id': str(change['list_id']),
                              'current': {'unit_price': amount(entry.unit_price) if entry else None, 'revision': entry.revision if entry else None}})
        else:
            price_plan.append((change, entry))
    for row in items:
        current = pricing.get(row['supplier_item_id'])
        before = item_values(current)
        diff = {field: row[field] for field in ITEM_FIELDS if field in row and before[field] != row[field]}
        if not diff:
            unchanged += 1
        elif row['expected_revision'] != (current.revision if current else None):
            conflicts.append({'supplier_item_id': str(row['supplier_item_id']), 'list_id': None,
                              'current': {'discount_group': before['discount_group'], 'floor_price': amount(before['floor_price']),
                                          'revision': current.revision if current else None}})
        else:
            item_plan.append((row, current, diff))
    if conflicts:
        raise PriceConflict(conflicts)
    now, history, summary = timezone.now(), [], Counter(unchanged=unchanged)
    stamp = {'source': source, 'reference': reference[:120], 'updated_by': actor, 'updated_at': now}
    created_keys = [(change['list_id'], change['supplier_item_id']) for change, entry in price_plan if entry is None and change['unit_price'] is not None]
    # A re-created entry continues the pair's revision count, so a stale expected_revision can never match a row deleted meanwhile.
    previous = {(row['price_list_id'], row['item_id']): row['total'] for row in PriceChange.objects.filter(
        kind='list_price', price_list_id__in={key[0] for key in created_keys}, item_id__in={key[1] for key in created_keys}).values(
        'price_list_id', 'item_id').annotate(total=Count('id'))} if created_keys else {}
    creates, updates, removals = [], [], []
    for change, entry in price_plan:
        key, new, old = (change['list_id'], change['supplier_item_id']), change['unit_price'], entry.unit_price if entry else None
        if entry is None:
            creates.append(PriceListEntry(price_list_id=key[0], item_id=key[1], unit_price=new, revision=previous.get(key, 0) + 1, **stamp))
            summary['created'] += 1
        elif new is None:
            removals.append(entry.pk)
            summary['removed'] += 1
        else:
            entry.unit_price, entry.revision = new, entry.revision + 1
            for field, value in stamp.items():
                setattr(entry, field, value)
            updates.append(entry)
            summary['updated'] += 1
        summary[f'list:{lists[key[0]].code}'] += 1
        history.append(PriceChange(supplier=supplier, item_id=key[1], price_list_id=key[0], kind='list_price', old_value=old, new_value=new,
                                   source=source, reference=reference[:120], actor=actor))
    PriceListEntry.objects.bulk_create(creates)
    PriceListEntry.objects.bulk_update(updates, ['unit_price', 'revision', 'source', 'reference', 'updated_by', 'updated_at'])
    PriceListEntry.objects.filter(pk__in=removals).delete()
    item_creates, item_updates = [], []
    for row, current, diff in item_plan:
        before = item_values(current)
        if current is None:
            current = SupplierItemPricing(item_id=row['supplier_item_id'], supplier=supplier, revision=1, updated_by=actor, updated_at=now)
            item_creates.append(current)
        else:
            current.revision, current.updated_by, current.updated_at = current.revision + 1, actor, now
            item_updates.append(current)
        for field, value in diff.items():
            setattr(current, field, value)
        if 'floor_price' in diff:
            summary['floor_updates'] += 1
            history.append(PriceChange(supplier=supplier, item_id=row['supplier_item_id'], kind='floor_price', old_value=before['floor_price'],
                                       new_value=diff['floor_price'], source=source, reference=reference[:120], actor=actor))
        if 'discount_group' in diff:
            summary['line_updates'] += 1
            history.append(PriceChange(supplier=supplier, item_id=row['supplier_item_id'], kind='discount_group', old_text=before['discount_group'],
                                       new_text=diff['discount_group'], source=source, reference=reference[:120], actor=actor))
    SupplierItemPricing.objects.bulk_create(item_creates)
    SupplierItemPricing.objects.bulk_update(item_updates, ['discount_group', 'floor_price', 'revision', 'updated_by', 'updated_at'])
    PriceChange.objects.bulk_create(history)
    touched = {change['list_id'] for change, _ in price_plan}
    PriceList.objects.filter(pk__in=touched).update(version=F('version') + 1, updated_at=now)
    result = {key: summary[key] for key in ('created', 'updated', 'removed', 'unchanged', 'floor_updates', 'line_updates')}
    if history:
        record_pricing_event(supplier, actor, audit_kind, object_id=reference[:40], payload={
            **result, 'reference': reference, 'lists': {key[5:]: value for key, value in summary.items() if key.startswith('list:')}})
    return {'summary': result, 'item_ids': item_ids}
