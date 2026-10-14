"""Supplier catalog import: normalized PDF-catalog extracts -> OEM table, company alternos and technical sheets.

    python -m mall.supplier_catalog_import --dry-run|--apply FILE [FILE ...] [--report PATH]

FILE follows the normalized extract schema (catalog + items with brand_code, product_type, oem, cross_refs, applications, specs).
Every OEM number a catalog prints becomes, or joins, a number of the library (from inventory refactor S4 a Reference: tipo oem, the
manufacturer as brand, mall.references.add_library_number, on the line that carries it or on a line of its own in referencia) with one
aftermarket_catalog source per catalog (status declared), whether or not a SKU carries it. A catalog item is linked to a SKU only when one of its numbers equals a code of that SKU
(the SKU, its OEM base, alternos or supplier codes), the SKU description classifies to the item's product type with the deterministic
category rules, and the makes agree. A linked SKU receives the catalog's own code and its competitor codes as company alternos only
when no OEM number carries them to it (see below) and, when the item prints measurements, its subgrupo template gains those fields
and the SKU gets the values. OEM numbers become OEM
alternos only on SKUs already marked OEM: on any other SKU reconcile_identities would promote a sole sourced OEM alterno to MAIN
outside the OEM finder's canary and review. Nothing is overwritten: a code owned by another SKU, a different stored measurement or
another subgrupo is reported and left alone. Re-running a file changes nothing.

The aftermarket codes an item prints (its own code and the competitor codes) are also filed under each of its OEM numbers as
OEMCrossReference rows, whether or not a SKU carries the number, and every SKU linked to the item, duplicates included, is tied to
those numbers (PartOEMLink, source catalog) when the makes agree. A number the catalog prints for two or more different parts (an
inner and an outer joint of one drive shaft, a row the extract misread) says nothing about which part replaces it: it files no codes
and ties no SKU, and a SKU matched to an item only through such numbers is tied to none of the item's numbers. Variants of one part
(GWT-116A and GWT-116AH, ATE 24.0108-0111.1 and its PowerDisc 24.0308-0111.1) count as one. A drive-shaft assembly number listed for
a CV component files nothing either: a finder assembly class under the make's own or partner numbering (Mitsubishi prints Hyundai
shafts), or a number linked to a SKU sold as a whole shaft (EJE COMPLETO, SEMIEJE). Mazda's 25-40X/25-50X/25-60X, printed under
Mazda, Ford or Kia, name the joint (owner, 2026-10-14), and Mitsubishi's MR series names joints as well as shafts: only such a SKU
makes one of them a shaft. The catalog's own item code is filed even
without a digit (ASVA FDWH-FOCMF); a competitor code needs one. A printed number the library does not hold whose twin spelling it
holds (16100-87795-000 and 16100-87795: mall.refactor.s0_normalize_numbers) is filed under the twin, and in the GM family under the
make's own canonical spelling when another make of the family holds it. That tool ran on the OEM table before S4 and refuses to
write once S4 moved the library to References (the table is frozen; twins left over are S9's).
Whether a number is printed for other parts too is decided per printed spelling, as before the twins became one, so the kept number
holds what each spelling files and a replace after the merge changes nothing.
The name links
of S2a's time are retired with S4 (a number a SKU's codigo names sits on that SKU, or rule 3 proposes it in S6). Codes an OEM number carries to the
SKU are not copied onto it as company alternos (alternos step 2c); a SKU matched only through shared numbers gets none either, so
only an item whose codes no number can carry (no OEM number printed, or only shared or assembly ones) still writes them on the SKU.
These rows are derived from the
extracts and the importer only adds to them: after changing these rules, empty OEMCrossReference and the catalog links and re-run
every extract. A corrected extract of one catalog replaces what the old one filed with mall.supplier_catalog_replace.
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

TAXONOMY = {
    'water_pump': ('REFRIGERACIÓN', 'BOMBAS DE AGUA'), 'fan_clutch': ('REFRIGERACIÓN', 'EMBRAGUES DE VENTILADOR'),
    'brake_pad': ('FRENOS', 'PASTILLAS DE FRENO'),
    'brake_disc': ('FRENOS', 'DISCOS DE FRENO'), 'brake_drum': ('FRENOS', 'TAMBORES DE FRENO'), 'brake_shoe': ('FRENOS', 'BANDAS DE FRENO'),
    'brake_wear_sensor': ('FRENOS', 'ACCESORIOS DE PASTILLAS'), 'bushing': ('SUSPENSIÓN', 'BUJES DE BRAZO'),
    'stabilizer_bushing': ('SUSPENSIÓN', 'BUJES DE ESTABILIZADORA'), 'engine_mount': ('MOTOR', 'BASES DE MOTOR'),
    'strut_mount': ('SUSPENSIÓN', 'BASES DE AMORTIGUADOR'), 'tensioner': ('MOTOR', 'TENSORES DE CORREA'),
    'idler_pulley': ('MOTOR', 'TENSORES DE CORREA'), 'timing_belt_kit': ('MOTOR', 'KITS DE DISTRIBUCIÓN'),
    'cv_joint_outer': ('TRANSMISIÓN', 'JUNTAS HOMOCINÉTICAS'), 'cv_joint_inner': ('TRANSMISIÓN', 'JUNTAS HOMOCINÉTICAS'),
    'cv_axle': ('TRANSMISIÓN', 'SEMIEJES'), 'cv_boot': ('TRANSMISIÓN', 'BOTAS DE HOMOCINÉTICA'),
    'wheel_hub': ('RODAMIENTOS', 'CUBOS DE RUEDA'), 'hub_bearing': ('RODAMIENTOS', 'RODAMIENTOS DE RUEDA'),
    'wheel_bearing': ('RODAMIENTOS', 'RODAMIENTOS DE RUEDA'),
}
# The category rules may put a SKU of the same physical family in a sibling subgrupo; these count as agreement too.
COMPATIBLE = {
    'bushing': [('SUSPENSIÓN', 'BUJES DE BRAZO'), ('SUSPENSIÓN', 'BUJES DE ESTABILIZADORA'), ('SUSPENSIÓN', 'BUJES DE MUELLE'),
                ('DIRECCIÓN', 'BUJES DE CREMALLERA')],
    'tensioner': [('MOTOR', 'TENSORES DE CORREA'), ('RODAMIENTOS', 'RODAMIENTOS DE TENSOR')],
    # CV JOINT is the catalog's own subgrupo for CV joints (PUNTA FLECHA EXT/INT); the category rules say JUNTAS HOMOCINÉTICAS.
    'cv_joint_outer': [('TRANSMISIÓN', 'JUNTAS HOMOCINÉTICAS'), ('TRANSMISIÓN', 'CV JOINT')],
    'cv_joint_inner': [('TRANSMISIÓN', 'JUNTAS HOMOCINÉTICAS'), ('TRANSMISIÓN', 'CV JOINT')],
    'idler_pulley': [('MOTOR', 'TENSORES DE CORREA'), ('RODAMIENTOS', 'RODAMIENTOS DE TENSOR')],
    'wheel_hub': [('RODAMIENTOS', 'CUBOS DE RUEDA'), ('RODAMIENTOS', 'RODAMIENTOS DE RUEDA')],
    'hub_bearing': [('RODAMIENTOS', 'RODAMIENTOS DE RUEDA'), ('RODAMIENTOS', 'CUBOS DE RUEDA')],
}
# Product families the shared category rules do not cover yet: used only when local_category finds no rule for a description.
EXTRA_RULES = [(c, s, re.compile(pattern)) for c, s, pattern in [
    ('REFRIGERACIÓN', 'EMBRAGUES DE VENTILADOR', r'^(?:FAN CLUTCH|EMBRAGUE (?:DE )?VENT(?:ILADOR)?|ACOPLE (?:DE )?VENTILADOR)\b'),
    ('FRENOS', 'ACCESORIOS DE PASTILLAS', r'^(?:SENSOR (?:DE )?(?:DESGASTE|PASTILLAS?)|ALARMA (?:DE )?(?:PASTILLAS?|FRENO)|TESTIGO (?:DE )?PASTILLAS?|CABLE (?:DE )?(?:SENSOR|ALARMA) (?:DE )?PASTILLAS?)\b'),
    # Wheel hubs ("HUB DEL", "HUB TOY DEL"); free-wheel locking hubs are another part.
    ('RODAMIENTOS', 'CUBOS DE RUEDA', r'^(?:HUB|MAZA|CUBO (?:DE )?RUEDA)\b(?! (?:LIBRE|LOCK|LOCKING|MANUAL|AUTOMATIC[OA]?|AUTO)\b)'),
    ('RODAMIENTOS', 'RODAMIENTOS DE RUEDA', r'^BALINERA (?:DE )?(?:RDA|RUEDA)\b'),
    ('RODAMIENTOS', 'RODAMIENTOS DE TENSOR', r'^(?:BALINERA (?:TENSOR|FIJA)|TENSOR BALINERA)\b'),
    ('MOTOR', 'TENSORES DE CORREA', r'^(?:POLEA (?:ALT|AJUST|LOCA|GUIA|TENSORA|INTERMEDIA)|TENSOR (?:ALT|HIDRA))\b'),
]]
# Codes and ambiguous printed columns stay text even when they look numeric (ASVA boot codes such as 2071).
TEXT_KEYS = {'cv_boot', 'boot_code', 'joint_type', 'design', 'type', 'splines_or_diameter', 'brake_system', 'bolt_hole_diameter'}
# Free-text remarks (often German) and the brand's own catalog numbers (ATE short number) would surface in the customer sheet as if
# they were measurements of the SKU: never imported into templates.
SKIP_KEYS = {'note', 'remark', 'remarks', 'short_number'}
# Plausible ranges for printed measurements: a value outside (a catalog typo such as a 630 mm seal) is reported, never stored.
SPEC_RANGES = {'seal_diameter': (15, 130), 'outer_splines': (10, 60), 'inner_splines': (10, 60), 'splines': (10, 60), 'abs_teeth': (20, 120),
               'length': (10, 2500), 'big_diameter': (20, 250), 'small_diameter': (8, 120), 'inner_diameter': (3, 200),
               'outer_diameter': (5, 400), 'holes': (2, 12), 'bolts': (2, 12), 'diameter': (100, 520), 'max_diameter': (100, 520),
               'thickness': (3, 60), 'min_thickness': (3, 60), 'height': (5, 400), 'pcd': (60, 250), 'center_bore': (30, 220),
               'width': (10, 260), 'depth': (10, 200), 'wheel_cylinder_diameter': (10, 40)}
# Product-specific ranges win over SPEC_RANGES (a disc's inner hat diameter is not a bushing bore).
SPEC_RANGES_BY_TYPE = {('brake_disc', 'inner_diameter'): (50, 320), ('brake_drum', 'inner_diameter'): (50, 320),
                       ('wheel_hub', 'inner_diameter'): (10, 120), ('hub_bearing', 'inner_diameter'): (10, 120)}
INTEGER_KEYS = {'holes', 'bolts', 'outer_splines', 'inner_splines', 'abs_teeth', 'teeth', 'splines', 'quantity', 'pieces'}
BOOLEAN_KEYS = {'vented', 'wear_indicator', 'abs', 'with_abs', 'with_sensor'}
YES, NO = {'YES', 'SI', 'SÍ', 'TRUE', 'Y', '+', 'X'}, {'NO', 'FALSE', 'N', '-'}
SAMPLE = 40
MIN_BARE_CODE = 4  # letters and digits an own item code without a digit needs to be filed (ASVA CH-WK)
# Variants of one part a brand codes beyond a letter suffix, folded onto one key: ATE's PowerDisc 24.03 is its 24.01 disc, slotted.
VARIANT_FOLDS = {'ATE': [(re.compile(r'^2403(\d{7})$'), r'2401\1')]}
# Twin spellings of one OEM number (canonical_number).
TRAILING_000_MAKES = {'DAIHATSU', 'SUZUKI', 'TOYOTA'}
GM_FAMILY = {'OPEL', 'VAUXHALL', 'GM', 'CHEVROLET', 'DAEWOO', 'HOLDEN', 'SAAB'}
OPEL_CATALOG_MAKES = {'OPEL', 'VAUXHALL'}
# The OEM finder's Mazda drive-shaft classes name only the CV joint (owner, 2026-10-14): 25-40X, 25-50X and 25-60X, X variants
# included, printed under Mazda, Ford (G565-25-500B) or Kia (B002-25-400B; Kia's Mazda-era 0K2N1-25-600 takes no class of its own).
JOINT_CLASSES = {('MAZDA', '25-40'), ('MAZDA', '25-50'), ('MAZDA', '25-60')}
# A SKU sold as a whole drive shaft: the numbers linked to it are shaft numbers, whatever CV component a catalog lists them for. Only
# a description that starts with it: BOTA SEMIEJE or RETEN SEMIEJE is a part of the shaft.
SHAFT_RE = re.compile(r'^(?:EJE COMPLETO|SEMI-?EJES?)\b')
ASSEMBLY_TEXT = ('SEMIEJE (EJE COMPLETO)', 'SEMIEJES')  # the description and part type a shaft number listed for a component gets


def compact(value):
    return re.sub(r'[^0-9A-Z]', '', (value or '').upper())


def canonical_number(manufacturer, number):
    """The spelling twin OEM numbers settle on (compact): Daihatsu and Suzuki print a trailing -000 the same number drops elsewhere
    (16100-87795-000 = 16100-87795, Toyota-badged Daihatsus included), and an Opel catalog number has seven digits, so a dropped
    leading zero comes back (636160 -> 0636160) and one too many goes (02898355 -> 2898355), the latter across the GM family."""
    make, number = ' '.join(str(manufacturer or '').upper().split()), compact(number)
    if make in TRAILING_000_MAKES and len(number) > 10 and number.endswith('000'):
        return number[:-3]
    if make in GM_FAMILY and number.isdigit():
        if len(number) == 8 and number.startswith('0'):
            return number[1:]
        if len(number) == 6 and make in OPEL_CATALOG_MAKES:
            return '0' + number
    return number


def twin_scope(manufacturer):
    """The makes whose numbers can be twins: the GM family shares Opel's catalog numbers, any other make has its own."""
    make = ' '.join(str(manufacturer or '').upper().split())
    return 'GM' if make in GM_FAMILY else make


