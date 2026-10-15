"""Stage S7 of the inventory refactor: the storefront's Search Document (mall.search_document).

    python -m mall.refactor.s7_search --dry-run|--apply|--rollback|--check|--compare [--snapshot PATH | --supplier-codes N] [--json]

--dry-run computes every document (one per line on sale, I8) without writing: how many lines, how big the documents are, how long it
takes, and, once 0085 is applied, how many a rebuild would write, leave alone and delete. It only reads, so it runs on the live database
before 0085 too (the tables it reads are S5's).
--apply rebuilds every document (mall.search_document.rebuild: only what changed is written, in batches; the documents of lines not on
sale go) and checks I8 after. The documents are derived data that the code before S7 never reads, so it runs before landing from the
worktree, and again after landing as the catch-up (plan §3 S7: D4, the pre-land build, D5, rebuild). The matching worker rebuilds every
six hours from then on, and every writer refreshes its lines' documents.
--rollback deletes every document (refused while CATALOG_SEARCH_SOURCE=document: flip the setting back first, which is S7's rollback;
the documents are rebuilt by --apply or the worker).
--check runs I8 both ways (Appendix C, C7); --compare runs the snapshot's searches (the newest snapshot's sample, or --snapshot) on both
backends (mall.catalog_search.backend) and lists, per search, what the document finds that the legacy search does not and the reverse:
the expected result is the same lines or more. --compare --supplier-codes N runs instead the codes of the N most recently updated matched
supplier items (lines on sale, live suppliers), as everyone searches them: after S6 a supplier's new codes are interno References, which
the document finds only in that supplier's own catalog, so each one the document misses is a code only its own supplier still finds
(tell the owner before flipping CATALOG_SEARCH_SOURCE).

On the live database --apply and --rollback need the S0.11 guard (mall.refactor.guard: the catalog freeze is on and no writer is still
busy); each run appends a line (the database, when, the user, the summary) to mall.refactor.output.manifest_path('s7')."""
import argparse
import gzip
import json
import os
import sys
import time
from pathlib import Path

STAGE = 's7'


def current_database():
    from django.db import connection
    return str(connection.settings_dict['NAME'])


def table_ready():
    from django.db import connection
    return 'mall_searchdocument' in connection.introspection.table_names()


def record(entry):
    from .output import manifest_path
    path = manifest_path(STAGE)
    if not path.parent.parent.exists():
        raise SystemExit(f'No existe {path.parent.parent}: la carpeta output/refactor del checkout principal.')
    path.parent.mkdir(exist_ok=True)
    with path.open('a', encoding='utf-8') as out:
        out.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')


def dry_run():
    from .. import search_document
    started = time.monotonic()
    docs = search_document.documents()
    seconds = round(time.monotonic() - started, 1)
    sizes = {field: sum(len(doc[field]) for doc in docs.values()) for field in ('codes', 'compact', 'reach', 'text')}
    result = {'database': current_database(), 'lines': len(docs), 'seconds_computing': seconds,
              'with_reach': sum(1 for doc in docs.values() if doc['reach']), 'with_internal': sum(1 for doc in docs.values() if doc['internal']),
              'with_brand': sum(1 for doc in docs.values() if doc['brand']),
              'characters': sizes, 'table': table_ready()}
    if result['table']:
        upsert, stale, same = search_document.plan(docs)
        result.update({'would_write': len(upsert), 'unchanged': same, 'would_delete': len(stale), 'check': search_document.check()})
    return result


def apply(username=None):
    from .. import search_document
    if not table_ready():
        raise SystemExit('Falta 0085_search_document: aplícala primero (python manage.py migrate mall 0085_search_document).')
    summary = search_document.rebuild(origin='cli')
    record({'action': 'apply', 'database': current_database(), 'user': username or '', 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'summary': summary})
    return summary


def rollback(username=None):
    from django.conf import settings
    from ..catalog_search import search_source
    from ..search_document_models import SearchDocument
    if search_source() == 'document':
        raise SystemExit(f'CATALOG_SEARCH_SOURCE={settings.CATALOG_SEARCH_SOURCE}: la tienda lee los documentos. Vuelve a legacy primero.')
    if not table_ready():
        return {'deleted': 0}
    deleted = SearchDocument.objects.all().delete()[0]
    record({'action': 'rollback', 'database': current_database(), 'user': username or '', 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'deleted': deleted})
    return {'deleted': deleted}


def snapshot_searches(path=None):
    from .snapshot import DEFAULT_OUT
    if path is None:
        found = sorted(Path(DEFAULT_OUT).glob('snapshot-*.json.gz'), key=lambda file: file.stat().st_mtime)
        if not found:
            raise SystemExit(f'No hay instantáneas en {DEFAULT_OUT}: indica --snapshot.')
        path = found[-1]
    with gzip.open(path, 'rt', encoding='utf-8') as source:
        return str(path), json.load(source)['sample']['searches']


