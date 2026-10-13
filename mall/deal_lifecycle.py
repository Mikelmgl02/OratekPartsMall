"""After the handshake the supplier moves the order on and the client closes it, one step at a time and never on its own: process
(Procesando: the supplier started preparing it), ship (En camino; a counter pickup skips it), deliver (the supplier's Entregado) and receive
(the client's Recibido, which closes the order). While the supplier still has the goods (GOODS_HELD: Acuerdo confirmado or Procesando) it
can report units it could not find, the client accepts that shortfall, and either party can cancel; once the order left the supplier only an
Oratek administrator cancels it (mall.deal_admin), as with a dispute over any open agreement. The caller holds the order row lock, saves the
row (these functions set its fields and return the extra fields to save) and writes the DealEvent with the returned description.

Stock follows mall.stock_reservations: a shortfall takes the missing units out of the supplier's count and the reservation at once (they never
come back as available, and the client's answer does not change that); a cancellation always releases what the deal still holds; process,
ship and the supplier's Entregado move no stock, because the reservation stays until the supplier's next upload says its count already
discounts the order. Items kept by the apiag-cloud pull settle at the supplier's Entregado instead: apiag-cloud's count leaves the sale out
once the supplier invoices it there. The client's Recibido moves no stock at all.
"""
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .request_models import (ACTIVE_AGREEMENT, GOODS_HELD, OPEN_AGREEMENT, PRE_HANDSHAKE, DealShortfall, DealShortfallLine,
                             SupplierRequest)
from .request_views import RequestConflict
from .stock_reservations import correct_shortfall, release_deal, settle_synced

SHORTFALL_REASONS = dict(DealShortfall.REASONS)
CANCEL_REASONS = dict(SupplierRequest.CANCEL_REASONS)
CANCEL_SIDES = {'client': 'EL CLIENTE', 'supplier': 'EL PROVEEDOR', 'admin': 'MOTIONPARTES'}
CANCEL_FIELDS = ['cancelled_at', 'cancelled_by', 'cancelled_side', 'cancel_reason', 'cancel_note']
# Where each step may start. Procesando is required; a counter pickup goes from it straight to Entregado.
PROCESS_FROM = ('handshaked',)
SHIP_FROM = ('processing',)
DELIVER_FROM = ('processing', 'in_transit')
RECEIVE_FROM = ('delivered',)
SHIPPED = ('in_transit', 'delivered')  # the order left the supplier: only MotionPartes cancels it now


def units(count):
    return f'{count} {"UNIDAD" if count == 1 else "UNIDADES"}'


def accepted_quotation(row):
    return row.quotations.filter(client_confirmed_at__isnull=False).order_by('-revision').first()


def agreement(row, quotation=None):
    """The accepted quotation's lines after the shortfalls the client accepted (pending ones are shown apart), or None before a handshake."""
    quotation = quotation or accepted_quotation(row)
    if quotation is None:
        return None
    missing, pending = defaultdict(int), defaultdict(int)
    for line in DealShortfallLine.objects.filter(shortfall__order=row, shortfall__status__in=['accepted', 'pending']).values(
            'quotation_line_id', 'missing', 'shortfall__status'):
        (missing if line['shortfall__status'] == 'accepted' else pending)[line['quotation_line_id']] += line['missing']
    lines = [{'line': line, 'agreed': line.quantity, 'missing': missing[line.pk], 'pending': pending[line.pk], 'final': line.quantity - missing[line.pk]}
             for line in quotation.lines.select_related('order_line').order_by('pk')]
    total = sum((entry['line'].unit_price * entry['final'] for entry in lines), Decimal('0')).quantize(Decimal('0.01'))
    return {'quotation': quotation, 'lines': lines, 'units': sum(entry['final'] for entry in lines), 'total': total}


def pending_shortfall(row):
    return row.shortfalls.filter(status='pending').first()


def require_status(row, allowed, message):
    if row.status not in allowed:
        raise RequestConflict(message)


