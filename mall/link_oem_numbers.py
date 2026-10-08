"""Link every catalog SKU to the OEM number its own code names (16100-79445-NPW -> TOYOTA 1610079445), see mall.oem_links.

    python -m mall.link_oem_numbers --dry-run|--apply

The catalog import already does this after an apply and saving a SKU refreshes its own links; run it after a bulk change no save
signal sees (a suffix-table edit, a queryset rename). It never writes a PartCode, renames a SKU or changes a MAIN.
"""
import argparse
import os
import sys


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Vincula cada SKU con el número OEM que nombra su propio código.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Cuenta los vínculos sin escribir nada.')
    mode.add_argument('--apply', action='store_true', help='Agrega los vínculos que faltan y quita los que ya no corresponden.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .oem_links import refresh_name_links, tables_ready
    stdout = stdout or sys.stdout
    if not tables_ready():
        stdout.write('Aplica la migración 0041 antes de vincular los SKU con sus números OEM.\n')
        sys.exit(1)
    counts = refresh_name_links(apply=options.apply)
    stdout.write(f"{'Aplicado' if options.apply else 'Simulación'} · {counts['skus']} SKU nombran un número de la biblioteca · "
                 f"{counts['links']} vínculos ({counts['new']} nuevos, {counts['removed']} retirados)\n")


if __name__ == '__main__':
    main()
