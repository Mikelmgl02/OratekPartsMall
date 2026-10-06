"""Supplier-only stock checks for quotations. Never writes stock and never reaches a client payload.

Lock order (for the future reservation step): the SupplierRequest row first (callers already hold it), then SupplierItem
rows in pk order. Never take the supplier Account lock after the order lock.
"""
from .models import SupplierItem


def line_item_identity_ok(line, supplier_id):
    """The captured order line still points at the same supplier item, part, code and brand, and it is still matched."""
    item = line.supplier_item
    return (item.supplier_id == supplier_id and item.part_id == line.part_id and item.supplier_invent_id == line.supplier_invent_id
            and item.codigo == line.codigo and item.brand == line.brand and item.matching_status == 'matched')


def check_lines(lines_with_qty, *, lock=False):
    """Single entry point: [(order_line, quantity)] -> [{order_line_id, quantity, identity_ok, available, reported, reserved, updated_at}].

    `available` is None when the identity guard fails. v1 reads without a lock; lock=True re-reads the items with
    select_for_update in pk order and is reserved for the stock reservation step.
    """
    pairs = list(lines_with_qty)
    if lock:
        items = {item.pk: item for item in SupplierItem.objects.select_for_update().filter(
            pk__in={line.supplier_item_id for line, _ in pairs}).order_by('pk')}
        for line, _ in pairs:
            line.supplier_item = items[line.supplier_item_id]
    result = []
    for line, quantity in pairs:
        item, ok = line.supplier_item, line_item_identity_ok(line, line.request.supplier_id)
        result.append({'order_line_id': line.pk, 'quantity': quantity, 'identity_ok': ok,
                       'available': item.available_quantity if ok else None, 'reported': item.reported_quantity if ok else None,
                       'reserved': item.reserved_quantity if ok else None, 'updated_at': item.updated_at if ok else None})
    return result