def report_shortfall(row, quotation, actor, lines, reason, note):
    """The supplier could not find some agreed units: the stock correction applies now, the agreement waits for the client. Only while
    the supplier still has the goods; the status stays as it was."""
    require_status(row, GOODS_HELD, 'Solo puedes reportar un faltante antes de enviar la orden (Acuerdo confirmado o Procesando).')
    if pending_shortfall(row):
        raise RequestConflict('Ya hay un faltante esperando la respuesta del cliente.')
    current = {entry['line'].order_line_id: entry for entry in agreement(row, quotation)['lines']}
    identifiers = [line['order_line_id'] for line in lines]
    if len(identifiers) != len(set(identifiers)):
        raise ValidationError('Indica cada artículo del faltante una sola vez.')
    for line in lines:
        entry = current.get(line['order_line_id'])
        if entry is None:
            raise ValidationError('Uno de los artículos no es parte de este acuerdo.')
        if line['missing'] > entry['final']:
            raise ValidationError(f'{entry["line"].order_line.codigo}: el acuerdo tiene {entry["final"]} unidades; el faltante no puede superarlas.')
    shortfall = DealShortfall.objects.create(order=row, quotation=quotation, reason=reason, note=note, reported_by=actor)
    DealShortfallLine.objects.bulk_create([DealShortfallLine(shortfall=shortfall, quotation_line=current[line['order_line_id']]['line'],
                                                             agreed=current[line['order_line_id']]['final'], missing=line['missing']) for line in lines])
    correct_shortfall(row, {current[line['order_line_id']]['line'].pk: line['missing'] for line in lines}, actor)
    detail = '; '.join(f'{current[line["order_line_id"]]["line"].order_line.codigo}: FALTAN {line["missing"]} DE {current[line["order_line_id"]]["final"]}'
                       for line in lines)
    return (f'EL PROVEEDOR REPORTÓ UN FALTANTE ({SHORTFALL_REASONS[reason].upper()}): {detail}.' + (f' {note}' if note else '')
            + ' EL CLIENTE DEBE ACEPTAR EL ACUERDO REDUCIDO O CANCELARLO.'), []


def accept_shortfall(row, actor):
    """The client keeps the smaller agreement, in the status it had; with nothing left it ends as a supplier cancellation."""
    shortfall = pending_shortfall(row)
    if row.status not in GOODS_HELD or shortfall is None:
        raise RequestConflict('No hay un faltante pendiente en este acuerdo.')
    shortfall.status, shortfall.responded_by, shortfall.responded_at = 'accepted', actor, timezone.now()
    shortfall.save(update_fields=['status', 'responded_by', 'responded_at'])
    left = agreement(row)['units']
    if left:
        return f'EL CLIENTE ACEPTÓ EL FALTANTE. EL ACUERDO QUEDA EN {units(left)}.', []
    fields, *_ = close(row, 'supplier', actor, 'no_stock' if shortfall.reason == 'no_stock' else 'cannot_deliver',
                      'EL CLIENTE ACEPTÓ EL FALTANTE DE TODAS LAS UNIDADES.')
    return 'EL CLIENTE ACEPTÓ EL FALTANTE DE TODAS LAS UNIDADES. EL ACUERDO QUEDA CANCELADO.', fields


def process(row, actor):
    """Procesando: the supplier started preparing the order (until then it shows Acuerdo confirmado). A pending shortfall does not stop it."""
    require_status(row, PROCESS_FROM, 'Solo puedes marcar como Procesando un acuerdo confirmado que aún no empezaste a preparar.')
    row.status, row.processing_at, row.processing_by = 'processing', timezone.now(), actor
    return 'EL PROVEEDOR ESTÁ PROCESANDO LA ORDEN.', ['processing_at', 'processing_by']