def variant_key(code, brand=''):
    key = compact(code)
    for pattern, replacement in VARIANT_FOLDS.get(brand, ()):
        key = pattern.sub(replacement, key)
    return key


def one_part(codes, brand=''):
    """The codes name one part: the shortest is a prefix of every other and the rest is at most three letters (GWT-116A, GWT-116AH),
    once the brand's variant codes are folded (ATE 24.0308-0111.1 is the PowerDisc of 24.0108-0111.1)."""
    keys = sorted({variant_key(code, brand) for code in codes}, key=len)
    return all(key.startswith(keys[0]) and re.fullmatch(r'[A-Z]{0,3}', key[len(keys[0]):]) for key in keys)


def fold(text):
    import unicodedata
    return ' '.join(''.join(ch for ch in unicodedata.normalize('NFKD', (text or '').upper()) if not unicodedata.combining(ch)).split())


def compatible(product_type):
    pairs = COMPATIBLE.get(product_type) or ([TAXONOMY[product_type]] if product_type in TAXONOMY else [])
    return {(fold(c), fold(s)) for c, s in pairs}


def citation(catalog, page=None):
    title = f"{catalog['title']}" + (f" ({catalog['year']})" if catalog.get('year') and catalog['year'] not in catalog['title'] else '')
    return (f'{title} · pág. {page}' if page else title)[:500]


