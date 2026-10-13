"""Stock held by quotations and confirmed deals. When the supplier sends a quotation, each line with units reserves them on the supplier
item, so they leave its available stock (what the supplier's other quotations check and customers see) while the supplier's reported count
stays as the supplier sent it. The hold lasts the supplier's quote_hold_days while the client decides: a new version replaces it, a
rejection releases it, and once it lapses (expire_due) the units are free again; the client's confirmation keeps it, without expiry, or
reserves again what had lapsed. A stock upload then asks the supplier, for every confirmed order still holding units of an uploaded item,
whether the new count already discounts it: if it does, its reservation is settled (the count includes the sale); if not, it keeps holding
the units. Quotations not yet confirmed are never asked: nothing was sold yet.

Every reservation's units enter the ledger once (a reserve entry) and leave it through entries that point to it and name its order: a
settle (the supplier's count includes the sale), a shortfall (units the supplier never had leave the count and the reservation together, so
available stock does not move) or a release (a cancelled deal frees what it still held). mall.check_stock_ledger matches them.

Lock order, shared with the availability checks: the order row first (the deal action holds it), then KEY SHARE on the supplier's and the
client's accounts, then supplier items in pk order. The deal's inserts reference those accounts, and their foreign-key checks take KEY SHARE
at commit; taken up front, before the items, the confirmation never waits for an account while it holds items. Uploads, feeds, matching and
a client's send hold the account (FOR UPDATE) and then wait for items, so waiting the other way round would deadlock.
"""
from django.db import connection
from django.utils import timezone
from rest_framework.exceptions import APIException

from .models import Account, StockEntry, StockReservation, SupplierItem, SupplierItemBranchStock


def share_accounts(order):
    """KEY SHARE on the deal's supplier and client accounts, taken before any item lock (see the lock order above)."""
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT id FROM {connection.ops.quote_name(Account._meta.db_table)} WHERE id IN (%s, %s) ORDER BY id FOR KEY SHARE',
                       [order.supplier_id, order.client_id])


def held_by(order, lines=None):
    """The deal's open reservations (optionally of some quotation lines) with their items, locked in the lock order: accounts, items in
    pk order, then the reservation rows only."""
    reservations = StockReservation.objects.filter(order=order, status='open')
    if lines is not None:
        reservations = reservations.filter(quotation_line_id__in=list(lines))
    item_ids = sorted(set(reservations.values_list('item_id', flat=True)))
    if not item_ids:
        return {}, []
    share_accounts(order)
    items = {item.pk: item for item in SupplierItem.objects.select_for_update().filter(pk__in=item_ids).order_by('pk')}
    held = list(StockReservation.objects.select_for_update(of=('self',)).filter(pk__in=reservations.values('pk')).order_by('created_at', 'pk'))
    return items, held