def ship(row, actor):
    """En camino: the order left the supplier, so no shortfall or party cancellation after it."""
    if row.status == 'handshaked':
        raise RequestConflict('Marca la orden como Procesando antes de enviarla.')
    require_status(row, SHIP_FROM, 'Solo puedes marcar En camino una orden en Procesando.')
    if pending_shortfall(row):
        raise RequestConflict('El cliente todavía debe responder al faltante reportado.')
    row.status, row.in_transit_at, row.in_transit_by = 'in_transit', timezone.now(), actor
    return f'EL PROVEEDOR ENVIÓ LA ORDEN: VA EN CAMINO CON {units(agreement(row)["units"])}.', ['in_transit_at', 'in_transit_by']


def deliver(row, actor):
    """The supplier's Entregado, from Procesando (a counter pickup) or En camino. The order stays open until the client's Recibido."""
    if row.status == 'handshaked':
        raise RequestConflict('Marca la orden como Procesando antes de marcarla como entregada.')
    require_status(row, DELIVER_FROM, 'Esta orden ya se marcó como entregada o está cerrada.')
    if pending_shortfall(row):
        raise RequestConflict('El cliente todavía debe responder al faltante reportado.')
    row.status, row.delivered_at, row.delivered_by = 'delivered', timezone.now(), actor
    settle_synced(row, actor)  # apiag-cloud items: their pulled count already leaves the sale out
    return (f'EL PROVEEDOR MARCÓ LA ORDEN COMO ENTREGADA: {units(agreement(row)["units"])}. FALTA QUE EL CLIENTE CONFIRME QUE LA RECIBIÓ.',
            ['delivered_at', 'delivered_by'])


def receive(row, actor):
    """Recibido: the client confirms it has the order, which closes it. No stock moves: Excel holds keep waiting for the supplier's next
    upload, and apiag-cloud's settled at the supplier's Entregado."""
    if row.status in ACTIVE_AGREEMENT:
        raise RequestConflict('El proveedor aún no marca la orden como entregada. Podrás confirmar que la recibiste cuando lo haga.')
    if row.status == 'received':
        raise RequestConflict('Ya confirmaste que recibiste esta orden.')
    require_status(row, RECEIVE_FROM, 'Esta orden no está esperando tu confirmación.')
    row.status, row.received_at, row.received_by = 'received', timezone.now(), actor
    return f'EL CLIENTE CONFIRMÓ QUE RECIBIÓ LA ORDEN: {units(agreement(row)["units"])}.', ['received_at', 'received_by']


def close(row, side, actor, reason, note):
    """Cancel: a pending shortfall is answered by the cancellation and the units the deal still holds are released (apiag-cloud's, settled
    at Entregado, stay settled). Returns the fields to save, whether the order had already left the supplier and the units released."""
    shipped = row.status in SHIPPED  # read before the status changes
    row.shortfalls.filter(status='pending').update(status='declined', responded_by=actor, responded_at=timezone.now())
    released = release_deal(row, actor, 'cancelado tras el envío' if shipped else 'cancelado')
    row.status, row.cancelled_at, row.cancelled_by, row.cancelled_side, row.cancel_reason, row.cancel_note = (
        'cancelled', timezone.now(), actor, side, reason, note)
    return CANCEL_FIELDS, shipped, released


OPEN_ORDER = PRE_HANDSHAKE


def reject(row, actor, reason, note):
    """Rechazar: the client turns the order down before any agreement (nothing is reserved yet); it ends cancelled by the client."""
    if row.status not in OPEN_ORDER:
        raise RequestConflict('Solo puedes rechazar una orden que aún no se confirmó.')
    fields, *_ = close(row, 'client', actor, reason, note)
    return f'EL CLIENTE RECHAZÓ LA ORDEN: {CANCEL_REASONS[reason].upper()}.' + (f' {note}' if note else ''), fields


