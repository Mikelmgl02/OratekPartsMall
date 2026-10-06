"""Deterministic supplier pricing engine (v2): identity guard, the client's list with its fallback, commercial rules, USD/PAB parity.

evaluate_line() is pure: no database, no AI input, Decimal end to end, one ROUND_HALF_UP to cents at the end. Exactly one rule applies to
a line, never stacked (decision D2): the client's agreement first, then the most specific target (item > line > brand > all), then the
highest minimum quantity; remaining ties take the higher price (rule_conflict when the prices differ), then the oldest rule. The
client's general discount is a synthetic client-wide rule. price_lines()/price_items() are the bulk loaders with a constant number of
queries. Runs on supplier paths only; its explanation is supplier-private.
"""
import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext

from django.db.models import Q
from django.utils import timezone

from .availability import line_item_identity_ok
from .pricing_models import ClientPricingProfile, PriceList, PriceListEntry, PricingRule, SupplierItemPricing, pricing_settings

ENGINE_VERSION = 'pricing-v2'
CENT = Decimal('0.01')
MAX_PRICE = Decimal('9999999999.99')
ROUNDING = 'ROUND_HALF_UP_0.01'
SCOPE_RANK = {'client': 2, 'all': 1}
TARGET_RANK = {'item': 4, 'line': 3, 'brand': 2, 'all': 1}
CLIENT_DISCOUNT = 'synthetic:client_discount'
CLIENT_DISCOUNT_NAME = 'DESCUENTO GENERAL DEL CLIENTE'
MAX_DISCARDED, MAX_BREAKS = 5, 2
LOAD = object()


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
        """False when the supplier has neither a price list nor a rule that prices this line: drafts then show no suggestion."""
        return self.explanation['price_list'] is not None or self.unit_price is not None

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


