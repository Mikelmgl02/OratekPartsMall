"""Deterministic supplier pricing engine (v1): identity guard, default list, base price, USD/PAB parity, floor flag.

evaluate_line() is pure: no database, no AI input, Decimal end to end, one ROUND_HALF_UP to cents at the end. price_lines() is the
bulk loader with a constant number of queries. Runs on supplier paths only; its explanation is supplier-private. Client profiles,
rules, fallback lists, volume breaks and the discarded/next_breaks lists arrive with S5 (the payload already has their slots).
"""
import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

from django.utils import timezone

from .availability import line_item_identity_ok
from .pricing_models import PriceList, PriceListEntry, SupplierItemPricing, pricing_settings

ENGINE_VERSION = 'pricing-v1'
CENT = Decimal('0.01')
MAX_PRICE = Decimal('9999999999.99')
ROUNDING = 'ROUND_HALF_UP_0.01'


@dataclass(frozen=True)
class PriceResult:
    unit_price: Decimal | None
    # priced | missing | no_price_list | identity_changed | currency_mismatch | out_of_range
    status: str
    fingerprint: str
    explanation: dict
    floor_price: Decimal | None
    parity_applied: bool

    @property
    def configured(self):
        """False when the supplier has no price list at all: drafts then show no suggestion."""
        return self.explanation['price_list'] is not None

    @property
    def codes(self):
        return self.explanation['codes']


def decimal_input(value, label):
    """Money enters the engine only as Decimal; a float (or anything else) is a programming error, never coerced."""
    if value is not None and (not isinstance(value, Decimal) or isinstance(value, bool)):
        raise TypeError(f'{label} debe ser Decimal, no {type(value).__name__}.')
    return value


def amount(value):
    return None if value is None else str(value.quantize(CENT))


def evaluate_line(*, identity_ok, quantity, currency, parity, settings_version, on_date, price_list=None, entry=None, floor_price=None):
    """price_list: {id, code, name, currency} or None (no list); entry: {id, revision, unit_price, updated_at} or None (no price)."""
    floor_price = decimal_input(floor_price, 'floor_price')
    base_price = decimal_input(entry['unit_price'], 'unit_price') if entry else None
    codes, steps, unit, parity_applied, base = [], [], None, False, None
    if not identity_ok:
        status = 'identity_changed'
        codes.append('identity_changed')
    elif price_list is None:
        status = 'no_price_list'
    elif entry is None:
        status = 'missing'
        codes.append('no_list_price')
    else:
        base = {'price_list_id': str(price_list['id']), 'price_list_code': price_list['code'], 'fallback': False, 'entry_revision': entry['revision'],
                'unit_price': amount(base_price), 'currency': price_list['currency'],
                'updated_at': entry['updated_at'].isoformat() if entry['updated_at'] else None}
        with localcontext() as context:
            context.prec = 28
            unit = base_price.quantize(CENT, rounding=ROUND_HALF_UP)
        status = 'priced'
        if price_list['currency'] != currency:
            if parity:
                parity_applied = True
                steps.append({'kind': 'parity', 'from': price_list['currency'], 'to': currency, 'rate': '1'})
                codes.append('currency_parity')
            else:
                unit, status = None, 'currency_mismatch'
                codes.append('currency_mismatch')
        if unit is not None and unit > MAX_PRICE:
            unit, status = None, 'out_of_range'
    if unit is not None and floor_price is not None and unit < floor_price:
        codes.append('below_floor')
    fingerprint = hashlib.sha256(json.dumps({
        'v': ENGINE_VERSION, 'settings_version': settings_version, 'currency': currency, 'parity_applied': parity_applied,
        'list_id': str(price_list['id']) if price_list else None, 'entry_id': entry['id'] if entry else None,
        'entry_revision': entry['revision'] if entry else None, 'rule_id': None, 'rule_revision': None, 'profile_id': None, 'profile_version': None,
        'result': amount(unit) if unit is not None else status}, sort_keys=True).encode()).hexdigest()
    explanation = {
        'engine': ENGINE_VERSION, 'evaluated_on': on_date.isoformat(), 'quantity_basis': quantity, 'status': status, 'unit_price': amount(unit),
        'currency': currency, 'profile': None,
        'price_list': {key: str(price_list[key]) for key in ('id', 'code', 'name', 'currency')} if price_list else None,
        'base': base, 'steps': steps, 'discarded': [], 'next_breaks': [], 'floor_price': amount(floor_price), 'codes': codes,
        'rounding': ROUNDING, 'fingerprint': fingerprint}
    return PriceResult(unit, status, fingerprint, explanation, floor_price, parity_applied)


def price_lines(supplier, client, currency, lines_with_qty, *, settings_row=None, on_date=None):
    """[(order_line, quantity)] -> {order_line.pk: PriceResult}, with order lines carrying supplier_item.

    Queries are constant in the number of lines: the default list, its entries for these items and the items' pricing rows (plus
    the settings when not given). The client is unused until S5 resolves its private profile. Never locks or writes anything.
    """
    pairs = list(lines_with_qty)
    settings_row = settings_row or pricing_settings(supplier)
    on_date = on_date or timezone.localdate()
    item_ids = {line.supplier_item_id for line, _ in pairs}
    default = PriceList.objects.filter(supplier=supplier, is_default=True, active=True).first()
    listed = {'id': default.pk, 'code': default.code, 'name': default.name, 'currency': default.currency} if default else None
    entries = {entry.item_id: {'id': entry.pk, 'revision': entry.revision, 'unit_price': entry.unit_price, 'updated_at': entry.updated_at}
               for entry in PriceListEntry.objects.filter(price_list=default, item_id__in=item_ids)} if default else {}
    floors = dict(SupplierItemPricing.objects.filter(supplier=supplier, item_id__in=item_ids).values_list('item_id', 'floor_price'))
    return {line.pk: evaluate_line(identity_ok=line_item_identity_ok(line, supplier.pk), quantity=quantity or line.quantity, currency=currency,
                                   parity=settings_row.usd_pab_parity, settings_version=settings_row.version, on_date=on_date, price_list=listed,
                                   entry=entries.get(line.supplier_item_id), floor_price=floors.get(line.supplier_item_id))
            for line, quantity in pairs}
