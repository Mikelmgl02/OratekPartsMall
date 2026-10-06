"""Supplier-only stock checks for quotations. Never writes stock and never reaches a client payload.

Lock order (for the future reservation step): the SupplierRequest row first (callers already hold it), then SupplierItem
rows in pk order. Never take the supplier Account lock after the order lock.
"""
from .models import SupplierItem
from .quote_draft_models import DealQuotationAudit, DealQuotationLineAudit


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


def shortfall_since_quote(quotation, order_lines):
    """Re-checks a published revision against live stock. None for revisions published before the trace existed (exempt).

    Only stock that dropped since the quote counts: for each offered line, relied_on = min(quantity, available_at_quote), and a line
    falls short when its identity broke after the quote or fewer units than relied_on remain. A line confirmed above stock at quote
    time ("5>3") is short only once fewer than the 3 units the supplier relied on remain.
    """
    audit = DealQuotationAudit.objects.filter(quotation=quotation).first()
    if audit is None:
        return None
    rows = list(DealQuotationLineAudit.objects.filter(line__quotation=quotation).select_related('line').order_by('line_id'))
    now = {entry['order_line_id']: entry for entry in check_lines((order_lines[row.line.order_line_id], row.line.quantity) for row in rows)}
    lines = []
    for row in rows:
        entry, quantity = now[row.line.order_line_id], row.line.quantity
        relied_on = min(quantity, row.available_at_quote or 0)
        short = quantity > 0 and ((row.identity_ok_at_quote and not entry['identity_ok']) or (entry['available'] or 0) < relied_on)
        lines.append({'order_line_id': str(row.line.order_line_id), 'quantity': quantity, 'available_at_quote': row.available_at_quote,
                      'available_at_accept': entry['available'], 'identity_ok_at_quote': row.identity_ok_at_quote,
                      'identity_ok_at_accept': entry['identity_ok'], 'shortfall': short})
    return {'audit': audit, 'rows': rows, 'lines': lines, 'short': any(line['shortfall'] for line in lines)}


def record_accept_check(check, result, at):
    """Stores the accept-time snapshot on the trace; available_at_accept is kept for every audited line, whatever the outcome."""
    check['audit'].accept_check = {'result': result, 'lines': check['lines'], 'at': at.isoformat()}
    check['audit'].accept_checked_at = at
    check['audit'].save(update_fields=['accept_check', 'accept_checked_at'])
    for row, line in zip(check['rows'], check['lines']):
        row.available_at_accept = line['available_at_accept']
    DealQuotationLineAudit.objects.bulk_update(check['rows'], ['available_at_accept'])
