"""Server-side exception catalog for supplier quotations. Pure: callers pass the order lines, the values to check, live stock
(availability.check_lines, read without a lock) and the supplier settings; nothing here reads or writes the database.

Shared by draft GET and save, publish and the audit trace. Acknowledgements are tied to the value they confirm: one counts only
while its context equals the freshly computed context, so editing the quantity or price, or a further stock drop, needs a new
confirmation. Price codes compare the line with the live pricing_engine result (passed in, never computed here); a stored price is
never changed by them: a moved suggestion only raises stale_price.
"""
import hashlib

# Codes a supplier may confirm. A block policy turns a confirmable code into a block that no acknowledgement lifts.
# Keep in sync with QuoteExceptionCodeEnum / AcknowledgeableExceptionEnum (config/settings.py) and the frontend label map.
ACKNOWLEDGEABLE = ('offered_gt_available', 'offered_gt_requested', 'identity_changed', 'zero_price', 'below_floor', 'stale_price')
# client_price_request and assistant_unmatched are info alerts from the AI assistant's latest run (quote_assistant.assistant_findings).
EXCEPTION_CODES = ('quantity_missing', 'price_missing', 'all_zero', 'draft_outdated', *ACKNOWLEDGEABLE, 'reduced_to_stock', 'zero_offered',
                   'manual_price', 'differs_from_list', 'no_list_price', 'currency_parity', 'currency_mismatch', 'fallback_list', 'rule_conflict', 'no_profile',
                   'client_price_request', 'assistant_unmatched')
SEVERITIES = ('block', 'confirm', 'info')


def es_money(value):
    """12345.5 -> '12.345,50', the format of every amount in these messages."""
    return f'{value:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')


def identity_context(item):
    """Short hash of the live inventory identity: a further change to the item voids an earlier confirmation."""
    values = (item.supplier_id, item.part_id, item.supplier_invent_id, item.codigo, item.brand, item.matching_status)
    return hashlib.sha256('|'.join(str(value) for value in values).encode()).hexdigest()[:12]


def line_findings(line, value, entry, settings_row, suggestion=None):
    """Exceptions of one order line for value {quantity, unit_price, quantity_source?, price_source?, engine_fingerprint?}, its
    check_lines entry and its live pricing_engine result (None when not priced: the price codes are skipped)."""
    quantity, price, found = value['quantity'], value['unit_price'], []
    source, suggested = value.get('price_source'), suggestion.unit_price if suggestion else None
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
        floor = suggestion.floor_price if suggestion else None
        if price is not None and floor is not None and price < floor:
            add('below_floor', 'confirm', f'Precio por debajo de tu mínimo ({es_money(floor)}).', f'{price:.2f}<{floor:.2f}')
        stored = value.get('engine_fingerprint')
        # Only a moved value is stale: a new fingerprint with the same price (an unrelated settings change) needs nothing.
        if suggestion and source == 'engine' and price is not None and stored and stored != suggestion.fingerprint and price != suggested:
            add('stale_price', 'confirm', f'Tu precio sugerido cambió de {es_money(price)} a {es_money(suggested)} desde que se calculó.'
                if suggested is not None else f'Tu lista ya no sugiere un precio para este artículo (antes {es_money(price)}).',
                f'{stored[:12]}>{suggestion.fingerprint[:12]}')
    if value.get('quantity_source') == 'available' and quantity is not None and quantity < line.quantity:
        add('reduced_to_stock', 'info', f'Ajustado a tus existencias ({quantity} de {line.quantity}).')
    if quantity == 0:
        add('zero_offered', 'info', 'No ofreces este artículo.')
    if suggested is not None and price is not None and price != suggested and source in ('manual', 'previous'):
        add('manual_price' if source == 'manual' else 'differs_from_list', 'info', f'Precio manual · sugerido {es_money(suggested)}.' if source == 'manual'
            else f'La versión anterior usó {es_money(price)}; tu lista sugiere hoy {es_money(suggested)}.')
    if suggestion and suggestion.status == 'missing' and quantity != 0:
        add('no_list_price', 'info', 'Sin precio en tu lista.')
    if suggestion and 'fallback_list' in suggestion.codes and quantity != 0:
        base, assigned = suggestion.explanation['base'], suggestion.explanation['profile'] or {}
        add('fallback_list', 'info', f'Precio de la lista {base["price_list_code"]} (respaldo).' if base and base['fallback'] else
            f'La lista {assigned.get("archived_list")} del cliente está archivada; se usa tu lista {(suggestion.explanation["price_list"] or {}).get("code", "predeterminada")}.')
    if suggestion and 'rule_conflict' in suggestion.codes and quantity != 0:
        add('rule_conflict', 'info', 'Dos reglas igual de específicas; se usó la de precio mayor.')
    return found