def cancel(row, side, actor, reason, note):
    """A party cancels while the supplier still has the goods; MotionPartes (side admin) any open agreement the client has not received,
    which keeps the dispatch dates it reached."""
    if row.status in PRE_HANDSHAKE:  # nothing agreed yet: the client rejects the order instead
        raise RequestConflict('Solo se puede cancelar un acuerdo confirmado.')
    if side == 'admin':
        require_status(row, OPEN_AGREEMENT, 'Este acuerdo ya está cerrado.')
    elif row.status in SHIPPED:
        raise RequestConflict('La orden ya salió del proveedor y no se puede cancelar aquí. Escríbele a la otra parte; si no se ponen de '
                              'acuerdo, MotionPartes puede intervenir.')
    else:
        require_status(row, GOODS_HELD, 'Esta orden ya está cerrada.')
    fields, shipped, released = close(row, side, actor, reason, note)
    description = f'{CANCEL_SIDES[side]} CANCELÓ EL ACUERDO: {CANCEL_REASONS[reason].upper()}.' + (f' {note}' if note else '')
    # After dispatch the goods may be gone: say what the stock did, so the supplier corrects its count the right way (or not at all).
    if shipped and released:
        description += (f' {units(released)} QUE EL ACUERDO AÚN RESERVABA VUELVEN A ESTAR DISPONIBLES; SI LA MERCANCÍA NO REGRESÓ, EL '
                        'PROVEEDOR DEBE CORREGIR SU INVENTARIO.')
    elif shipped:
        description += ' SUS UNIDADES YA SE HABÍAN DESCONTADO DE LAS EXISTENCIAS DEL PROVEEDOR; SI LA MERCANCÍA REGRESA, DEBE SUMARLAS A SU INVENTARIO.'
    return description, fields


def lifecycle_data(row):
    """What both parties see of the agreement after the handshake: final lines, shortfalls, each step's date and cancellation."""
    current = agreement(row) if row.handshaked_at else None
    shortfalls = list(row.shortfalls.prefetch_related('lines__quotation_line__order_line').select_related('reported_by', 'responded_by'))
    return {
        'agreement': None if current is None else {
            'quotation_id': str(current['quotation'].pk), 'units': current['units'], 'total': str(current['total']),
            'lines': [{'order_line_id': str(entry['line'].order_line_id), 'codigo': entry['line'].order_line.codigo,
                       'description': entry['line'].order_line.description, 'agreed': entry['agreed'], 'missing': entry['missing'],
                       'pending_missing': entry['pending'], 'final': entry['final'], 'unit_price': str(entry['line'].unit_price),
                       'total': str((entry['line'].unit_price * entry['final']).quantize(Decimal('0.01')))} for entry in current['lines']]},
        'shortfalls': [{'id': str(item.pk), 'status': item.status, 'reason': item.reason, 'reason_label': SHORTFALL_REASONS[item.reason],
                        'note': item.note, 'reported_at': item.reported_at.isoformat(),
                        'responded_at': item.responded_at.isoformat() if item.responded_at else None,
                        'lines': [{'order_line_id': str(line.quotation_line.order_line_id), 'codigo': line.quotation_line.order_line.codigo,
                                   'description': line.quotation_line.order_line.description, 'agreed': line.agreed, 'missing': line.missing}
                                  for line in item.lines.all()]} for item in shortfalls],
        'processing_at': row.processing_at.isoformat() if row.processing_at else None,
        'in_transit_at': row.in_transit_at.isoformat() if row.in_transit_at else None,
        'delivered_at': row.delivered_at.isoformat() if row.delivered_at else None,
        'received_at': row.received_at.isoformat() if row.received_at else None,
        # Whether the handshake reserved stock (deals confirmed before reservations did not): the screens say what moves stock.
        'stock_reserved': row.reservations.exists(),
        'cancellation': None if row.status != 'cancelled' else {
            'side': row.cancelled_side, 'reason': row.cancel_reason, 'reason_label': CANCEL_REASONS[row.cancel_reason], 'note': row.cancel_note,
            'cancelled_at': row.cancelled_at.isoformat()},
    }
