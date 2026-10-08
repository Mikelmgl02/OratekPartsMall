"""Alternos step 2c: remove the company alternos a supplier catalog copied onto a SKU once the SKU reaches the same code through its
OEM numbers (mall.oem_links). A SKU that carries any catalog alterno it cannot reach that way is left whole, for a person to review:
the copy may come from an item matched through a number the catalog prints for several parts. Hand-typed alternos, OEM alternos and
codes no imported catalog cites are never touched.

    python -m mall.prune_catalog_alternos --dry-run|--apply [--report PATH]

The report lists every alterno it removes or keeps for review (CSV). Apply runs take the matching worker's lock; back up the
database first. Re-running removes nothing more.
"""
import argparse
import csv
import os
import sys
from collections import defaultdict


def catalog_citations():
    """The bare citations of the catalogs mall.supplier_catalog_import loaded (their OEM table sources name the catalog key)."""
    from .oem_reference_models import OEMReferenceSource
    return sorted(set(OEMReferenceSource.objects.filter(kind='aftermarket_catalog', detail__has_key='catalog')
                      .values_list('citation', flat=True)) - {''})


def written_by_catalog(source, citations):
    return any(source == citation or source.startswith(f'{citation} · ') for citation in citations)


def plan():
    """{'remove': [PartCode], 'review': {sku: [PartCode]}}: per canonical SKU, its catalog-written company alternos, all removed when
    the SKU reaches every one of them through its OEM numbers, none otherwise."""
    from .models import PartCode
    from .oem_links import part_equivalents
    from .oem_reference_models import compact, manufacturer_name
    citations = catalog_citations()
    by_part = defaultdict(list)
    for code in (PartCode.objects.filter(ref_type='company', part__merged_into__isnull=True).select_related('part')
                 .order_by('part__sku', 'brand', 'code', 'pk')):
        if written_by_catalog(code.reference_source, citations):
            by_part[code.part_id].append(code)
    remove, review = [], {}
    for codes in by_part.values():
        part = codes[0].part
        reached = {(c['brand'], compact(c['code'])) for c in part_equivalents(part)['cross_references']}
        if all((manufacturer_name(c.brand), compact(c.code)) in reached for c in codes):
            remove += codes
        else:
            review[part.sku] = codes
    return {'remove': remove, 'review': review}


def write_report(path, result):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['accion', 'sku', 'marca', 'codigo', 'tipo', 'fuente', 'id_alterno', 'id_sku'])
        for code in result['remove']:
            writer.writerow(['retirado', code.part.sku, code.brand, code.code, code.ref_type, code.reference_source, code.pk, code.part_id])
        for codes in result['review'].values():
            for code in codes:
                writer.writerow(['revisar', code.part.sku, code.brand, code.code, code.ref_type, code.reference_source, code.pk, code.part_id])


def run(*, apply=False, report_path=None, stdout=None):
    from django.db import transaction
    from .matching_queue import matching_lock
    from .models import PartCode
    stdout = stdout or sys.stdout

    def execute():
        result = plan()
        if report_path:
            write_report(report_path, result)
        removed = 0
        if apply and result['remove']:
            with transaction.atomic():
                removed = PartCode.objects.filter(pk__in=[code.pk for code in result['remove']]).delete()[0]
        kept = sum(len(codes) for codes in result['review'].values())
        stdout.write(f"{'Aplicado' if apply else 'Simulación'} · {len(result['remove'])} alternos de catálogo ya llegan por número OEM "
                     f"en {len({code.part_id for code in result['remove']})} SKU ({removed} retirados) · {len(result['review'])} SKU con "
                     f"{kept} alternos quedan para revisión\n")
        return {'remove': len(result['remove']), 'removed': removed, 'review_skus': len(result['review']), 'review_alternos': kept}

    if not apply:
        return execute()
    with matching_lock() as acquired:
        if not acquired:
            stdout.write('El análisis de coincidencias está en curso; vuelve a intentarlo cuando termine.\n')
            return None
        return execute()


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Retira los alternos de empresa que un catálogo copió en un SKU cuando el SKU ya llega a '
                                                 'esos códigos por sus números OEM.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Cuenta y lista sin retirar nada.')
    mode.add_argument('--apply', action='store_true', help='Retira los alternos (respalda la base de datos antes).')
    parser.add_argument('--report', help='Escribe el detalle (CSV) en esta ruta.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    result = run(apply=options.apply, report_path=options.report, stdout=stdout)
    sys.exit(0 if result is not None else 1)


if __name__ == '__main__':
    main()