def rounded(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def rule_matches(rule, item, profile, on_date):
    """Everything but the quantity: active, inside its window, for every client or this pair, and aimed at this item."""
    if not rule['active'] or (rule['valid_from'] and on_date < rule['valid_from']) or (rule['valid_until'] and on_date > rule['valid_until']):
        return False
    if rule['scope'] == 'client' and not (profile and str(rule['profile_id']) == str(profile['id'])):
        return False
    target, value = rule['target'], rule['target_value']
    return target == 'all' or (target == 'item' and item is not None and str(rule['item_id']) == str(item['id'])) or (
        item is not None and value != '' and value == {'brand': item['brand'], 'line': item['discount_group']}.get(target))


def synthetic_discount(profile):
    """The client's general discount: a client-wide rule of the pair from one unit, carrying the profile's version."""
    return {'id': CLIENT_DISCOUNT, 'revision': profile['version'], 'name': CLIENT_DISCOUNT_NAME, 'scope': 'client', 'profile_id': profile['id'],
            'target': 'all', 'target_value': '', 'item_id': None, 'kind': 'discount', 'value': profile['discount_percent'], 'currency': '',
            'min_quantity': 1, 'valid_from': None, 'valid_until': None, 'active': True, 'created_at': ''}


def evaluate_line(*, identity_ok, quantity, currency, parity, settings_version, on_date, price_list=None, entry=None, floor_price=None, item=None,
                  profile=None, rules=()):
    """price_list: the resolved list {id, code, name, currency} or None (no list). entry: {id, revision, unit_price, updated_at} or None (no
    price), plus `list` and `fallback` when it came from the default list because the client's list has no price. item: {id, brand,
    discount_group}. profile: {id, version, discount_percent, archived_list} or None. rules: [{id, revision, name, scope, profile_id, target,
    target_value, item_id, kind, value, currency, min_quantity, valid_from, valid_until, active, created_at}] (non-matching ones are ignored).
    quantity is the offered quantity, or the requested one until the supplier sets it: it picks the rule but never enters the fingerprint."""
    floor_price = decimal_input(floor_price, 'floor_price')
    base_price = decimal_input(entry['unit_price'], 'unit_price') if entry else None
    for rule in rules:
        decimal_input(rule['value'], 'value')
    if profile:
        decimal_input(profile['discount_percent'], 'discount_percent')
    codes, steps, discarded, breaks, unit, parity_applied, base, winner = [], [], [], [], None, False, None, None
    if not identity_ok:
        status = 'identity_changed'
        codes.append('identity_changed')
    else:
        base_list = (entry.get('list') or price_list) if entry else None
        if entry:
            base = {'price_list_id': str(base_list['id']), 'price_list_code': base_list['code'], 'fallback': bool(entry.get('fallback')),
                    'entry_revision': entry['revision'], 'unit_price': amount(base_price), 'currency': base_list['currency'],
                    'updated_at': entry['updated_at'].isoformat() if entry['updated_at'] else None}
        if (profile and profile.get('archived_list')) or (entry and entry.get('fallback')):
            codes.append('fallback_list')
        base_usable = base_price is not None and (base_list['currency'] == currency or parity)
        pool = [rule for rule in rules if rule_matches(rule, item, profile, on_date)]
        if profile and profile['discount_percent'] > 0:
            pool.append(synthetic_discount(profile))

        def outcome(rule):
            """-> (exact price or None, reason it cannot apply)."""
            if rule['kind'] == 'net_price':
                return (rule['value'], '') if rule['currency'] == currency or parity else (None, 'moneda_distinta')
            if base_price is None:
                return None, 'sin_precio_base'
            if not base_usable:
                return None, 'moneda_distinta'
            with localcontext() as context:
                context.prec = 28
                return base_price * (Decimal('100') - rule['value']) / Decimal('100'), ''

        outcomes = {id(rule): outcome(rule) for rule in pool}
        rank = lambda rule: (SCOPE_RANK[rule['scope']], TARGET_RANK[rule['target']], rule['min_quantity'])

        def choose(basis):
            """The single winning rule at this quantity, with every other candidate in precedence order."""
            candidates = [rule for rule in pool if rule['min_quantity'] <= basis]
            def order(rule):
                price = outcomes[id(rule)][0]
                return (*(-value for value in rank(rule)), price is None, -(rounded(price) if price is not None else 0), str(rule['created_at'] or ''), str(rule['id']))
            candidates.sort(key=order)
            viable = [rule for rule in candidates if outcomes[id(rule)][0] is not None]
            return (viable[0] if viable else None), candidates

        def price_at(rule):
            exact = outcomes[id(rule)][0] if rule else base_price if base_usable else None
            return None if exact is None else rounded(exact)

        winner, candidates = choose(quantity)
        result_currency = None
        if winner:
            unit = price_at(winner)
            result_currency = winner['currency'] if winner['kind'] == 'net_price' else base_list['currency']
            steps.append({'kind': 'rule', 'rule_id': str(winner['id']), 'rule_revision': winner['revision'], 'name': winner['name'], 'scope': winner['scope'],
                          'target': winner['target'], 'target_value': winner['target_value'], 'min_quantity': winner['min_quantity'],
                          'action': winner['kind'], 'value': amount(winner['value']), 'before': amount(base_price), 'after': amount(unit)})
            for rule in candidates:
                if rule is winner:
                    continue
                price, reason = outcomes[id(rule)]
                if not reason:
                    tied = rank(rule) == rank(winner)
                    reason = 'menos_especifica' if not tied else 'empate' if rounded(price) == unit else 'precio_menor'
                    # A tie with another result is a conflict whether or not the rule still fits in the capped discarded list.
                    if reason == 'precio_menor' and 'rule_conflict' not in codes:
                        codes.append('rule_conflict')
                if len(discarded) < MAX_DISCARDED:
                    discarded.append({'rule_id': str(rule['id']), 'name': rule['name'], 'reason': reason})
        else:
            discarded = [{'rule_id': str(rule['id']), 'name': rule['name'], 'reason': outcomes[id(rule)][1]} for rule in candidates][:MAX_DISCARDED]
            if base_usable:
                unit, result_currency = rounded(base_price), base_list['currency']
        if unit is not None:
            status = 'priced'
            if result_currency != currency:
                parity_applied = True
                steps.append({'kind': 'parity', 'from': result_currency, 'to': currency, 'rate': '1'})
                codes.append('currency_parity')
            if unit > MAX_PRICE:
                unit, status = None, 'out_of_range'
        elif base_price is not None:
            status = 'currency_mismatch'
            codes.append('currency_mismatch')
        elif price_list is None:
            status = 'no_price_list'
        else:
            status = 'missing'
            codes.append('no_list_price')
        # Volume breaks: the next minimum quantities at which the price changes again (a higher threshold that keeps the previous break's
        # price is no new break).
        last = unit
        for threshold in sorted({rule['min_quantity'] for rule in pool if rule['min_quantity'] > quantity}):
            if len(breaks) >= MAX_BREAKS:
                break
            rule = choose(threshold)[0]
            price = price_at(rule)
            if rule and price is not None and price != last and price <= MAX_PRICE:
                breaks.append({'min_quantity': threshold, 'unit_price': amount(price), 'rule_name': rule['name']})
                last = price
    if unit is not None and floor_price is not None and unit < floor_price:
        codes.append('below_floor')
    applied = next((step for step in steps if step['kind'] == 'rule'), None)
    fingerprint = hashlib.sha256(json.dumps({
        'v': ENGINE_VERSION, 'settings_version': settings_version, 'currency': currency, 'parity_applied': parity_applied,
        'list_id': str(price_list['id']) if price_list else None, 'entry_id': entry['id'] if entry else None,
        'entry_revision': entry['revision'] if entry else None, 'rule_id': applied['rule_id'] if applied else None,
        'rule_revision': applied['rule_revision'] if applied else None, 'profile_id': str(profile['id']) if profile else None,
        'profile_version': profile['version'] if profile else None, 'result': amount(unit) if unit is not None else status}, sort_keys=True).encode()).hexdigest()
    explanation = {
        'engine': ENGINE_VERSION, 'evaluated_on': on_date.isoformat(), 'quantity_basis': quantity, 'status': status, 'unit_price': amount(unit),
        'currency': currency, 'profile': {'id': str(profile['id']), 'version': profile['version'], 'discount_percent': amount(profile['discount_percent']),
                                          'archived_list': profile.get('archived_list')} if profile else None,
        'price_list': {key: str(price_list[key]) for key in ('id', 'code', 'name', 'currency')} if price_list else None,
        'base': base, 'steps': steps, 'discarded': discarded, 'next_breaks': breaks, 'floor_price': amount(floor_price), 'codes': codes,
        'rounding': ROUNDING, 'fingerprint': fingerprint}
    return PriceResult(unit, status, fingerprint, explanation, floor_price, parity_applied)


def list_input(row):
    return {'id': row.pk, 'code': row.code, 'name': row.name, 'currency': row.currency} if row else None


def entry_input(entry, source=None):
    return {'id': entry.pk, 'revision': entry.revision, 'unit_price': entry.unit_price, 'updated_at': entry.updated_at,
            **({'list': list_input(source), 'fallback': True} if source else {})}


def profile_input(profile):
    if profile is None:
        return None
    archived = profile.price_list.code if profile.price_list_id and not profile.price_list.active else None
    return {'id': profile.pk, 'version': profile.version, 'discount_percent': profile.discount_percent, 'archived_list': archived}


def rule_input(rule):
    return {field: getattr(rule, field) for field in ('id', 'revision', 'name', 'scope', 'profile_id', 'target', 'target_value', 'item_id', 'kind', 'value',
                                                      'currency', 'min_quantity', 'valid_from', 'valid_until', 'active', 'created_at')}


def client_profile(supplier, client_id):
    """The pair's private profile (with its list), or None: no client, the supplier itself, or nothing saved yet."""
    if not client_id or str(client_id) == str(supplier.pk):
        return None
    return ClientPricingProfile.objects.select_related('price_list').filter(supplier=supplier, client_id=client_id).first()


def price_items(supplier, client_id, currency, rows, *, settings_row=None, on_date=None, profile=LOAD):
    """[(key, supplier_item, quantity, identity_ok)] -> {key: PriceResult}.

    Queries are constant in the number of items: the pair's profile (unless given), the default list, the entries of the resolved and
    fallback lists for these items, the items' pricing rows and the candidate rules, filtered in SQL by status, date window, scope and the
    targets these items can match (plus the settings when not given). Never locks or writes anything.
    """
    rows = list(rows)
    settings_row = settings_row or pricing_settings(supplier)
    on_date = on_date or timezone.localdate()
    profile = client_profile(supplier, client_id) if profile is LOAD else profile
    items = {item.pk: item for _, item, _, _ in rows}
    default = PriceList.objects.filter(supplier=supplier, is_default=True, active=True).first()
    assigned = profile.price_list if profile and profile.price_list_id and profile.price_list.supplier_id == supplier.pk else None
    resolved = assigned if assigned and assigned.active else default
    fallback = default if resolved and default and resolved.pk != default.pk and profile.fallback_to_default else None
    lists = [row.pk for row in (resolved, fallback) if row]
    entries = {(entry.price_list_id, entry.item_id): entry for entry in PriceListEntry.objects.filter(price_list_id__in=lists, item_id__in=items)} if lists else {}
    pricing = {row.item_id: row for row in SupplierItemPricing.objects.filter(supplier=supplier, item_id__in=items)}
    brands = {(item.brand or '').strip().upper() for item in items.values()} - {''}
    groups = {row.discount_group for row in pricing.values()} - {''}
    targets = Q(target='all') | Q(target='item', item_id__in=items) | Q(target='brand', target_value__in=brands) | Q(target='line', target_value__in=groups)
    scope = Q(scope='all') | Q(scope='client', profile=profile) if profile else Q(scope='all')
    candidates = PricingRule.objects.filter(scope, targets, Q(valid_from__isnull=True) | Q(valid_from__lte=on_date), Q(valid_until__isnull=True) | Q(valid_until__gte=on_date),
                                            supplier=supplier, active=True).order_by('created_at', 'id')
    rules = {'all': [], 'item': {}, 'brand': {}, 'line': {}}
    for rule in map(rule_input, candidates):
        if rule['target'] == 'all':
            rules['all'].append(rule)
        else:
            rules[rule['target']].setdefault(str(rule['item_id']) if rule['target'] == 'item' else rule['target_value'], []).append(rule)
    person, results = profile_input(profile), {}
    for key, item, quantity, identity_ok in rows:
        group = pricing[item.pk].discount_group if item.pk in pricing else ''
        brand = (item.brand or '').strip().upper()
        own = entries.get((resolved.pk, item.pk)) if resolved else None
        backup = entries.get((fallback.pk, item.pk)) if fallback and own is None else None
        results[key] = evaluate_line(
            identity_ok=identity_ok, quantity=quantity, currency=currency, parity=settings_row.usd_pab_parity, settings_version=settings_row.version,
            on_date=on_date, price_list=list_input(resolved), entry=entry_input(own) if own else entry_input(backup, fallback) if backup else None,
            floor_price=pricing[item.pk].floor_price if item.pk in pricing else None, item={'id': item.pk, 'brand': brand, 'discount_group': group},
            profile=person, rules=rules['all'] + rules['item'].get(str(item.pk), []) + (rules['brand'].get(brand, []) if brand else [])
            + (rules['line'].get(group, []) if group else []))
    return results


def price_lines(supplier, client_id, currency, lines_with_qty, *, settings_row=None, on_date=None, profile=LOAD):
    """[(order_line, quantity)] -> {order_line.pk: PriceResult}, with order lines carrying supplier_item. The quantity basis is the offered
    quantity, or the requested one while none is set (0 included: the suggestion is for the units the client asked for)."""
    return price_items(supplier, client_id, currency, [(line.pk, line.supplier_item, quantity or line.quantity, line_item_identity_ok(line, supplier.pk))
                                                       for line, quantity in lines_with_qty], settings_row=settings_row, on_date=on_date, profile=profile)
