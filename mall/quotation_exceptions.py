"""Server-side exception catalog for supplier quotations. Pure: callers pass the order lines, the values to check, live stock
(availability.check_lines, read without a lock) and the supplier settings; nothing here reads or writes the database.

Shared by draft GET and save, publish and the audit trace. Acknowledgements are tied to the value they confirm: one counts only
while its context equals the freshly computed context, so editing the quantity or price, or a further stock drop, needs a new
confirmation. Price codes (stale_price, below_floor, manual_price, …) arrive with the pricing engine.
"""
import hashlib

# Codes a supplier may confirm. A block policy turns a confirmable code into a block that no acknowledgement lifts.
ACKNOWLEDGEABLE = ('offered_gt_available', 'offered_gt_requested', 'identity_changed', 'zero_price')
EXCEPTION_CODES = ('quantity_missing', 'price_missing', 'all_zero', 'draft_outdated', *ACKNOWLEDGEABLE, 'reduced_to_stock', 'zero_offered')
SEVERITIES = ('block', 'confirm', 'info')


def identity_context(item):
    """Short hash of the live inventory identity: a further change to the item voids an earlier confirmation."""
    values = (item.supplier_id, item.part_id, item.supplier_invent_id, item.codigo, item.brand, item.matching_status)
    return hashlib.sha256('|'.join(str(value) for value in values).encode()).hexdigest()[:12]


def line_findings(line, value, entry, settings_row):
    """Exceptions of one order line for value {quantity, unit_price, quantity_source?} and its check_lines entry."""
    quantity, price, found = value['quantity'], value['unit_price'], []
    def add(code, severity, message, context=''):
        found.append({'code': code, 'severity': severity, 'message': message, 'context': context})
    if quantity is None:
        add('quantity_missing', 'block', 'Indica la cantidad ofrecida (0 si no la ofreces).')
    if price is None and quantity:
        add('price_missing', 'block', 'Falta el precio.')
    if quantity:
        if not entry['identity_ok']:
            add('identity_changed', 'confirm', 'El artículo de tu inventario cambió desde la solicitud; confirma que ofreces el mismo repuesto.',
                identity_context(line.supplier_item))
        elif quantity > entry['available']:
            add('offered_gt_available', settings_row.over_stock_policy, f'Ofreces {quantity} y tienes {entry["available"]} disponibles.',
                f'{quantity}>{entry["available"]}')
        if quantity > line.quantity:
            add('offered_gt_requested', settings_row.over_request_policy, f'Ofreces más unidades de las solicitadas ({quantity} de {line.quantity}).',
                f'{quantity}>{line.quantity}')
        if price is not None and price == 0:
            add('zero_price', 'confirm', 'Precio 0,00 con unidades ofrecidas.', f'{quantity}@0.00')
    if value.get('quantity_source') == 'available' and quantity is not None and quantity < line.quantity:
        add('reduced_to_stock', 'info', f'Ajustado a tus existencias ({quantity} de {line.quantity}).')
    if quantity == 0:
        add('zero_offered', 'info', 'No ofreces este artículo.')
    return found


def acknowledge(found, acknowledgements):
    """Marks confirm-level findings whose stored acknowledgement still matches the current context."""
    stored = {ack['code']: ack for ack in acknowledgements or ()}
    for item in found:
        ack = stored.get(item['code'])
        item['ack'] = ack if item['severity'] == 'confirm' and ack and ack.get('context') == item['context'] else None
        item['acknowledged'] = item['ack'] is not None
    return found


def compute_exceptions(lines, values, stock, settings_row, *, acknowledgements=None, order_acknowledgements=(), outdated=False):
    """-> {lines: {order_line_pk: [finding]}, order: [finding], summary: {blocking, to_confirm, info}}.

    values and stock are keyed by order line pk; acknowledgements maps an order line pk to its stored [{code, context, user_id, at}].
    """
    acknowledgements = acknowledgements or {}
    per_line = {line.pk: acknowledge(line_findings(line, values[line.pk], stock[line.pk], settings_row), acknowledgements.get(line.pk))
                for line in lines}
    order = []
    if outdated:
        order.append({'code': 'draft_outdated', 'severity': 'block', 'message': 'El borrador se preparó sobre una versión anterior; recárgalo.', 'context': ''})
    if lines and all(values[line.pk]['quantity'] == 0 for line in lines):
        order.append({'code': 'all_zero', 'severity': 'block', 'message': 'Ofrece al menos una unidad.', 'context': ''})
    acknowledge(order, order_acknowledgements)
    every = order + [item for found in per_line.values() for item in found]
    return {'lines': per_line, 'order': order, 'summary': {
        'blocking': sum(item['severity'] == 'block' for item in every),
        'to_confirm': sum(item['severity'] == 'confirm' and not item['acknowledged'] for item in every),
        'info': sum(item['severity'] == 'info' for item in every)}}


def publish_blockers(result, *, legacy=False):
    """Findings that stop a publish: any block, plus unacknowledged confirms unless the quote takes the legacy (draftless) path."""
    stops = lambda item: item['severity'] == 'block' or (not legacy and item['severity'] == 'confirm' and not item['acknowledged'])
    flat = [(None, item) for item in result['order']] + [(pk, item) for pk, found in result['lines'].items() for item in found]
    return [{'order_line_id': str(pk) if pk else None, **{key: item[key] for key in ('code', 'severity', 'message', 'context', 'acknowledged')}}
            for pk, item in flat if stops(item)]


def audit_findings(found):
    """The frozen copy kept in the publication trace: who confirmed each alert and when (null when unacknowledged)."""
    return [{'code': item['code'], 'severity': item['severity'], 'context': item['context'],
             'acknowledged_by': item['ack']['user_id'] if item['ack'] else None, 'acknowledged_at': item['ack']['at'] if item['ack'] else None}
            for item in found]