def application_text(app):
    years = app.get('years') or ''
    return ' '.join(x for x in [app.get('make'), app.get('model'), app.get('chassis'), app.get('engine'), years] if x).strip()


def spec_value(spec, key):
    """(kind, value) for a printed spec: integer/number as Decimal, boolean, or text; None when unusable."""
    raw = str(spec.get('value', '')).strip()
    if not raw:
        return None
    up = raw.upper()
    if key in TEXT_KEYS:
        return 'text', ' '.join(up.split())[:2000]
    if key in BOOLEAN_KEYS or up in YES | NO and key not in INTEGER_KEYS:
        if up in YES:
            return 'boolean', True
        if up in NO:
            return 'boolean', False
    number = raw.replace(',', '.') if re.fullmatch(r'-?\d+(?:[.,]\d+)?', raw) else None
    if number is not None:
        try:
            value = Decimal(number)
        except InvalidOperation:
            value = None
        if value is not None and value.is_finite():
            if key in INTEGER_KEYS and value == value.to_integral_value() and not spec.get('unit'):
                return 'integer', value
            return 'number', value
    return 'text', ' '.join(up.split())[:2000]


class CatalogImport:
    """One catalog extract against the current catalog: plan() decides everything read-only, apply() writes the plan."""

    def __init__(self, data, finder, indexes):
        self.catalog, self.items, self.finder, self.ix = data['catalog'], data['items'], finder, indexes
        self.fid = {str(k): k for k in finder.by_id}  # str id -> the finder's own key
        self.brand = self.catalog['brand'].strip().upper()
        self.report = {'catalog': self.catalog['key'], 'title': self.catalog['title'], 'items': len(self.items), 'counts': Counter(),
                       'reasons': Counter(), 'samples': defaultdict(list)}
        self._oem_rows, self._shared, self._twins, self.clean = None, None, None, {}

    def shared_numbers(self):
        """Compact OEM numbers, as printed, this catalog prints for two or more of its parts that are not variants of one part. Each
        printed spelling counts on its own, also once the library keeps one spelling of twins: s0_normalize_numbers gave the kept
        number what each spelling had filed (GMB prints 16100-97205-000 for GWD-50AH alone and 16100-97205 for GWD-50AH and
        GWT-134AH, so GWD-50AH's codes are filed under 16100-97205 through the -000 spelling)."""
        if self._shared is None:
            parts = defaultdict(set)
            for item in self.items:
                if (item.get('brand_code') or '').strip():
                    for o in item.get('oem', []):
                        parts[compact(o['code'])].add(item['brand_code'])
            self._shared = {number for number, codes in parts.items() if len(codes) > 1 and not one_part(codes, self.brand)}
            self.report['counts']['shared_numbers'] = len(self._shared)
        return self._shared

    def shared(self, oem):
        """The catalog prints this spelling of the number for other parts too (shared_numbers)."""
        return compact(oem['code']) in self.shared_numbers()

    def note(self, bucket, value):
        if len(self.report['samples'][bucket]) < SAMPLE:
            self.report['samples'][bucket].append(value)

    # ------------------------------------------------------------------ matching
    def item_groups(self, item):
        makes = {o['manufacturer'] for o in item.get('oem', []) if o.get('manufacturer')} | {a['make'] for a in item.get('applications', []) if a.get('make')}
        return {g for g in map(self.make_group, makes) if g}

    def product_ok(self, pid, product_type):
        """(agrees, why, the subgrupo the SKU's own description classifies to; None for a SKU already classified)."""
        from .category_suggestions import description, local_category
        p = self.finder.by_id[self.fid[str(pid)]]
        wanted = compatible(product_type)
        if not wanted:
            return False, 'no_taxonomy', None
        if p.get('subcategory'):
            return ((fold(p['category']), fold(p['subcategory'])) in wanted), 'classified', None
        row = {'sku': p['sku'], 'name': p.get('name') or '', 'description': p['description'], 'category': '', 'subcategory': ''}
        found = local_category(row)
        if not found:
            text = description(row)
            found = next(((c, s) for c, s, pattern in EXTRA_RULES if pattern.search(text)), None)
        if not found:
            return False, 'description_unclassified', None
        return (fold(found[0]), fold(found[1])) in wanted, 'rules', found

    def retired(self, pid):
        from .catalog_suffixes import LIFECYCLE_RE
        return bool(LIFECYCLE_RE.search(self.finder.by_id[self.fid[str(pid)]]['sku']))

    def part_keys(self, pid):
        """Every key that can match this SKU (raw codes and OEM bases), from the shared indexes."""
        cache = self.ix.setdefault('part_keys', {})
        if not cache:
            for name in ('raw', 'oem'):
                for key, pids in self.ix[name].items():
                    for p in pids:
                        cache.setdefault(p, set()).add(key)
        return cache.get(str(pid), set())

    def make_ok(self, pid, groups):
        makes = {g for g in map(self.make_group, self.finder.parsed[self.fid[str(pid)]]['makes']) if g}
        if not makes or not groups:
            return None
        return bool(makes & groups)

    def plan(self):
        rc, ix = self.report['counts'], self.ix
        links_by_part, links_by_item, keys_by_link, oem_by_item = defaultdict(set), defaultdict(set), {}, {}
        self.targets = {}  # (pid, item) -> subgrupo for an unclassified SKU: the one its description names
        for n, item in enumerate(self.items):
            if not (item.get('brand_code') or '').strip():
                rc['oem_only_items'] += 1  # an OEM number the catalog prints without a part of its own: OEM table only
                continue
            oem_keys = oem_by_item[n] = {compact(o['code']) for o in item.get('oem', []) if len(compact(o['code'])) >= 5}
            brand_keys = {k for k in [compact(item['brand_code'])] + [compact(x['code']) for x in item.get('cross_refs', [])]
                          if len(k) >= 5 and re.search(r'\d', k)}
            candidates = {}
            for k in oem_keys:
                for pid in ix['oem'].get(k, set()) | ix['raw'].get(k, set()):
                    candidates.setdefault(pid, 'oem')
            for k in brand_keys:
                for pid in ix['raw'].get(k, set()):
                    candidates.setdefault(pid, 'brand')
            if not candidates:
                rc['items_without_sku'] += 1
                continue
            groups = self.item_groups(item)
            for pid, method in candidates.items():
                if self.retired(pid):  # an -ANULADO / _DELETED SKU would claim the codes and turn the live SKU into a duplicate
                    rc['retired_skus_skipped'] += 1
                    continue
                ok, why, found = self.product_ok(pid, item['product_type'])
                if not ok:
                    self.report['reasons'][f'product:{why}'] += 1
                    self.note('product_mismatch', {'sku': self.finder.by_id[self.fid[str(pid)]]['sku'], 'description': self.finder.by_id[self.fid[str(pid)]]['description'][:80],
                                                   'item': item['brand_code'], 'product_type': item['product_type'], 'why': why})
                    continue
                make = self.make_ok(pid, groups)
                if make is False or make is None and method != 'oem':
                    self.report['reasons']['make_mismatch' if make is False else 'make_unknown_brand_match'] += 1
                    continue
                links_by_part[pid].add(n)
                links_by_item[n].add(pid)
                keys_by_link[(pid, n)] = (oem_keys | brand_keys) & self.part_keys(pid)
                # Matched through the item's own codes or a number only this part of the catalog prints: the item's numbers are the SKU's.
                self.clean[(pid, n)] = bool(keys_by_link[(pid, n)] & (brand_keys | (oem_keys - self.shared_numbers())))
                if found:
                    self.targets[(pid, n)] = found
                rc[f'link_{method}'] += 1
        self.links = []
        for pid, items in links_by_part.items():
            if len(items) > 1:
                # Several items of one product type matched through an OEM number they all print (a catalog listing one part in two
                # make sections, e.g. GMB GWDW-90A and GWG-90A for OEM 96352648): each claims to replace that number, so all link.
                # A shared competitor code is no such proof: ASVA prints SNR R177.27 for two bearings of different sizes.
                shared = set.intersection(*(keys_by_link[(pid, n)] for n in items)) & set.intersection(*(oem_by_item[n] for n in items))
                if not shared or len({self.items[n]['product_type'] for n in items}) > 1:
                    rc['ambiguous_parts'] += 1
                    self.note('ambiguous', {'sku': self.finder.by_id[self.fid[str(pid)]]['sku'], 'items': sorted(self.items[n]['brand_code'] for n in items)})
                    continue
                rc['multi_item_parts'] += 1
            for n in sorted(items):
                self.links.append((pid, n, len(links_by_item[n]) > 1))
        rc['linked_parts'] = len(self.links)
        for pid, n, _ in self.links:
            if not self.clean[(pid, n)]:
                rc['matches_through_shared_numbers'] += 1
                self.note('shared_number_matches', {'sku': self.finder.by_id[self.fid[str(pid)]]['sku'], 'item': self.items[n]['brand_code'],
                                                    'numbers': sorted(keys_by_link[(pid, n)])})
        rc['items_linked'] = len({n for _, n, _ in self.links})
        rc['duplicate_part_items'] = sum(1 for n, pids in links_by_item.items() if len(pids) > 1)
        for n, pids in links_by_item.items():
            if len(pids) > 1:
                self.note('duplicates', {'item': self.items[n]['brand_code'], 'skus': sorted(self.finder.by_id[self.fid[str(p)]]['sku'] for p in pids)})
        return self.report

    # ------------------------------------------------------------------ OEM table
    def number_key(self, oem):
        """(manufacturer, compact number) an OEM number the catalog prints is filed under: as printed, or the twin spelling the library
        holds instead (canonical_number: mall.refactor.s0_normalize_numbers made them one), so an import brings no pair back."""
        key = (oem['manufacturer'].strip().upper(), compact(oem['code']))
        return self.twins().get(key, key)

    def twins(self):
        """{(manufacturer, printed number): (manufacturer, spelling)} for the printed numbers the library does not hold under their make,
        read once per catalog. The make holds a twin: its canonical spelling, else the one it has (a lone 16100-28030-000 takes a printed
        16100-28030). Only another make of the GM family holds one: the make's own canonical spelling (VAUXHALL 636160 is filed as
        0636160 beside OPEL 0636160), the spelling the normalization would rename it to."""
        if self._twins is None:
            from . import references
            from .models import Reference
            from .oem_links import chunks
            from .oem_reference_models import manufacturer_name
            printed, candidates = set(), set()
            for item in self.items:
                for o in item.get('oem', []):
                    key = (o['manufacturer'].strip().upper(), compact(o['code']))
                    canonical = canonical_number(*key)
                    printed.add(key)
                    candidates |= {key[1], canonical, canonical + '000', '0' + canonical, canonical[1:] if canonical.startswith('0') else canonical}
            groups = defaultdict(set)  # (scope, canonical): {(manufacturer, code) held}
            for chunk in chunks(sorted(candidates - {''})):
                for manufacturer, spelling in Reference.objects.filter(references.library_q(), references.compact_q(chunk)).values_list('brand', 'code'):
                    code = compact(spelling)
                    groups[(twin_scope(manufacturer), canonical_number(manufacturer, code))].add((manufacturer, code))
            self._twins = {}
            for make, number in printed:
                canonical, name = canonical_number(make, number), manufacturer_name(make)
                held = groups.get((twin_scope(make), canonical), set())
                if (name, number) in held:
                    continue
                own = sorted(code for manufacturer, code in held if manufacturer == name)
                if canonical in own:
                    self._twins[(make, number)] = (make, canonical)
                elif own:
                    self._twins[(make, number)] = (make, own[0])
                elif held and number != canonical:
                    self._twins[(make, number)] = (make, canonical)
        return self._twins

    def is_assembly(self, manufacturer, code):
        """A drive-shaft assembly number, e.g. Hyundai 49501-2S300 printed next to a CV joint: an OEM finder assembly class under the
        make's own or platform-partner numbering (Mitsubishi prints Hyundai shafts), or a number linked to a SKU sold as a whole shaft
        (build_indexes 'shafts'). Mazda's 25-40X/25-50X/25-60X, under Mazda, Ford or Kia, and Kia's Mazda-era 0K2N1-25-600 name the
        joint (JOINT_CLASSES), and Mitsubishi's MR series carries no function code and names joints too (MR222052 is ASVA MT-08's
        only number, MR-567160-NDK a PUNTA): such a number is a shaft only through a shaft SKU (MR453384)."""
        from .oem_finder import ASSEMBLY_CLASSES, MAKE_SYSTEMS, system_hits
        own, partners = MAKE_SYSTEMS.get(manufacturer, ([], []))
        for h in system_hits(code.strip().upper(), self.finder.table):
            if h['system'] not in own and h['system'] not in partners:
                continue
            if (h['system'], h.get('lexkey')) in ASSEMBLY_CLASSES - JOINT_CLASSES:
                return True
        return compact(code) in self.ix.get('shafts', ())

    def listed_assembly(self, item, oem):
        """The OEM number is a drive-shaft assembly printed next to a CV component, not the component's own number."""
        return (item['product_type'].startswith('cv_') and item['product_type'] != 'cv_axle'
                and self.is_assembly(oem['manufacturer'].strip().upper(), oem['code']))

    def filed_key(self, brand, code):
        """The compact key an aftermarket code is filed under, or '' when it names nothing: a competitor code needs a digit, while the
        catalog's own item code is the item's name and is filed with MIN_BARE_CODE letters (ASVA FDWH-FOCMF, CH-WK)."""
        key = compact(code)
        own = ' '.join(str(brand or '').upper().split()) == self.brand
        return key if re.search(r'\d', key) or own and len(key) >= MIN_BARE_CODE else ''

    def aftermarket_codes(self, item):
        """(brand, code, citation) the item prints as equivalents of its OEM numbers: the catalog's own code and the competitor codes."""
        codes = []
        if (item.get('brand_code') or '').strip():
            codes.append((self.brand, item['brand_code'], citation(self.catalog, (item.get('pages') or [None])[0])))
        for xr in item.get('cross_refs', []):
            if xr.get('brand') and xr.get('code'):
                codes.append((xr['brand'], xr['code'], citation(self.catalog, xr.get('page'))))
        return codes

    def oem_rows(self):
        if self._oem_rows is not None:
            return self._oem_rows
        rows = {}
        for item in self.items:
            pt = TAXONOMY.get(item['product_type'])
            codes = self.aftermarket_codes(item)
            for o in item.get('oem', []):
                key = self.number_key(o)
                if not key[0] or not 1 <= len(key[1]) <= 60:
                    continue
                assembly = self.listed_assembly(item, o)
                # catalog: the extract that printed the number first, when several extracts of one catalog are replaced together;
                # component: the description and part type the first item gives a number that is not a shaft's.
                component = (item.get('name_es') or '', pt[1] if pt else '')
                row = rows.setdefault(key, {'printed': set(), 'pages': set(), 'brand_codes': set(), 'product_types': set(), 'assembly': assembly,
                                            'catalog': item.get('_extract') or self.catalog['key'], 'component': component,
                                            'description': ASSEMBLY_TEXT[0] if assembly else component[0],
                                            'part_type': ASSEMBLY_TEXT[1] if assembly else component[1], 'apps': [], 'cross': {}})
                row['printed'].add(o['code'].strip().upper())
                row['pages'].add(o.get('page'))
                if item.get('brand_code'):
                    row['brand_codes'].add(item['brand_code'])
                row['product_types'].add(item['product_type'])
                for a in item.get('applications', []):
                    text = application_text(a)
                    if text and text not in row['apps'] and len(row['apps']) < 8:
                        row['apps'].append(text)
                if assembly:  # the component's codes do not replace the whole drive shaft
                    continue
                if self.shared(o):
                    self.report['counts']['cross_reference_shared_skipped'] += len(codes)
                    continue
                for brand, code, cite in codes:
                    row['cross'].setdefault((brand, code), set()).add(cite)
        self._oem_rows = rows
        return rows

    def write_oem_table(self, apply):
        """Every number the catalog prints into the library (mall.references.add_library_number): merged into the number it has, or put
        on the line that carries it, or on a new line in referencia."""
        from . import references
        rows, rc = self.oem_rows(), self.report['counts']
        existing = set(self.library_ids(set(rows)))
        rc['oem_numbers'] = len(rows)
        rc['oem_new'] = sum(1 for key in rows if key not in existing)
        if not apply:
            return
        cite = citation(self.catalog)
        for (manufacturer, number), row in sorted(rows.items()):
            try:
                references.add_library_number(
                    manufacturer, number, printed=sorted(row['printed']), source_kind='aftermarket_catalog', citation=cite,
                    source_detail={'catalog': row['catalog'], 'brand': self.brand, 'brand_codes': sorted(row['brand_codes']),
                                   'pages': sorted(p for p in row['pages'] if p), 'product_types': sorted(row['product_types']),
                                   **({'assembly_number_listed_for_component': True} if row['assembly'] else {})},
                    description=row['description'][:300], part_type=row['part_type'], applications='; '.join(row['apps']))
            except references.ReferenceConflict as error:
                self.report['reasons'][f'oem_invalid:{error.message}'] += 1

    def library_ids(self, keys):
        """{(manufacturer, compact code): number id} of the numbers already in the library (its References' ids; manufacturers compared
        as stored)."""
        from . import references
        from .models import Reference
        from .oem_reference_models import manufacturer_name
        from .oem_links import chunks
        wanted, found = {(manufacturer_name(manufacturer), number): (manufacturer, number) for manufacturer, number in keys}, {}
        # By the compact number, as the library keys it: a short number's clave is its printed spelling (OPEL 5 69 036).
        for chunk in chunks(sorted({number for _, number in keys})):
            for pk, manufacturer, printed in (Reference.objects.filter(references.library_q(), references.compact_q(chunk))
                                              .order_by(references.exclusive_first(), 'pk').values_list('pk', 'brand', 'code')):
                key = (manufacturer, compact(printed))
                if key in wanted and wanted[key] not in found:
                    found[wanted[key]] = pk
        return found

    def write_cross_references(self, apply):
        """Every aftermarket code under each OEM number of its item, whether or not a SKU carries the number. A dry run counts the codes
        of a number the library does not hold yet as new."""
        from .oem_links import store_cross_references
        rows, rc = self.oem_rows(), self.report['counts']
        refs = self.library_ids(set(rows))
        entries, pending, bare = {}, set(), set()
        for key, row in rows.items():
            for (brand, code), cites in row['cross'].items():
                filed = self.filed_key(brand, code)
                if filed and not re.search(r'\d', filed):
                    bare.add((' '.join(brand.upper().split()), filed))
                if key in refs:
                    entries[(refs[key], brand, code)] = sorted(cites)
                elif filed:
                    pending.add((key, brand.strip().upper(), filed))
        counts = store_cross_references(entries, apply=apply, bare=bare)
        rc['cross_reference_new'] = counts['new'] + len(pending)
        rc['cross_reference_present'] = counts['present'] + counts['cited']

    # ------------------------------------------------------------------ alternos and technical sheets
    def owned_elsewhere(self, code, pid):
        key, pid = compact(code), str(pid)
        owners = self.ix['codes'].get(key, set()) | self.ix['skus'].get(key, set())
        claimed = self.ix['claims'].get(key)
        return bool(owners - {pid}) or (claimed is not None and claimed != pid)

    def add_code(self, pid, code, brand, ref_type, cite, apply, bucket):
        from . import references
        pid, code, brand, rc = str(pid), code.strip().upper(), brand.strip().upper(), self.report['counts']
        if not code or len(code) > 120:
            return
        key = compact(code)
        if pid in self.ix['codes'].get(key, set()) or pid in self.ix['skus'].get(key, set()):
            rc[f'{bucket}_present'] += 1
            return
        if self.owned_elsewhere(code, pid):
            rc[f'{bucket}_conflict'] += 1
            self.note(f'{bucket}_conflict', {'sku': self.finder.by_id[self.fid[str(pid)]]['sku'], 'code': code, 'brand': brand})
            return
        if apply:
            try:
                references.add(pid, code, brand, ref_type=ref_type, source=cite)
            except references.ReferenceConflict:  # another line holds the key (mall.references.validate): reported, never moved
                rc[f'{bucket}_conflict'] += 1
                self.note(f'{bucket}_conflict', {'sku': self.finder.by_id[self.fid[str(pid)]]['sku'], 'code': code, 'brand': brand})
                return
            self.ix['codes'].setdefault(key, set()).add(pid)
        self.ix['claims'][key] = pid
        rc[f'{bucket}_new'] += 1
        self.note(f'{bucket}_new', {'sku': self.finder.by_id[self.fid[str(pid)]]['sku'], 'code': code, 'brand': brand})

    def sheet(self, part, item, apply, target=None):
        """Classify an unclassified SKU into target (the subgrupo its description names, else the item's), add missing template fields
        and store the absent values. A SKU already in any subgrupo the item's product type accepts keeps it."""
        from .technical_data import bind_part_types, grupo_nombre
        from .technical_models import PartSpecification, TechnicalField, TechnicalTemplate
        from .technical_api import specification
        rc, target = self.report['counts'], target or TAXONOMY.get(item['product_type'])
        specs = [s for s in item.get('specs', []) if s.get('key') and s['key'] not in SKIP_KEYS and str(s.get('value', '')).strip()]
        if not specs or not target:
            return
        if part.part_type_id:
            if (fold(grupo_nombre(part.part_type)), fold(part.part_type.name)) not in compatible(item['product_type']):
                rc['specs_other_subgroup'] += 1
                return
        elif part.category or part.subcategory:
            rc['specs_partial_classification'] += 1
            return
        else:
            rc['subgroups_assigned'] += 1
            self.note('subgroup_assigned', {'sku': part.sku, 'subgroup': f'{target[0]} / {target[1]}'})
            if not apply:
                rc['specs_new'] += len({s['key'] for s in specs})
                return
            part.category, part.subcategory = target
            bind_part_types([part])
            part.save(update_fields=['category', 'subcategory', 'part_type'])
        # A dry run reads the template and the stored values too, so a re-run reports present values instead of new ones.
        template = (TechnicalTemplate.objects.select_for_update().get(pk=part.part_type_id) if apply
                    else TechnicalTemplate.objects.filter(pk=part.part_type_id).first())
        fields = {f.key: f for f in template.fields.all()} if template else {}
        stored = {s.field_id: s for s in PartSpecification.objects.filter(part=part)}
        created, grew, pending = [], False, set()
        for spec in specs:
            key = re.sub(r'[^a-z0-9_]', '_', spec['key'].strip().lower())[:60].strip('_')
            parsed = spec_value(spec, key)
            if not key or not parsed:
                continue
            kind, value = parsed
            bounds = SPEC_RANGES_BY_TYPE.get((item['product_type'], key)) or SPEC_RANGES.get(key)
            if bounds and kind in ('number', 'integer') and not bounds[0] <= value <= bounds[1]:
                rc['specs_out_of_range'] += 1
                self.note('specs_out_of_range', {'sku': part.sku, 'field': key, 'value': str(value), 'range': list(bounds)})
                continue
            unit = (spec.get('unit') or '').strip() if kind == 'number' else ''
            field = fields.get(key)
            if field is None and not apply:
                if key not in pending:  # the field itself is new: nothing stored to compare with
                    pending.add(key)
                    rc['specs_new'] += 1
                continue
            if field is None:
                field = TechnicalField.objects.create(template=template, key=key, label=(spec.get('label_es') or key.replace('_', ' ')).upper()[:120],
                                                      section='MEDIDAS' if kind in ('number', 'integer') else 'CARACTERÍSTICAS', kind=kind, unit=unit,
                                                      position=len(fields))
                fields[key], grew = field, True
            if field.kind != kind and not (field.kind == 'number' and kind == 'integer'):
                rc['specs_kind_mismatch'] += 1
                continue
            payload = {'value': value if kind == 'boolean' else str(value), 'source': citation(self.catalog, spec.get('page'))}
            if field.kind == 'number' and unit:
                payload['unit'] = unit
            try:
                row = specification(field, payload, part)
            except Exception:
                rc['specs_invalid'] += 1
                continue
            if row is None:
                continue
            old = stored.get(field.pk)
            if old is not None:
                same = (old.number_value, old.boolean_value, old.text_value) == (row.number_value, row.boolean_value, row.text_value)
                rc['specs_present' if same else 'specs_conflict'] += 1
                if not same:
                    self.note('specs_conflict', {'sku': part.sku, 'field': key, 'stored': str(old.number_value or old.boolean_value or old.text_value),
                                                 'catalog': str(value)})
                continue
            created.append(row)
            stored[field.pk] = row
        if created:
            rc['specs_new'] += len(created)
            if apply:
                PartSpecification.objects.bulk_create(created)
                part.technical_revision += 1
                part.save(update_fields=['technical_revision'])
        if grew:
            template.revision += 1
            template.save(update_fields=['revision', 'updated_at'])
            rc['template_fields_added'] += 1

    def write_links(self, apply, codes=True):
        """codes=False only collects the catalog links (mall.supplier_catalog_replace): no alternos, measurements or subgrupos."""
        from django.db import transaction
        from .models import Part
        self.catalog_links = defaultdict(set)  # (pid, (manufacturer, number)) -> citations, written by write_catalog_links
        for pid, n, duplicate in self.links:
            item = self.items[n]
            pages = item.get('pages') or [None]
            cite = citation(self.catalog, pages[0])
            with transaction.atomic():
                # Lock the SKU row alone: PostgreSQL cannot lock the nullable side of the part_type join; part_type loads lazily.
                part = (Part.objects.select_for_update() if apply else Part.objects).get(pk=pid)
                if not part.active or part.merged_into_id:
                    continue
                carried = self.link_numbers(pid, n, item, cite)
                if not codes:
                    continue
                if carried:  # the item's codes reach the SKU through those numbers: no copy on the SKU
                    self.report['counts']['codes_carried_by_numbers'] += 1
                elif not self.clean.get((pid, n)):
                    pass  # matched only through numbers the catalog prints for other parts too: reported, nothing written
                elif not duplicate:  # a code has one owner: duplicate SKUs of one item need Agrupar SKU first
                    self.add_code(pid, item['brand_code'], self.brand, 'company', cite, apply, 'brand_code')
                    for xr in item.get('cross_refs', []):
                        if xr.get('brand') and xr.get('code'):
                            self.add_code(pid, xr['code'], xr['brand'], 'company', citation(self.catalog, xr.get('page')), apply, 'cross_ref')
                if not duplicate:
                    if part.is_OEM:  # sister OEM numbers; reconcile_identities never touches a main already marked OEM
                        groups = {g for g in map(self.make_group, self.finder.parsed[self.fid[str(pid)]]['makes']) if g}
                        for o in item.get('oem', []):
                            if not groups or self.make_group(o['manufacturer']) in groups:
                                self.add_code(pid, o['code'], o['manufacturer'], 'oem', citation(self.catalog, o.get('page')), apply, 'oem_alterno')
                self.sheet(part, item, apply, self.targets.get((pid, n)))

    def link_numbers(self, pid, n, item, cite):
        """Tie the SKU to the OEM numbers its item prints, duplicates of one item included (the numbers have no single owner), when it
        matched the item cleanly; never to a number the catalog prints for other parts too, one the SKU's makes rule out or an
        assembly number listed for a component."""
        if not self.clean.get((pid, n)):
            return 0
        groups, tied = {g for g in map(self.make_group, self.finder.parsed[self.fid[str(pid)]]['makes']) if g}, 0
        for o in item.get('oem', []):
            key = self.number_key(o)
            if not key[0] or not 1 <= len(key[1]) <= 60 or self.shared(o) or self.listed_assembly(item, o):
                continue
            if not groups or self.make_group(o['manufacturer']) in groups:
                self.catalog_links[(str(pid), key)].add(f"{cite} · {item['brand_code']}")
                tied += 1
        return tied

    def write_catalog_links(self, apply):
        from .oem_links import store_catalog_links
        rc = self.report['counts']
        refs = self.library_ids({key for _, key in self.catalog_links})
        entries, pending = {}, 0
        for (pid, key), cites in self.catalog_links.items():
            if key in refs:
                entries[(pid, refs[key])] = sorted(cites)
            else:
                pending += 1  # dry run: the number itself is new
        counts = store_catalog_links(entries, apply=apply)
        rc['catalog_link_new'] = counts['new'] + pending
        rc['catalog_link_present'] = counts['present'] + counts['cited']

    @staticmethod
    def make_group(make):
        """The OEM finder's make group for a catalog manufacturer or a SKU make (mall.oem_links.make_group)."""
        from .oem_links import make_group
        return make_group(make)


