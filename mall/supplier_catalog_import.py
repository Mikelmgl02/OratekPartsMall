"""Supplier catalog import: normalized PDF-catalog extracts -> OEM table, company alternos and technical sheets.

    python -m mall.supplier_catalog_import --dry-run|--apply FILE [FILE ...] [--report PATH]

FILE follows the normalized extract schema (catalog + items with brand_code, product_type, oem, cross_refs, applications, specs).
Every OEM number a catalog prints becomes, or joins, an OEMReference with one aftermarket_catalog source per catalog (status
declared), whether or not a SKU carries it. A catalog item is linked to a SKU only when one of its numbers equals a code of that SKU
(the SKU, its OEM base, alternos or supplier codes), the SKU description classifies to the item's product type with the deterministic
category rules, and the makes agree. A linked SKU receives the catalog's own code and its competitor codes as company alternos and,
when the item prints measurements, its subgrupo template gains those fields and the SKU gets the values. OEM numbers become OEM
alternos only on SKUs already marked OEM: on any other SKU reconcile_identities would promote a sole sourced OEM alterno to MAIN
outside the OEM finder's canary and review. Nothing is overwritten: a code owned by another SKU, a different stored measurement or
another subgrupo is reported and left alone. Re-running a file changes nothing.
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
    'idler_pulley': [('MOTOR', 'TENSORES DE CORREA'), ('RODAMIENTOS', 'RODAMIENTOS DE TENSOR')],
    'wheel_hub': [('RODAMIENTOS', 'CUBOS DE RUEDA'), ('RODAMIENTOS', 'RODAMIENTOS DE RUEDA')],
    'hub_bearing': [('RODAMIENTOS', 'RODAMIENTOS DE RUEDA'), ('RODAMIENTOS', 'CUBOS DE RUEDA')],
}
# Product families the shared category rules do not cover yet: used only when local_category finds no rule for a description.
EXTRA_RULES = [(c, s, re.compile(pattern)) for c, s, pattern in [
    ('REFRIGERACIÓN', 'EMBRAGUES DE VENTILADOR', r'^(?:FAN CLUTCH|EMBRAGUE (?:DE )?VENT(?:ILADOR)?|ACOPLE (?:DE )?VENTILADOR)\b'),
]]
# Codes and ambiguous printed columns stay text even when they look numeric (ASVA boot codes such as 2071).
TEXT_KEYS = {'cv_boot', 'boot_code', 'joint_type', 'note', 'design', 'type', 'splines_or_diameter'}
# Plausible ranges for printed measurements: a value outside (a catalog typo such as a 630 mm seal) is reported, never stored.
SPEC_RANGES = {'seal_diameter': (15, 130), 'outer_splines': (10, 60), 'inner_splines': (10, 60), 'splines': (10, 60), 'abs_teeth': (20, 120),
               'length': (50, 1500), 'big_diameter': (20, 250), 'small_diameter': (8, 120), 'inner_diameter': (3, 200),
               'outer_diameter': (5, 400), 'holes': (2, 12), 'bolts': (2, 12)}
INTEGER_KEYS = {'holes', 'bolts', 'outer_splines', 'inner_splines', 'abs_teeth', 'teeth', 'splines', 'quantity', 'pieces'}
BOOLEAN_KEYS = {'vented', 'wear_indicator', 'abs', 'with_abs', 'with_sensor'}
YES, NO = {'YES', 'SI', 'SÍ', 'TRUE', 'Y', '+', 'X'}, {'NO', 'FALSE', 'N', '-'}
SAMPLE = 40


def compact(value):
    return re.sub(r'[^0-9A-Z]', '', (value or '').upper())


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

    def note(self, bucket, value):
        if len(self.report['samples'][bucket]) < SAMPLE:
            self.report['samples'][bucket].append(value)

    # ------------------------------------------------------------------ matching
    def item_groups(self, item):
        from .oem_finder import MAKE_GROUP
        makes = {o['manufacturer'] for o in item.get('oem', []) if o.get('manufacturer')} | {a['make'] for a in item.get('applications', []) if a.get('make')}
        return {MAKE_GROUP.get(m.upper(), m.upper()) for m in makes}

    def product_ok(self, pid, product_type):
        from .category_suggestions import description, local_category
        p = self.finder.by_id[self.fid[str(pid)]]
        wanted = compatible(product_type)
        if not wanted:
            return False, 'no_taxonomy'
        if p.get('subcategory'):
            return ((fold(p['category']), fold(p['subcategory'])) in wanted), 'classified'
        row = {'sku': p['sku'], 'name': p.get('name') or '', 'description': p['description'], 'category': '', 'subcategory': ''}
        found = local_category(row)
        if not found:
            text = description(row)
            found = next(((c, s) for c, s, pattern in EXTRA_RULES if pattern.search(text)), None)
        if not found:
            return False, 'description_unclassified'
        return (fold(found[0]), fold(found[1])) in wanted, 'rules'

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
        from .oem_finder import MAKE_GROUP
        makes = self.finder.parsed[self.fid[str(pid)]]['makes']
        if not makes or not groups:
            return None
        return bool({MAKE_GROUP.get(m, m) for m in makes} & groups)

    def plan(self):
        rc, ix = self.report['counts'], self.ix
        links_by_part, links_by_item, keys_by_link = defaultdict(set), defaultdict(set), {}
        for n, item in enumerate(self.items):
            if not (item.get('brand_code') or '').strip():
                rc['oem_only_items'] += 1  # an OEM number the catalog prints without a part of its own: OEM table only
                continue
            oem_keys = {compact(o['code']) for o in item.get('oem', []) if len(compact(o['code'])) >= 5}
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
                ok, why = self.product_ok(pid, item['product_type'])
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
                rc[f'link_{method}'] += 1
        self.links = []
        for pid, items in links_by_part.items():
            if len(items) > 1:
                # Several items of one product type matched through the same number (a catalog listing one part in two make
                # sections, e.g. GMB GWDW-90A and GWG-90A for OEM 96352648): each claims to replace that number, so all link.
                shared = set.intersection(*(keys_by_link[(pid, n)] for n in items))
                if not shared or len({self.items[n]['product_type'] for n in items}) > 1:
                    rc['ambiguous_parts'] += 1
                    self.note('ambiguous', {'sku': self.finder.by_id[self.fid[str(pid)]]['sku'], 'items': sorted(self.items[n]['brand_code'] for n in items)})
                    continue
                rc['multi_item_parts'] += 1
            for n in sorted(items):
                self.links.append((pid, n, len(links_by_item[n]) > 1))
        rc['linked_parts'] = len(self.links)
        rc['items_linked'] = len({n for _, n, _ in self.links})
        rc['duplicate_part_items'] = sum(1 for n, pids in links_by_item.items() if len(pids) > 1)
        for n, pids in links_by_item.items():
            if len(pids) > 1:
                self.note('duplicates', {'item': self.items[n]['brand_code'], 'skus': sorted(self.finder.by_id[p]['sku'] for p in pids)})
        return self.report

    # ------------------------------------------------------------------ OEM table
    def is_assembly(self, manufacturer, code):
        """A drive-shaft assembly number (OEM finder ASSEMBLY_CLASSES), e.g. Hyundai 49501-2S300 printed next to a CV joint."""
        from .oem_finder import ASSEMBLY_CLASSES, MAKE_SYSTEMS, system_hits
        own = set(MAKE_SYSTEMS.get(manufacturer, ([], []))[0])
        return any((h['system'], h.get('lexkey')) in ASSEMBLY_CLASSES for h in system_hits(code.strip().upper(), self.finder.table) if h['system'] in own)

    def oem_rows(self):
        rows = {}
        for item in self.items:
            pt = TAXONOMY.get(item['product_type'])
            for o in item.get('oem', []):
                key = (o['manufacturer'].strip().upper(), compact(o['code']))
                if not key[0] or not 1 <= len(key[1]) <= 60:
                    continue
                assembly = item['product_type'].startswith('cv_') and item['product_type'] != 'cv_axle' and self.is_assembly(key[0], o['code'])
                row = rows.setdefault(key, {'printed': set(), 'pages': set(), 'brand_codes': set(), 'product_types': set(), 'assembly': assembly,
                                            'description': 'SEMIEJE (EJE COMPLETO)' if assembly else item.get('name_es') or '',
                                            'part_type': 'SEMIEJES' if assembly else pt[1] if pt else '', 'apps': []})
                row['printed'].add(o['code'].strip().upper())
                row['pages'].add(o.get('page'))
                if item.get('brand_code'):
                    row['brand_codes'].add(item['brand_code'])
                row['product_types'].add(item['product_type'])
                for a in item.get('applications', []):
                    text = application_text(a)
                    if text and text not in row['apps'] and len(row['apps']) < 8:
                        row['apps'].append(text)
        return rows

    def write_oem_table(self, apply):
        from .oem_reference import upsert_reference
        from .oem_reference_models import OEMReference
        rows, rc = self.oem_rows(), self.report['counts']
        existing = set(OEMReference.objects.filter(code__in=[k for _, k in rows]).values_list('manufacturer', 'code'))
        rc['oem_numbers'] = len(rows)
        rc['oem_new'] = sum(1 for key in rows if key not in existing)
        if not apply:
            return
        cite = citation(self.catalog)
        for (manufacturer, number), row in sorted(rows.items()):
            try:
                upsert_reference(manufacturer, number, printed=sorted(row['printed']), source_kind='aftermarket_catalog', citation=cite,
                                 detail={'catalog': self.catalog['key'], 'brand': self.brand, 'brand_codes': sorted(row['brand_codes']),
                                         'pages': sorted(p for p in row['pages'] if p), 'product_types': sorted(row['product_types']),
                                         **({'assembly_number_listed_for_component': True} if row['assembly'] else {})},
                                 defaults={'description': row['description'][:300], 'part_type': row['part_type'],
                                           'applications': '; '.join(row['apps'])})
            except ValueError as error:
                self.report['reasons'][f'oem_invalid:{error}'] += 1

    # ------------------------------------------------------------------ alternos and technical sheets
    def owned_elsewhere(self, code, pid):
        key, pid = compact(code), str(pid)
        owners = self.ix['codes'].get(key, set()) | self.ix['skus'].get(key, set())
        claimed = self.ix['claims'].get(key)
        return bool(owners - {pid}) or (claimed is not None and claimed != pid)

    def add_code(self, pid, code, brand, ref_type, cite, apply, bucket):
        from .models import PartCode
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
        self.ix['claims'][key] = pid
        rc[f'{bucket}_new'] += 1
        self.note(f'{bucket}_new', {'sku': self.finder.by_id[self.fid[str(pid)]]['sku'], 'code': code, 'brand': brand})
        if apply:
            PartCode.objects.create(part_id=pid, code=code, brand=brand, ref_type=ref_type, reference_source=cite)
            self.ix['codes'].setdefault(key, set()).add(pid)

    def sheet(self, part, item, apply):
        """Classify an unclassified SKU into the item's subgrupo, add missing template fields and store the absent values."""
        from .technical_data import bind_part_types
        from .technical_models import PartSpecification, TechnicalField, TechnicalTemplate
        from .technical_api import specification
        rc, target = self.report['counts'], TAXONOMY.get(item['product_type'])
        specs = [s for s in item.get('specs', []) if s.get('key') and str(s.get('value', '')).strip()]
        if not specs or not target:
            return
        if part.part_type_id:
            if (fold(part.part_type.category), fold(part.part_type.name)) != (fold(target[0]), fold(target[1])):
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
        template = TechnicalTemplate.objects.select_for_update().get(pk=part.part_type_id) if apply else None
        fields = {f.key: f for f in template.fields.all()} if template else {}
        stored = {s.field_id: s for s in PartSpecification.objects.filter(part=part)} if apply else {}
        created, grew = [], False
        for spec in specs:
            key = re.sub(r'[^a-z0-9_]', '_', spec['key'].strip().lower())[:60].strip('_')
            parsed = spec_value(spec, key)
            if not key or not parsed:
                continue
            kind, value = parsed
            bounds = SPEC_RANGES.get(key)
            if bounds and kind in ('number', 'integer') and not bounds[0] <= value <= bounds[1]:
                rc['specs_out_of_range'] += 1
                self.note('specs_out_of_range', {'sku': part.sku, 'field': key, 'value': str(value), 'range': list(bounds)})
                continue
            unit = (spec.get('unit') or '').strip() if kind == 'number' else ''
            field = fields.get(key)
            if not apply:
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
            PartSpecification.objects.bulk_create(created)
            part.technical_revision += 1
            part.save(update_fields=['technical_revision'])
            rc['specs_new'] += len(created)
        if grew:
            template.revision += 1
            template.save(update_fields=['revision', 'updated_at'])
            rc['template_fields_added'] += 1

    def write_links(self, apply):
        from django.db import transaction
        from .models import Part
        for pid, n, duplicate in self.links:
            item = self.items[n]
            pages = item.get('pages') or [None]
            cite = citation(self.catalog, pages[0])
            with transaction.atomic():
                # Lock the SKU row alone: PostgreSQL cannot lock the nullable side of the part_type join; part_type loads lazily.
                part = (Part.objects.select_for_update() if apply else Part.objects).get(pk=pid)
                if not part.active or part.merged_into_id:
                    continue
                if not duplicate:  # a code has one owner: duplicate SKUs of one item need Agrupar SKU first
                    self.add_code(pid, item['brand_code'], self.brand, 'company', cite, apply, 'brand_code')
                    for xr in item.get('cross_refs', []):
                        if xr.get('brand') and xr.get('code'):
                            self.add_code(pid, xr['code'], xr['brand'], 'company', citation(self.catalog, xr.get('page')), apply, 'cross_ref')
                    if part.is_OEM:  # sister OEM numbers; reconcile_identities never touches a main already marked OEM
                        groups = {g for g in [self.make_group(m) for m in self.finder.parsed[self.fid[str(pid)]]['makes']] if g}
                        for o in item.get('oem', []):
                            if not groups or self.make_group(o['manufacturer']) in groups:
                                self.add_code(pid, o['code'], o['manufacturer'], 'oem', citation(self.catalog, o.get('page')), apply, 'oem_alterno')
                self.sheet(part, item, apply)

    @staticmethod
    def make_group(make):
        from .oem_finder import MAKE_GROUP
        make = (make or '').strip().upper()
        return MAKE_GROUP.get(make, make)


