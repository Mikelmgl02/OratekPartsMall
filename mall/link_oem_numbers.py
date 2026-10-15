"""Retired with inventory refactor S4 (plan §3 S4): it linked every catalog SKU to the OEM number its own code names (16100-79445-NPW
-> TOYOTA 1610079445). From S4 a library number a line carries is one of that line's References (mall.refactor.s4_library moved the
numbers the name links tied), and rule 3 (S6) proposes the rest for review, so it writes nothing any more.

    python -m mall.link_oem_numbers --dry-run|--apply

It still honours the catalog freeze on --apply, like every catalog CLI, and says that it is retired. S10 removes it.
"""
import argparse
import os
import sys


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Retirado con S4: los números OEM que nombra el código de una línea son referencias de esa línea.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='No hace nada (retirado).')
    mode.add_argument('--apply', action='store_true', help='No hace nada (retirado).')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .refactor.guard import require_freeze
    require_freeze(options.apply)
    stdout = stdout or sys.stdout
    stdout.write(f"{'Aplicado' if options.apply else 'Simulación'} · retirado con S4: los números OEM que nombra el código de una línea son "
                 'referencias de esa línea (la regla 3 propone el resto en S6); no se escribe nada.\n')


if __name__ == '__main__':
    main()
