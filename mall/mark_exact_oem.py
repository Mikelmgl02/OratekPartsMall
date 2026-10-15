"""Retired with inventory refactor S4 (plan §3 S4): it marked OEM every SKU whose code was a declared or verified number of the OEM
list. From S4 the library's numbers are References and a line's tipo mirrors its main (S2b), so there is nothing to mark
(mall.catalog_identity.mark_exact_matches is retired too).

    python -m mall.mark_exact_oem --dry-run|--apply [--report PATH]

It still honours the catalog freeze on --apply, like every catalog CLI, writes an empty report when asked and says that it is retired.
S10 removes it.
"""
import argparse
import csv
import os
import sys


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Retirado con S4: el tipo de una línea sigue a su referencia principal.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='No hace nada (retirado).')
    mode.add_argument('--apply', action='store_true', help='No hace nada (retirado).')
    parser.add_argument('--report', metavar='PATH', help='Escribe un CSV vacío (solo la cabecera).')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .refactor.guard import require_freeze
    require_freeze(options.apply)
    stdout = stdout or sys.stdout
    if options.report:
        with open(options.report, 'w', newline='', encoding='utf-8') as handle:
            csv.writer(handle).writerow(['part_id', 'sku', 'oem_items'])
    stdout.write(f"{'Aplicado' if options.apply else 'Simulación'} · retirado con S4: el tipo de una línea sigue a su referencia principal; "
                 '0 SKU por marcar como OEM\n')


if __name__ == '__main__':
    main()
