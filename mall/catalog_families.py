"""Code families are review candidates, never automatic compatibility decisions."""
import re
from collections import Counter


POSITION_SUFFIXES = {'LEFT', 'RIGHT', 'LH', 'RH', 'L', 'R', 'IZQ', 'IZQUIERDO', 'IZQUIERDA',
                    'DER', 'DERECHO', 'DERECHA', 'FRONT', 'REAR', 'DEL', 'DELANTERO',
                    'DELANTERA', 'TRAS', 'TRASERO', 'TRASERA', 'UPPER', 'LOWER',
                    'SUPERIOR', 'INFERIOR', 'INT', 'EXT', 'INTERIOR', 'EXTERIOR',
                    'ABS', 'STD', 'TURBO', 'KIT', 'SET', 'PAIR', 'MM', 'CM'}


def normalized_reference(value):
    return re.sub(r'[^A-Z0-9]', '', value.upper())


def supplier_base(sku):
    tokens = sku.strip().upper().split('-')
    suffixes = []
    while tokens and re.fullmatch(r'[A-Z]{1,12}', tokens[-1]):
        suffixes.append(tokens.pop())
    base = '-'.join(tokens)
    reference = normalized_reference(base)
    if not suffixes or any(token in POSITION_SUFFIXES for token in suffixes):
        return ''
    return base if len(reference) >= 8 and sum(char.isdigit() for char in reference) >= 3 else ''


def description_text(row):
    return re.sub(r'\s+', ' ', row.get('description', '').strip().upper())


def application(row):
    text = description_text(row)
    match = re.fullmatch(r'(.+?)\s+((?:19|20)?\d{2})\s*[-–/]\s*((?:19|20)?\d{2})', text)
    if not match:
        return text, None
    def year(value):
        value = int(value)
        return value if value >= 100 else value + (1900 if value >= 70 else 2000)
    start, end = year(match[2]), year(match[3])
    return (match[1], (start, end)) if start <= end else (text, None)


def descriptions_compatible(source, target):
    first, second = description_text(source), description_text(target)
    if first and first == second:
        return True
    a, years_a = application(source)
    b, years_b = application(target)
    # Only a trailing, overlapping application range may differ. Other vehicle,
    # position, dimension and specification text must still be identical.
    return bool(a and a == b and years_a and years_b and max(years_a[0], years_b[0]) <= min(years_a[1], years_b[1]))


def supplier_suffix_match(source, target):
    return not source.get('is_OEM') and supplier_base(source['sku']) == target['sku'].strip().upper() and descriptions_compatible(source, target)


def family_candidates(rows, blocked_parents=()):
    """Find full base codes, including proposed parents absent from the catalog."""
    buckets = {}
    for sku, row in rows.items():
        if row.get('is_OEM'):
            continue  # An approved OEM suffix is part of its identity, not a company suffix.
        base = supplier_base(sku)
        if base:
            buckets.setdefault(base, []).append(row)
    families = {}
    for base, members in sorted(buckets.items()):
        target = rows.get(base)
        if target:
            members = [row for row in members if supplier_suffix_match(row, target)]
        else:
            if base in blocked_parents or len(members) < 2:
                continue
            # Do not manufacture one family from contradictory applications.
            if any(not descriptions_compatible(a, b) for a in members for b in members):
                continue
            description = Counter(description_text(row) for row in members).most_common(1)[0][0]
            target = {'id': '', 'sku': base, 'row': None, 'name': base, 'description': description,
                      'category': '', 'subcategory': '', 'codes': [], 'active': True,
                      'merged_into': None, 'proposed_parent': True,
                      'family_members': sorted(row['sku'] for row in members)}
        if members:
            warnings = []
            if any(description_text(row) != description_text(target) for row in members):
                warnings.append('Los años de aplicación difieren entre los códigos. Confirma la equivalencia antes de agrupar.')
            families[base] = {'target': target, 'sources': members, 'warnings': warnings}
    return families