def catalog_citations(datasets=()):
    """Citations of every supplier catalog, imported before or in this run. The alternos a catalog wrote cite it, and they never act
    as matching keys: otherwise a re-run, or the next catalog, would chain one catalog's competitor codes into links of its own (SNR
    R177.27, printed for two different ASVA bearings, would hand the second bearing's measurements to the first one's SKU)."""
    from django.db.models import Q
    from .models import ReferenceSource
    cited = set(ReferenceSource.objects.filter(kind='catalogo').filter(Q(detail__legacy_kind='aftermarket_catalog') | ~Q(detail__has_key='legacy_kind'))
                .values_list('citation', flat=True).distinct())
    return tuple(sorted({c for c in cited | {citation(d['catalog']) for d in datasets} if c}))


def build_indexes(finder, imported=()):
    """Matching keys (raw, oem) from the catalog's own data only, never from alternos citing a supplier catalog (imported); the
    ownership indexes (codes, skus) hold every code so an imported alterno still counts as present or as owned."""
    from .lines import all_lines
    from .models import Reference
    raw, oem, codes, skus = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    from .catalog_suffixes import clean_code

    def own(ref):
        return not (ref or '').startswith(imported) if imported else True

    for p in finder.learn:
        pid, P = p['id'], finder.parsed[p['id']]
        keys = {compact(p['sku'])} | {compact(s) for s in P['segs']}
        keys |= {compact(c['code']) for c in finder.codes_by_part.get(pid, []) if own(c.get('reference_source'))}
        for it in finder.items_by_part.get(pid, []):
            keys.add(compact(it['codigo']))
            keys |= {compact(s) for s in clean_code(it['codigo']).replace('_', '-').split(' ') if s}
        for k in keys - {''}:
            raw[k].add(str(pid))
        for it in P['interps']:
            if it['source'] == 'alterno' and not own(it.get('reference_source')):
                continue
            if it['source'] in ('sku', 'sku_prefixed', 'sku_segment', 'alterno', 'codigo', 'supplier_reference') and it.get('key'):
                oem[it['key']].add(str(pid))
    # Finder snapshot ids and ORM ids differ in type (str vs UUID): every index keys parts by str(id). A line in referencia (a library
    # number no product carries, S4) owns nothing: a code of it a SKU takes absorbs that line (rule 3f).
    for pid, code in Reference.objects.exclude(part__estado='referencia').values_list('part_id', 'code'):
        codes[compact(code)].add(str(pid))
    for pid, sku in all_lines().exclude(estado='referencia').values_list('pk', 'sku'):  # a line in any other estado holds its codigo
        skus[compact(sku)].add(str(pid))
    return {'raw': raw, 'oem': oem, 'codes': codes, 'skus': skus, 'claims': {}, 'shafts': shaft_numbers(finder.table)}