def hold(order, quotation, actor, *, expires_at=None):
    """The order's stock follows `quotation`, its current revision: what another revision held is released (a new version replaces it),
    its lines with units keep their open reservation and the ones without one reserve now, every line offered in full (a line confirmed
    above stock holds what was promised). expires_at: until when a quotation the client has not confirmed holds its units; None once
    confirmed. Returns (units reserved now, units held in all)."""
    lines = list(quotation.lines.select_related('order_line').filter(quantity__gt=0).order_by('pk'))
    current = StockReservation.objects.filter(order=order, status='open')
    item_ids = sorted({line.order_line.supplier_item_id for line in lines} | set(current.values_list('item_id', flat=True)))
    if not item_ids:
        return 0, 0
    share_accounts(order)
    items = {item.pk: item for item in SupplierItem.objects.select_for_update().filter(pk__in=item_ids).order_by('pk')}
    held = list(StockReservation.objects.select_for_update(of=('self',)).filter(pk__in=current.values('pk')).order_by('created_at', 'pk'))
    offered = {line.pk for line in lines}
    for reservation in held:
        if reservation.quotation_line_id not in offered:
            release(items[reservation.item_id], reservation, actor, f'{order.reference} · v{quotation.revision} la reemplaza')
    kept = [reservation for reservation in held if reservation.quotation_line_id in offered]
    StockReservation.objects.filter(pk__in=[reservation.pk for reservation in kept]).update(expires_at=expires_at)
    reserved, label = 0, f'{order.reference} · cotización v{quotation.revision}' if expires_at else order.reference
    for line in lines:
        if any(reservation.quotation_line_id == line.pk for reservation in kept):
            continue
        item = items[line.order_line.supplier_item_id]
        item.reserved_quantity += line.quantity
        item.save(update_fields=['reserved_quantity', 'updated_at'])
        reservation = StockReservation.objects.create(item=item, order=order, quotation_line=line, quantity=line.quantity, expires_at=expires_at)
        StockEntry.objects.create(item=item, reservation=reservation, kind='reserve', quantity=line.quantity, direction='credit',
                                  balance_type='reservation', reserved_delta=line.quantity, reserved_direction='credit',
                                  reported_after=item.reported_quantity, reserved_after=item.reserved_quantity, actor=actor, reference=label[:120])
        reserved += line.quantity
    return reserved, reserved + sum(reservation.quantity for reservation in kept)


def held_units(order_lines):
    """{order line id: units its order's open reservations hold}: an order's own hold is part of what it can count on, so its stock
    checks add it back to the item's available units."""
    return dict(StockReservation.objects.filter(status='open', quotation_line__order_line__in=list(order_lines)).values_list(
        'quotation_line__order_line_id', 'quantity'))


def available_for(item, held=0):
    """The item's available units for an order that already holds `held` of them."""
    return max(0, item.reported_quantity - (item.reserved_quantity - held))


def open_reservations(items):
    """{item id: [open reservations with their order]} of confirmed deals for these supplier items (a quotation's hold is not a sale)."""
    held = {}
    for reservation in StockReservation.objects.filter(item__in=list(items), status='open', order__handshaked_at__isnull=False).select_related(
            'order').order_by('created_at', 'pk'):
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


def correct_shortfall(order, missing, actor):
    """Units the supplier never had ({quotation line id: units}): they leave the deal's open reservation and the supplier's count together,
    so available stock does not move and nothing comes back as available. A settled reservation needs nothing (the count already left the
    whole order out). Returns the units taken out of reservations."""
    items, held = held_by(order, missing)
    taken = 0
    for reservation in held:
        item, units = items[reservation.item_id], min(missing[reservation.quotation_line_id], reservation.quantity)
        if item.reserved_quantity < units:
            raise LedgerMismatch(f'Las reservas de {item.codigo} ({item.supplier_invent_id}) no cuadran: el artículo reserva {item.reserved_quantity} '
                                 f'unidades y el faltante quita {units}. No se guardó nada; avisa al administrador.')
        counted = min(units, item.reported_quantity)  # a count already below the missing units has nothing more to give
        item.reported_quantity -= counted
        if counted and item.source == 'apiag':
            # A shortfall names no sucursal: the rows go (their sum would no longer be the count) and the next pull listing the item
            # rebuilds them from apiag-cloud, whose count stays the supplier's own.
            SupplierItemBranchStock.objects.filter(item=item).delete()
        item.reserved_quantity -= units
        StockEntry.objects.create(item=item, reservation=reservation, kind='shortfall', quantity=counted or units, direction='debit',
                                  balance_type='stock' if counted else 'reservation', reserved_delta=units, reserved_direction='debit',
                                  reported_after=item.reported_quantity, reserved_after=item.reserved_quantity, actor=actor,
                                  reference=f'{order.reference} · faltante')
        item.save(update_fields=['reported_quantity', 'reserved_quantity', 'updated_at'])
        reservation.quantity -= units
        if not reservation.quantity:
            reservation.status, reservation.released_at = 'released', timezone.now()
        reservation.save(update_fields=['quantity', 'status', 'released_at'])
        taken += units
    return taken


