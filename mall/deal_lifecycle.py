"""After the handshake: the supplier marks the deal delivered or reports units it could not find, the client accepts that shortfall or
either party cancels, and an Oratek administrator can cancel a disputed deal (mall.deal_admin). The caller holds the order row lock, saves the
row (these functions set its fields and return the extra fields to save) and writes the DealEvent with the returned description.

Stock follows mall.stock_reservations: a shortfall takes the missing units out of the supplier's count and the reservation at once (they never
come back as available, and the client's answer does not change that); a cancellation releases what the deal still holds; a delivery moves
no stock, because the reservation stays until the supplier's next upload says its count already discounts the order.
"""
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .request_models import DealShortfall, DealShortfallLine, SupplierRequest
from .request_views import RequestConflict
from .stock_reservations import correct_shortfall, release_deal

SHORTFALL_REASONS = dict(DealShortfall.REASONS)
CANCEL_REASONS = dict(SupplierRequest.CANCEL_REASONS)
CANCEL_SIDES = {'client': 'EL CLIENTE', 'supplier': 'EL PROVEEDOR', 'admin': 'MOTIONPARTES'}
CANCEL_FIELDS = ['cancelled_at', 'cancelled_by', 'cancelled_side', 'cancel_reason', 'cancel_note']


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


def require_open_agreement(row, message):
    if row.status != 'handshaked':
        raise RequestConflict(message)


def report_shortfall(row, quotation, actor, lines, reason, note):
    """The supplier could not find some agreed units: the stock correction applies now, the agreement waits for the client."""
    require_open_agreement(row, 'Solo puedes reportar un faltante de un acuerdo confirmado que aún no se entregó.')
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
    """The client keeps the smaller agreement; with nothing left it ends as a supplier cancellation."""
    shortfall = pending_shortfall(row)
    if row.status != 'handshaked' or shortfall is None:
        raise RequestConflict('No hay un faltante pendiente en este acuerdo.')
    shortfall.status, shortfall.responded_by, shortfall.responded_at = 'accepted', actor, timezone.now()
    shortfall.save(update_fields=['status', 'responded_by', 'responded_at'])
    left = agreement(row)['units']
    if left:
        return f'EL CLIENTE ACEPTÓ EL FALTANTE. EL ACUERDO QUEDA EN {units(left)}.', []
    fields = close(row, 'supplier', actor, 'no_stock' if shortfall.reason == 'no_stock' else 'cannot_deliver',
                   'EL CLIENTE ACEPTÓ EL FALTANTE DE TODAS LAS UNIDADES.')
    return 'EL CLIENTE ACEPTÓ EL FALTANTE DE TODAS LAS UNIDADES. EL ACUERDO QUEDA CANCELADO.', fields


def deliver(row, actor):
    require_open_agreement(row, 'Solo puedes marcar como entregado un acuerdo confirmado.')
    if pending_shortfall(row):
        raise RequestConflict('El cliente todavía debe responder al faltante reportado.')
    row.status, row.delivered_at, row.delivered_by = 'delivered', timezone.now(), actor
    return f'EL PROVEEDOR MARCÓ EL ACUERDO COMO ENTREGADO: {units(agreement(row)["units"])}.', ['delivered_at', 'delivered_by']


def close(row, side, actor, reason, note):
    """Cancel: a pending shortfall is answered by the cancellation and the units the deal still holds are released."""
    row.shortfalls.filter(status='pending').update(status='declined', responded_by=actor, responded_at=timezone.now())
    release_deal(row, actor)
    row.status, row.cancelled_at, row.cancelled_by, row.cancelled_side, row.cancel_reason, row.cancel_note = (
        'cancelled', timezone.now(), actor, side, reason, note)
    return CANCEL_FIELDS


def cancel(row, side, actor, reason, note):
    require_open_agreement(row, 'Solo se puede cancelar un acuerdo confirmado que aún no se entregó.')
    fields = close(row, side, actor, reason, note)
    return f'{CANCEL_SIDES[side]} CANCELÓ EL ACUERDO: {CANCEL_REASONS[reason].upper()}.' + (f' {note}' if note else ''), fields


def lifecycle_data(row):
    """What both parties see of the agreement after the handshake: final lines, shortfalls, delivery and cancellation."""
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
        'delivered_at': row.delivered_at.isoformat() if row.delivered_at else None,
        # Whether the handshake reserved stock (deals confirmed before reservations did not): the screens say what moves stock.
        'stock_reserved': row.reservations.exists(),
        'cancellation': None if row.status != 'cancelled' else {
            'side': row.cancelled_side, 'reason': row.cancel_reason, 'reason_label': CANCEL_REASONS[row.cancel_reason], 'note': row.cancel_note,
            'cancelled_at': row.cancelled_at.isoformat()},
    }