def shaft_numbers(table=None):
    """Compact OEM numbers of whole drive shafts: those a live SKU sold as one (EJE COMPLETO, SEMIEJE, subgrupo SEMIEJES) names with
    its own code, carries as an OEM alterno or is linked to. A CV catalog lists them for its joints, but the joint does not replace
    the shaft (is_assembly)."""
    from .lines import storefront_lines
    from .models import Reference
    from .oem_links import chunks, name_key
    from .oem_reference_models import PartOEMLink
    shafts, numbers = [], set()
    for pk, sku, description, subcategory in storefront_lines().values_list('pk', 'sku', 'description', 'subcategory'):
        if SHAFT_RE.search(fold(description)) or fold(subcategory) == 'SEMIEJES':
            shafts.append(pk)
            numbers.add(name_key(sku, table))
    for chunk in chunks(shafts):
        numbers.update(compact(code) for code in PartOEMLink.objects.filter(part_id__in=chunk, reference__isnull=False).values_list('reference__code', flat=True))
        numbers.update(compact(code) for code in Reference.objects.filter(part_id__in=chunk, ref_type='oem').values_list('code', flat=True))
    return numbers - {''}


def run(paths, *, apply=False, report_path=None, stdout=None):
    from .catalog_suffixes import suffix_table
    from .oem_finder import OEMFinder, load_snapshot, matching_lock
    from .oem_links import refresh_name_links
    stdout = stdout or sys.stdout
    started = time.monotonic()
    datasets = [json.load(open(path, encoding='utf-8')) for path in paths]

    def execute():
        finder = OEMFinder(load_snapshot(), suffix_table())
        indexes = build_indexes(finder, catalog_citations(datasets))
        reports = []
        for data in datasets:
            job = CatalogImport(data, finder, indexes)
            job.plan()
            job.write_oem_table(apply)
            job.write_cross_references(apply)
            job.write_links(apply)
            job.write_catalog_links(apply)
            report = job.report
            report['counts'] = dict(report['counts'])
            report['reasons'] = dict(report['reasons'])
            report['samples'] = dict(report['samples'])
            reports.append(report)
            c = report['counts']
            stdout.write(f"{'Aplicado' if apply else 'Simulación'} · {report['title']} · {report['items']} artículos · "
                         f"OEM {c.get('oem_numbers', 0)} ({c.get('oem_new', 0)} nuevos) · SKU vinculados {c.get('linked_parts', 0)} · "
                         f"alternos de marca {c.get('brand_code_new', 0)} (+{c.get('cross_ref_new', 0)} de competidores, "
                         f"{c.get('brand_code_conflict', 0) + c.get('cross_ref_conflict', 0)} en conflicto) · alternos OEM {c.get('oem_alterno_new', 0)} · "
                         f"subgrupos asignados {c.get('subgroups_assigned', 0)} · medidas {c.get('specs_new', 0)} "
                         f"({c.get('specs_conflict', 0)} en conflicto) · equivalencias de posventa {c.get('cross_reference_new', 0)} nuevas · "
                         f"vínculos de SKU por catálogo {c.get('catalog_link_new', 0)} nuevos\n")
        # The name links are retired with S4: a number a SKU's codigo names sits on that SKU, or rule 3 proposes it (S6).
        return reports, refresh_name_links(apply=apply)

    if apply:
        with matching_lock() as acquired:
            if not acquired:
                stdout.write('El análisis de coincidencias está en curso; vuelve a intentarlo cuando termine.\n')
                return None
            reports, names = execute()
    else:
        reports, names = execute()
    result = {'mode': 'apply' if apply else 'dry_run', 'seconds': round(time.monotonic() - started, 1), 'catalogs': reports, 'name_links': names}
    if report_path:
        with open(report_path, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=1, default=str)
    return result


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Importa extractos normalizados de catálogos PDF: tabla OEM, alternos y fichas técnicas.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula todo sin escribir nada.')
    mode.add_argument('--apply', action='store_true', help='Escribe la tabla OEM, los alternos y las medidas.')
    parser.add_argument('files', nargs='+', help='Extractos JSON normalizados.')
    parser.add_argument('--report', help='Escribe el informe JSON en esta ruta.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .refactor.guard import require_freeze
    require_freeze(options.apply)
    result = run(options.files, apply=options.apply, report_path=options.report, stdout=stdout)
    sys.exit(0 if result is not None else 1)


if __name__ == '__main__':
    main()
