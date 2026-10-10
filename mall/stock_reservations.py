"""Stock held by confirmed deals. At the handshake each confirmed quotation line reserves its units on the supplier item, so they leave
its available stock (what quotations check and customers see) while the supplier's reported count stays as the supplier sent it.
A stock upload then asks the supplier, for every confirmed order still holding units of an uploaded item, whether the new count already
discounts it: if it does, its reservation is settled (the count includes the sale); if not, it keeps holding the units.

Every reservation's units enter the ledger once (a reserve entry) and leave it once (a settle entry), both pointing to the reservation and
naming its order, so mall.check_stock_ledger can match them.

Lock order, shared with the availability checks: the order row first (the deal action holds it), then KEY SHARE on the supplier's and the
client's accounts, then supplier items in pk order. The deal's inserts reference those accounts, and their foreign-key checks take KEY SHARE
at commit; taken up front, before the items, the confirmation never waits for an account while it holds items. Uploads, feeds, matching and
a client's send hold the account (FOR UPDATE) and then wait for items, so waiting the other way round would deadlock.
"""
from django.db import connection
from django.utils import timezone
from rest_framework.exceptions import APIException

from .models import Account, StockEntry, StockReservation, SupplierItem


def reserve_confirmed(order, quotation, actor):
    """Reserve the units of every confirmed line with units; returns the reservations made."""
    lines = list(quotation.lines.select_related('order_line').filter(quantity__gt=0).order_by('pk'))
    if not lines:
        return []
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT id FROM {connection.ops.quote_name(Account._meta.db_table)} WHERE id IN (%s, %s) ORDER BY id FOR KEY SHARE',
                       [order.supplier_id, order.client_id])
    items = {item.pk: item for item in SupplierItem.objects.select_for_update().filter(
        pk__in=sorted({line.order_line.supplier_item_id for line in lines})).order_by('pk')}
    made = []
    for line in lines:
        item = items[line.order_line.supplier_item_id]
        item.reserved_quantity += line.quantity
        item.save(update_fields=['reserved_quantity', 'updated_at'])
        reservation = StockReservation.objects.create(item=item, order=order, quotation_line=line, quantity=line.quantity)
        made.append(reservation)
        StockEntry.objects.create(item=item, reservation=reservation, kind='reserve', quantity=line.quantity, direction='credit',
                                  balance_type='reservation', reserved_delta=line.quantity, reserved_direction='credit',
                                  reported_after=item.reported_quantity, reserved_after=item.reserved_quantity, actor=actor, reference=order.reference)
    return made


def open_reservations(items):
    """{item id: [open reservations with their order]} for these supplier items."""
    held = {}
    for reservation in StockReservation.objects.filter(item__in=list(items), status='open').select_related('order').order_by('created_at', 'pk'):
        held.setdefault(reservation.item_id, []).append(reservation)
    return held


class LedgerMismatch(APIException):
    # 422, not 409: reviewing the file again cannot help; the reserved count needs an administrator (mall.check_stock_ledger).
    status_code = 422
    default_detail = 'Las reservas de este artículo no cuadran con sus pedidos confirmados. No se guardó nada; avisa al administrador.'


def settle(item, reservations, actor, reference):
    """The supplier's new count already discounts these confirmed orders: their units are part of it now, so they stop holding stock.
    One settle entry per order. Returns the units settled; refuses (nothing saved) when the item reserves fewer units than they hold."""
    # Only the reservation rows are locked (of=self): locking their orders too would take an order after its item, against the lock order.
    held = list(StockReservation.objects.select_for_update(of=('self',)).select_related('order').filter(
        item=item, status='open', pk__in=list(reservations)).order_by('created_at', 'pk'))
    if not held:
        return 0
    units = sum(reservation.quantity for reservation in held)
    if item.reserved_quantity < units:
        raise LedgerMismatch(f'Las reservas de {item.codigo} ({item.supplier_invent_id}) no cuadran: el artículo reserva {item.reserved_quantity} '
                             f'unidades y sus pedidos confirmados suman {units}. No se importó este lote; avisa al administrador.')
    for reservation in held:
        item.reserved_quantity -= reservation.quantity
        StockEntry.objects.create(item=item, reservation=reservation, kind='settle', quantity=reservation.quantity, direction='debit',
                                  balance_type='reservation', reserved_delta=reservation.quantity, reserved_direction='debit',
                                  reported_after=item.reported_quantity, reserved_after=item.reserved_quantity, actor=actor,
                                  reference=f'{reservation.order.reference} · {reference}'[:120])
    item.save(update_fields=['reserved_quantity', 'updated_at'])
    StockReservation.objects.filter(pk__in=[reservation.pk for reservation in held]).update(
        status='settled', settled_at=timezone.now(), settled_by=actor, settled_reference=reference[:120])
    return units