def recent_supplier_codes(limit):
    """The codigos of the most recently updated matched items of live suppliers on lines on sale, each once, newest first."""
    from ..lines import storefront_lines
    from ..models import SupplierItem
    codes = []
    rows = (SupplierItem.objects.filter(matching_status='matched', supplier__active=True, part__in=storefront_lines().values('pk'))
            .exclude(codigo='').order_by('-updated_at', 'pk').values_list('codigo', flat=True))
    for code in rows.iterator(chunk_size=2000):
        if code not in codes:
            codes.append(code)
            if len(codes) >= limit:
                break
    return codes


def compare(searches, page=50):
    """Per search: the lines each backend finds (whole sets) and the first page in rank order. superset: the document finds every line the
    legacy search finds."""
    from ..catalog_scope import FULL
    from ..catalog_search import document_search_parts, document_search_rank, legacy_search_parts, legacy_search_rank
    from ..lines import storefront_lines

    def run(parts, rank, search):
        started = time.monotonic()
        query = parts(FULL.parts(storefront_lines()), search, None)
        ids = {str(pk) for pk in query.values_list('pk', flat=True)}
        ordering = rank(search, None)
        ordered = query.annotate(search_rank=ordering).order_by('search_rank', 'sku', 'id') if ordering is not None else query.order_by('sku', 'id')
        first = [str(pk) for pk in ordered.values_list('pk', flat=True)[:page]]
        return ids, first, round(time.monotonic() - started, 3)

    rows = []
    for search in searches:
        legacy, legacy_page, legacy_seconds = run(legacy_search_parts, legacy_search_rank, search)
        document, document_page, document_seconds = run(document_search_parts, document_search_rank, search)
        missing, extra = sorted(legacy - document), sorted(document - legacy)
        rows.append({'search': search, 'legacy': len(legacy), 'document': len(document), 'missing': missing[:20], 'missing_count': len(missing),
                     'extra_count': len(extra), 'extra': extra[:20], 'superset': not missing, 'same_first_page': legacy_page == document_page,
                     'seconds': {'legacy': legacy_seconds, 'document': document_seconds}})
    return rows


def explain(ids):
    """sku and description of lines, for the report of a difference."""
    from ..models import Part
    return [f'{sku} · {description[:60]}' for sku, description in Part.objects.filter(pk__in=ids).values_list('sku', 'description').order_by('sku')]


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description='S7: el documento de búsqueda de la tienda.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true', help='Calcula los documentos sin escribir nada (también antes de 0085).')
    mode.add_argument('--apply', action='store_true', help='Reconstruye todos los documentos (en vivo requiere el catálogo congelado).')
    mode.add_argument('--rollback', action='store_true', help='Borra los documentos (solo con CATALOG_SEARCH_SOURCE=legacy).')
    mode.add_argument('--check', action='store_true', help='I8 en ambos sentidos.')
    mode.add_argument('--compare', action='store_true', help='Las búsquedas de la instantánea en ambas búsquedas.')
    parser.add_argument('--snapshot', help='La instantánea cuya muestra de búsquedas usa --compare (la más reciente si se omite).')
    parser.add_argument('--supplier-codes', type=int, metavar='N',
                        help='--compare con los códigos de los N artículos de proveedor vinculados más recientes en vez de la instantánea.')
    parser.add_argument('--user', help='Usuario que figura en el manifiesto.')
    parser.add_argument('--json', action='store_true', help='Imprime el resultado en JSON.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .guard import require_freeze
    require_freeze(options.apply or options.rollback)
    stdout = stdout or sys.stdout
    if options.dry_run:
        result = dry_run()
    elif options.apply:
        result = apply(options.user)
    elif options.rollback:
        result = rollback(options.user)
    elif options.check:
        from .. import search_document
        result = search_document.check()
    else:
        if not table_ready():
            raise SystemExit('Falta 0085_search_document: --compare lee los documentos.')
        if options.supplier_codes:
            path, searches = f'supplier-codes:{options.supplier_codes}', recent_supplier_codes(options.supplier_codes)
        else:
            path, searches = snapshot_searches(options.snapshot)
        rows = compare(searches)
        result = {'snapshot': path, 'searches': len(rows), 'superset': sum(row['superset'] for row in rows),
                  'same_first_page': sum(row['same_first_page'] for row in rows), 'rows': rows}
        for row in rows:
            row['missing_lines'] = explain(row['missing'])
            row['extra_lines'] = explain(row['extra'][:10])
    if options.json:
        stdout.write(json.dumps(result, ensure_ascii=False, default=str, indent=1) + '\n')
    elif options.compare:
        for row in result['rows']:
            mark = 'OK ' if row['superset'] else 'MISSING'
            stdout.write(f"{mark} {row['search']!r}: legacy {row['legacy']}, document {row['document']} (+{row['extra_count']}, "
                         f"-{row['missing_count']}), first page {'same' if row['same_first_page'] else 'differs'}, "
                         f"{row['seconds']['legacy']}s / {row['seconds']['document']}s\n")
            for line in row['missing_lines']:
                stdout.write(f'    - {line}\n')
            for line in row['extra_lines']:
                stdout.write(f'    + {line}\n')
        stdout.write(f"{result['superset']}/{result['searches']} searches find every legacy line; {result['same_first_page']} keep their first page.\n")
    else:
        stdout.write(json.dumps(result, ensure_ascii=False, default=str) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