def build_indexes(finder):
    from .models import Part, PartCode
    raw, oem, codes, skus = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    from .catalog_suffixes import clean_code
    for p in finder.learn:
        pid, P = p['id'], finder.parsed[p['id']]
        keys = {compact(p['sku'])} | {compact(s) for s in P['segs']}
        keys |= {compact(c['code']) for c in finder.codes_by_part.get(pid, [])}
        for it in finder.items_by_part.get(pid, []):
            keys.add(compact(it['codigo']))
            keys |= {compact(s) for s in clean_code(it['codigo']).replace('_', '-').split(' ') if s}
        for k in keys - {''}:
            raw[k].add(str(pid))
        for it in P['interps']:
            if it['source'] in ('sku', 'sku_prefixed', 'sku_segment', 'alterno', 'codigo', 'supplier_reference') and it.get('key'):
                oem[it['key']].add(str(pid))
    # Finder snapshot ids and ORM ids differ in type (str vs UUID): every index keys parts by str(id).
    for pid, code in PartCode.objects.values_list('part_id', 'code'):
        codes[compact(code)].add(str(pid))
    for pid, sku in Part.objects.values_list('pk', 'sku'):
        skus[compact(sku)].add(str(pid))
    return {'raw': raw, 'oem': oem, 'codes': codes, 'skus': skus, 'claims': {}}


def run(paths, *, apply=False, report_path=None, stdout=None):
    from .catalog_suffixes import suffix_table
    from .oem_finder import OEMFinder, load_snapshot, matching_lock
    stdout = stdout or sys.stdout
    started = time.monotonic()
    datasets = [json.load(open(path, encoding='utf-8')) for path in paths]

    def execute():
        finder = OEMFinder(load_snapshot(), suffix_table())
        indexes = build_indexes(finder)
        reports = []
        for data in datasets:
            job = CatalogImport(data, finder, indexes)
            job.plan()
            job.write_oem_table(apply)
            job.write_links(apply)
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
                         f"({c.get('specs_conflict', 0)} en conflicto)\n")
        return reports

    if apply:
        with matching_lock() as acquired:
            if not acquired:
                stdout.write('El análisis de coincidencias está en curso; vuelve a intentarlo cuando termine.\n')
                return None
            reports = execute()
    else:
        reports = execute()
    result = {'mode': 'apply' if apply else 'dry_run', 'seconds': round(time.monotonic() - started, 1), 'catalogs': reports}
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
    result = run(options.files, apply=options.apply, report_path=options.report, stdout=stdout)
    sys.exit(0 if result is not None else 1)


if __name__ == '__main__':
    main()