def settle_synced(order, actor):
    """Entregado, for items kept by the apiag-cloud pull: once the supplier invoices the sale there, apiag-cloud's own count leaves those
    units out, so the deal's open reservations on them settle now (they would otherwise be discounted twice, the pull never settles).
    Items counted by Excel keep waiting for the next upload, which asks. Returns the units settled."""
    items, held = held_by(order)
    by_item = {}
    for reservation in held:
        if items[reservation.item_id].source == 'apiag':
            by_item.setdefault(reservation.item_id, []).append(reservation.pk)
    return sum(settle(items[item_id], reservations, actor, 'entregado · apiag-cloud') for item_id, reservations in sorted(by_item.items(), key=lambda pair: str(pair[0])))


def release(item, reservation, actor, reference):
    """Frees what a reservation still holds (the units exist, so they are available again); the item and the reservation are locked."""
    if item.reserved_quantity < reservation.quantity:
        raise LedgerMismatch(f'Las reservas de {item.codigo} ({item.supplier_invent_id}) no cuadran: el artículo reserva {item.reserved_quantity} '
                             f'unidades y {reference} retiene {reservation.quantity}. No se guardó nada; avisa al administrador.')
    item.reserved_quantity -= reservation.quantity
    StockEntry.objects.create(item=item, reservation=reservation, kind='release', quantity=reservation.quantity, direction='debit',
                              balance_type='reservation', reserved_delta=reservation.quantity, reserved_direction='debit',
                              reported_after=item.reported_quantity, reserved_after=item.reserved_quantity, actor=actor, reference=reference[:120])
    item.save(update_fields=['reserved_quantity', 'updated_at'])
    reservation.status, reservation.released_at = 'released', timezone.now()
    reservation.save(update_fields=['status', 'released_at'])
    return reservation.quantity


def release_deal(order, actor, why='cancelado'):
    """A rejected order or a cancelled deal frees the units it still held. Returns the units released."""
    items, held = held_by(order)
    return sum(release(items[reservation.item_id], reservation, actor, f'{order.reference} · {why}') for reservation in held)


def expire_due(now=None):
    """Quotations whose hold lapsed while the client decided: their units are free again (the quotation stays; a later confirmation checks
    the stock and reserves again). Each order in its own transaction, in the lock order (the order row first). Returns the orders released."""
    from django.db import transaction
    from .models import DealEvent, SupplierRequest
    now = now or timezone.now()
    due = list(StockReservation.objects.filter(status='open', expires_at__lte=now, order__handshaked_at__isnull=True)
               .values_list('order_id', flat=True).distinct().order_by('order_id'))
    expired = 0
    for order_id in due:
        with transaction.atomic():
            order = SupplierRequest.objects.select_for_update(of=('self',)).get(pk=order_id)
            if order.handshaked_at:
                continue  # confirmed meanwhile: the hold is the deal's now
            items, held = held_by(order)
            held = [reservation for reservation in held if reservation.expires_at and reservation.expires_at <= now]
            if not held:
                continue
            quotation = held[0].quotation_line.quotation
            # No one decided it: the ledger names who sent the quotation, and the event acts for no account (it reads MotionPartes).
            actor, revision = quotation.published_by, quotation.revision
            units = sum(release(items[reservation.item_id], reservation, actor, f'{order.reference} · cotización v{revision} vencida') for reservation in held)
            DealEvent.objects.create(order=order, account=None, actor=actor, kind='hold_expired', quotation=quotation,
                                     description=f'VENCIÓ LA RESERVA DE LA COTIZACIÓN V{revision}: {units} {"UNIDAD VUELVE" if units == 1 else "UNIDADES VUELVEN"} '
                                                 'A ESTAR DISPONIBLES. SI EL CLIENTE LA CONFIRMA, SE REVISAN LAS EXISTENCIAS.')
            expired += 1
    return expired