def acknowledge(found, acknowledgements):
    """Marks confirm-level findings whose stored acknowledgement still matches the current context."""
    stored = {ack['code']: ack for ack in acknowledgements or ()}
    for item in found:
        ack = stored.get(item['code'])
        item['ack'] = ack if item['severity'] == 'confirm' and ack and ack.get('context') == item['context'] else None
        item['acknowledged'] = item['ack'] is not None
    return found


def compute_exceptions(lines, values, stock, settings_row, *, pricing=None, acknowledgements=None, order_acknowledgements=(), outdated=False,
                       no_profile=False, extra=None):
    """-> {lines: {order_line_pk: [finding]}, order: [finding], summary: {blocking, to_confirm, info}}.

    values, stock and pricing (live pricing_engine results) are keyed by order line pk; acknowledgements maps an order line pk to its
    stored [{code, context, user_id, at}]; no_profile: the client has no private profile with this supplier yet; extra: info findings
    built elsewhere ({lines: {pk: [finding]}, order: [finding]}), such as the assistant's.
    """
    acknowledgements, pricing, extra = acknowledgements or {}, pricing or {}, extra or {'lines': {}, 'order': []}
    per_line = {line.pk: acknowledge(line_findings(line, values[line.pk], stock[line.pk], settings_row, pricing.get(line.pk))
                                     + [dict(item) for item in extra['lines'].get(line.pk, [])], acknowledgements.get(line.pk)) for line in lines}
    order = []
    if outdated:
        order.append({'code': 'draft_outdated', 'severity': 'block', 'message': 'El borrador se preparó sobre una versión anterior; recárgalo.', 'context': ''})
    if lines and all(values[line.pk]['quantity'] == 0 for line in lines):
        order.append({'code': 'all_zero', 'severity': 'block', 'message': 'Ofrece al menos una unidad.', 'context': ''})
    results = [result for result in pricing.values() if result]
    listed = next((result.explanation['price_list'] for result in results if result.explanation['price_list']), None)
    if no_profile and listed:
        order.append({'code': 'no_profile', 'severity': 'info', 'context': '',
                      'message': f'Cliente sin perfil comercial; se usa tu lista predeterminada {listed["code"]}.'})
    parity = next((result for result in results if result.parity_applied), None)
    if parity:
        step = next(step for step in parity.explanation['steps'] if step['kind'] == 'parity')
        order.append({'code': 'currency_parity', 'severity': 'info', 'context': '',
                      'message': f'Paridad USD/PAB 1:1 aplicada: tu lista está en {step["from"]} y la cotización en {step["to"]}.'})
    mismatch = next((result for result in results if result.status == 'currency_mismatch'), None)
    if mismatch:
        order.append({'code': 'currency_mismatch', 'severity': 'info', 'context': '', 'message': (
            f'Moneda distinta: tu lista está en {mismatch.explanation["price_list"]["currency"]} y la cotización en {mismatch.explanation["currency"]}; '
            'sin precio sugerido.')})
    order += [dict(item) for item in extra['order']]
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
