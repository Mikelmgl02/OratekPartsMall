"""S0.10, target data step 1: where each catalog item's own code stands under its OEM numbers. Read-only.

    python -m mall.refactor.s0_own_codes FILE [FILE ...] [--report PATH]

The merge pass joins the numbers a catalog prints under one of its own items (rule 3b), so it needs the item's own code filed under
them (OEMCrossReference). For every item of FILE that prints OEM numbers it says whether the code is filed today under one of them
that still takes it with the current rules (filed), whether mall.supplier_catalog_replace FILE (or mall.supplier_catalog_import)
would file it (refile), whether it is filed today only under numbers that no longer take it, so the replace takes it away (retire),
or why no number can take it: every number is printed for another part too (shared) or is a drive-shaft number listed for a CV
component (assembly), the code has no key (no_key), or a superuser retired it from every number (retired). Each sample lists up to
20 codes. The 2026-10-13 count was 1,143 unfiled own codes: 120 ASVA hub, 511 ASVA CV, 367 ATE and 145 GMB items.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

SAMPLE = 20
REASONS = ('filed', 'refile', 'retire', 'shared', 'assembly', 'no_key', 'retired')


def classify(job, filed, retired, library):
    """{brand_code: reason} for the items of one catalog that print OEM numbers. filed and retired: {(brand, compact code): {reference
    id}}; library: {(manufacturer, compact number): reference id}."""
    from ..oem_reference_models import manufacturer_name
    out = {}
    for item in job.items:
        code = (item.get('brand_code') or '').strip()
        if not code or not item.get('oem'):
            continue
        key = job.filed_key(job.brand, code)
        if not key:
            out[code] = 'no_key'
            continue
        takers, others, why = set(), set(), set()  # the library ids of the numbers that take the code, and of the others
        for o in item['oem']:
            number = job.number_key(o)[1]
            ref = library.get((manufacturer_name(o['manufacturer']), number))
            if job.shared(o):
                why.add('shared')
            elif job.listed_assembly(item, o):
                why.add('assembly')
            elif ref is not None and ref in retired.get((job.brand, key), set()):
                why.add('retired')
            else:
                takers.add(ref)
                continue
            others.add(ref)
        mine = filed.get((job.brand, key), set())
        if mine & (takers - {None}):
            out[code] = 'filed'
        elif takers:
            out[code] = 'refile'
        elif mine & (others - {None}):
            out[code] = 'retire'
        else:
            out[code] = 'assembly' if 'assembly' in why else 'shared' if 'shared' in why else 'retired'
    return out


def run(paths, *, report_path=None, stdout=None):
    from ..catalog_suffixes import suffix_table
    from ..oem_finder import OEMFinder, load_snapshot
    from ..oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, OEMReference
    from ..supplier_catalog_import import CatalogImport, build_indexes, catalog_citations
    stdout = stdout or sys.stdout
    datasets = [json.load(open(path, encoding='utf-8')) for path in paths]
    finder = OEMFinder(load_snapshot(), suffix_table())
    indexes = build_indexes(finder, catalog_citations(datasets))
    library = {(manufacturer, code): str(pk) for pk, manufacturer, code in OEMReference.objects.values_list('pk', 'manufacturer', 'code')}
    filed, retired = defaultdict(set), defaultdict(set)
    for table, out in ((OEMCrossReference, filed), (OEMCrossReferenceRetired, retired)):
        for ref, brand, number in table.objects.values_list('reference_id', 'brand', 'number'):
            out[(brand, number)].add(str(ref))
    result = {'catalogs': []}
    for data in datasets:
        job = CatalogImport(data, finder, indexes)
        reasons = classify(job, filed, retired, library)
        counts = Counter(reasons.values())
        samples = defaultdict(list)
        for code, reason in sorted(reasons.items()):
            if reason != 'filed' and len(samples[reason]) < SAMPLE:
                samples[reason].append(code)
        result['catalogs'].append({'catalog': data['catalog']['key'], 'title': data['catalog']['title'], 'items_with_numbers': len(reasons),
                                   'counts': {reason: counts.get(reason, 0) for reason in REASONS}, 'samples': dict(samples)})
        c = counts
        stdout.write(f"{data['catalog']['title']} · {len(reasons)} artículos con números OEM · código propio archivado {c.get('filed', 0)} · "
                     f"se archivará {c.get('refile', 0)} · se retirará {c.get('retire', 0)} · no se archiva: {c.get('shared', 0)} con números "
                     f"compartidos, {c.get('assembly', 0)} con números de eje, {c.get('no_key', 0)} sin clave, {c.get('retired', 0)} retirados\n")
    if report_path:
        with open(report_path, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=1)
    return result


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Dónde está el código propio de cada artículo de un catálogo bajo sus números OEM (solo lectura).')
    parser.add_argument('files', nargs='+', help='Extractos JSON normalizados.')
    parser.add_argument('--report', help='Escribe el informe JSON en esta ruta.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    run(options.files, report_path=options.report, stdout=stdout)


if __name__ == '__main__':
    main()
