"""Mark OEM every SKU whose code is a declared or verified number of the OEM list (MR-453383 = MITSUBISHI MR453383), see
mall.catalog_identity.mark_exact_matches.

    python -m mall.mark_exact_oem --dry-run|--apply [--report PATH]

Every matching pass already does this (OEM_EXACT_MARK); run it to see or apply the marks at once, for instance after an import of
OEM numbers. --report writes one CSV row per SKU (sku, the list items with its code). It never renames a SKU or writes a PartCode;
each mark leaves a CatalogIdentityChange (reference.action = exact_match).
"""
import argparse
import csv
import os
import sys


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Marca como OEM los SKU cuyo código está en la lista OEM (declarado o verificado).')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Lista los SKU sin escribir nada.')
    mode.add_argument('--apply', action='store_true', help='Los marca como OEM.')
    parser.add_argument('--report', metavar='PATH', help='Escribe un CSV con cada SKU y los ítems de la lista con su código.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .catalog_identity import mark_exact_matches
    from .oem_apply import service_user
    stdout = stdout or sys.stdout
    rows = mark_exact_matches(service_user() if options.apply else None, apply=options.apply)
    if options.report:
        with open(options.report, 'w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['part_id', 'sku', 'oem_items'])
            for pk, sku, items in rows:
                writer.writerow([pk, sku, ' · '.join(f'{manufacturer} {number}' for manufacturer, number in items)])
    stdout.write(f"{'Aplicado' if options.apply else 'Simulación'} · {len(rows)} SKU con su código en la lista OEM "
                 f"{'marcados' if options.apply else 'por marcar'} como OEM\n")


if __name__ == '__main__':
    main()
