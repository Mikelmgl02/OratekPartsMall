"""Undo an OEM auto-apply run: python -m mall.oem_finder_revert --run ID [--part UUID]

Each applied row gets its old SKU back (when no other SKU holds it), loses the PartCodes the run created (the algo OEM reference and
the alternos the rename kept) and its OEM flag, and gains a reverse CatalogIdentityChange; OEMFinderChange.reverted_at marks it.
A row whose SKU, grouping or run-written PartCodes changed since is reported and left untouched (all or nothing). Repeating the
command changes nothing. Takes the matching advisory lock (exit 2 while a matching pass or finder run is busy).
"""
import argparse
import os
import sys
import uuid


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='Deshace una ejecución de la aplicación OEM automática (o un SKU de ella).')
    parser.add_argument('--run', type=int, required=True, help='ID de la ejecución apply_auto.')
    parser.add_argument('--part', type=uuid.UUID, help='Solo este SKU (UUID del repuesto).')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .oem_apply import SKIP_LABELS, apply_tables_ready, revert_run, service_user
    from .oem_finder import matching_lock
    from .oem_finder_models import OEMFinderRun
    out = stdout or sys.stdout
    if not apply_tables_ready():
        print('Las tablas de la aplicación OEM aún no están migradas.', file=out)
        return 1
    if not OEMFinderRun.objects.filter(pk=options.run, mode='apply_auto').exists():
        print(f'No existe la ejecución apply_auto {options.run}.', file=out)
        return 1
    with matching_lock() as acquired:
        if not acquired:
            print('Hay un análisis de coincidencias o una búsqueda OEM en curso; vuelve a intentarlo cuando termine.', file=out)
            return 2
        result = revert_run(options.run, actor=service_user(), part=options.part)
    if result['reverted']:
        from .matching_queue import enqueue_matching
        enqueue_matching()
    print('Ejecución %d: %d revertidos, %d ya estaban revertidos, %d sin revertir.'
          % (options.run, result['reverted'], result['already_reverted'], len(result['skipped'])), file=out)
    for row in result['skipped']:
        print('  %s: %s%s' % (row['sku'], SKIP_LABELS.get(row['reason'], row['reason']), f" ({row['detail']})" if row['detail'] else ''), file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
