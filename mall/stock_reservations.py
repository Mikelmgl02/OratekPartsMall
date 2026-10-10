"""Stock held by confirmed deals. At the handshake each confirmed quotation line reserves its units on the supplier item, so they leave
its available stock (what quotations check and customers see) while the supplier's reported count stays as the supplier sent it.
A stock upload then asks the supplier, for every confirmed order still holding units of an uploaded item, whether the new count already
discounts it: if it does, its reservation is settled (the count includes the sale); if not, it keeps holding the units.

Lock order, shared with the availability checks: the order row first (the deal action holds it), then supplier items in pk order.
"""
from django.utils import timezone

from .models import StockEntry, StockReservation, SupplierItem


def reserve_confirmed(order, quotation, actor):
    """Reserve the units of every confirmed line with units; returns the reservations made."""
    lines = list(quotation.lines.select_related('order_line').filter(quantity__gt=0).order_by('pk'))
    if not lines:
        return []
    items = {item.pk: item for item in SupplierItem.objects.select_for_update().filter(
        pk__in=sorted({line.order_line.supplier_item_id for line in lines})).order_by('pk')}
    made = []
    for line in lines:
        item = items[line.order_line.supplier_item_id]
        item.reserved_quantity += line.quantity
        item.save(update_fields=['reserved_quantity', 'updated_at'])
        made.append(StockReservation.objects.create(item=item, order=order, quotation_line=line, quantity=line.quantity))
        StockEntry.objects.create(item=item, kind='reserve', quantity=line.quantity, direction='credit', balance_type='reservation',
                                  reserved_delta=line.quantity, reserved_direction='credit', reported_after=item.reported_quantity,
                                  reserved_after=item.reserved_quantity, actor=actor, reference=order.reference)
    return made


def open_reservations(items):
    """{item id: [open reservations with their order]} for these supplier items."""
    held = {}
    for reservation in StockReservation.objects.filter(item__in=list(items), status='open').select_related('order').order_by('created_at', 'pk'):
        held.setdefault(reservation.item_id, []).append(reservation)
    return held


def settle(item, reservations, actor, reference):
    """The supplier's new count already discounts these confirmed orders: their units are part of it now, so they stop holding stock.
    Returns the units settled."""
    held = list(StockReservation.objects.select_for_update().filter(item=item, status='open', pk__in=list(reservations)).order_by('pk'))
    if not held:
        return 0
    units = sum(reservation.quantity for reservation in held)
    item.reserved_quantity = max(0, item.reserved_quantity - units)
    item.save(update_fields=['reserved_quantity', 'updated_at'])
    StockReservation.objects.filter(pk__in=[reservation.pk for reservation in held]).update(
        status='settled', settled_at=timezone.now(), settled_by=actor, settled_reference=reference[:120])
    StockEntry.objects.create(item=item, kind='settle', quantity=units, direction='debit', balance_type='reservation', reserved_delta=units,
                              reserved_direction='debit', reported_after=item.reported_quantity, reserved_after=item.reserved_quantity,
                              actor=actor, reference=reference[:120])
    return units
