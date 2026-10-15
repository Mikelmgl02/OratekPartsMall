"""Inventory refactor S10's archive and drop (plan §3 S10.1-3): the read-only preconditions, and the JSON export that comes before 0088
(the archive schema) and 0089 (the dropped columns), which refuse to run on a database whose rows the export does not hold.

    python -m mall.refactor.s10_archive --check         the preconditions, read only; exit 1 when one fails
    python -m mall.refactor.s10_archive --export [DIR]  every table 0088 archives and every value 0089 drops, as JSON lines (one row per
                                                        line, PostgreSQL's row_to_json), with DIR/export.json, the manifest the two
                                                        migrations check (rows and sha256 per file); and output/catalog_data copied to
                                                        docker_backups/catalog_data-<stamp>. Read only on the database, in one
                                                        REPEATABLE READ snapshot.

The migrations find the manifest at S10_EXPORT (a directory or its export.json), else the main checkout's output/refactor/s10/export
(mall.refactor.output.stage_dir: /app/output/refactor/s10/export in the containers, REFACTOR_OUTPUT in a rehearsal). Run both inside
the app container, from the checkout being deployed, before migrating to 0088:

    docker exec partsmall-app-1 python -m mall.refactor.s10_archive --check
    docker exec partsmall-app-1 python -m mall.refactor.s10_archive --export     (DIR defaults to that folder, or S10_EXPORT)

An archived table moves back with ALTER TABLE archive.<table> SET SCHEMA public (0088's reverse does it; 0087's reverse puts its
foreign keys back from archive.s10_constraints); a dropped column comes back only from the dump taken before the window, or from this
export."""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

# The tables 0088 moves to the archive schema (frozen since their stage; their models left the state with 0087).
ARCHIVED = ('mall_skuproposalitem', 'mall_skuproposal', 'mall_oemcrossreferenceretired', 'mall_oemcrossreference', 'mall_partoemlink',
            'mall_oemreferencesource', 'mall_oemreference', 'mall_suppliercodemapping', 'mall_alternate')
# The columns 0089 drops: (table, key column, columns). Only rows with a value are exported (a blank or NULL one says nothing).
DROPPED = (('mall_part', 'id', ('name', 'category', 'subcategory', 'legacy_brand')),
           ('mall_partcode', 'id', ('ref_type', 'reference_source', 'oem_reference_id')),
           ('mall_parttype', 'id', ('category',)),
           ('mall_skureviewverdict', 'id', ('proposal_id',)),
           ('mall_searchdocument', 'line_id', ('reach',)))
# ref_type is no value of its own: 0086's trigger writes it from tipo. Only a row whose ref_type disagrees would say something.
DERIVED = {('mall_partcode', 'ref_type'): "ref_type <> CASE tipo WHEN 'oem' THEN 'oem' WHEN 'aftermarket' THEN 'company' ELSE 'unknown' END"}
OUTCOMES = ('converted', 'case', 'archive')  # what S9's xref_outcomes.jsonl says of every OEMCrossReference row
LEGACY_KINDS = ('supplier', 'catalog', 'catalog_reference', 'seed')
LEGACY_STATUSES = ('unmatched', 'resolved')


def qn(name):
    return '"' + name.replace('"', '""') + '"'


def has_value(table, column):
    """The SQL condition of a column that holds something 0089 would lose."""
    if (table, column) in DERIVED:
        return DERIVED[table, column]
    if column.endswith('_id'):
        return f'{qn(column)} IS NOT NULL'
    return f"COALESCE({qn(column)}::text, '') <> ''"


def column_query(table, key, columns):
    cond = ' OR '.join(f'({has_value(table, column)})' for column in columns)
    picked = ', '.join(qn(name) for name in (key, *columns))
    return f'SELECT {picked} FROM {qn(table)} WHERE {cond}'


def table_exists(cursor, table, schema='public'):
    cursor.execute('SELECT to_regclass(%s) IS NOT NULL', [f'{schema}.{table}'])
    return cursor.fetchone()[0]


def column_exists(cursor, table, column):
    cursor.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
                   [table, column])
    return cursor.fetchone() is not None


def count(cursor, sql, params=None):
    cursor.execute(f'SELECT count(*) FROM ({sql}) t', params or [])
    return cursor.fetchone()[0]


# ---------------------------------------------------------------- preconditions
def outcomes_path():
    from .output import stage_dir
    return stage_dir('s9') / 'xref_outcomes.jsonl'


def xref_outcomes(path):
    """{OEMCrossReference id: its latest outcome} from S9's outcomes file (every run appends; the last line for a row wins)."""
    found = {}
    if not path.exists():
        return found
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                row = json.loads(line)
                if row.get('row') is not None:
                    found[int(row['row'])] = row.get('outcome')
    return found


def preconditions(cursor, outcomes=None):
    """[(label, count, ok, detail)]: each S10 precondition, read only. ok False stops the archive."""
    checks = []

    def check(label, value, ok, detail=''):
        checks.append((label, value, ok, detail))

    # 1. Every OEMCrossReference row is converted, in a case or listed as archive only (S9's outcomes file).
    if table_exists(cursor, 'mall_oemcrossreference'):
        cursor.execute('SELECT id FROM mall_oemcrossreference')
        ids = [row[0] for row in cursor.fetchall()]
        outcomes = xref_outcomes(outcomes_path()) if outcomes is None else outcomes
        missing = [pk for pk in ids if outcomes.get(pk) not in OUTCOMES]
        check('códigos de la biblioteca (OEMCrossReference) sin resultado de S9', len(missing), not missing,
              f'{len(ids)} filas; ejemplos: {missing[:5]}' if missing else f'{len(ids)} filas, todas con resultado')
    else:
        check('OEMCrossReference', 0, True, 'ya archivada')
    # 2. Values that leave with the columns and are no other column's: listed (the export keeps them), never blocking.
    if column_exists(cursor, 'mall_part', 'name'):
        check('líneas con un nombre distinto de su descripción y su SKU (no unificadas)',
              count(cursor, "SELECT 1 FROM mall_part WHERE name <> '' AND name <> description AND name <> sku AND estado <> 'unificada'"), True,
              'el nombre deja de existir: la descripción es el texto de la línea (la exportación los guarda)')
        check('líneas con legacy_brand', count(cursor, "SELECT 1 FROM mall_part WHERE legacy_brand <> ''"), True)
        check('líneas con categoría en texto y sin subgrupo',
              count(cursor, "SELECT 1 FROM mall_part WHERE part_type_id IS NULL AND (category <> '' OR subcategory <> '')"), True,
              'su texto se pierde: no nombra un subgrupo')
    # 3. The mirrors the generated columns replace, and ref_type, agree with what derives them.
    check('active distinto de estado = publicada', count(cursor, "SELECT 1 FROM mall_part WHERE active <> (estado = 'publicada')"), True)
    check('is_OEM distinto de tipo = OEM', count(cursor, "SELECT 1 FROM mall_part WHERE \"is_OEM\" <> (tipo = 'OEM')"), True)
    if column_exists(cursor, 'mall_partcode', 'ref_type'):
        check('ref_type distinto del de su tipo', count(cursor, f"SELECT 1 FROM mall_partcode WHERE {DERIVED['mall_partcode', 'ref_type']}"), True)
    if column_exists(cursor, 'mall_partcode', 'reference_source'):
        check('reference_source sin una fuente con esa cita', count(cursor, """
            SELECT 1 FROM mall_partcode r WHERE r.reference_source <> '' AND NOT EXISTS (
                SELECT 1 FROM mall_referencesource s WHERE s.reference_id = r.id AND (s.citation = r.reference_source
                    OR r.reference_source LIKE '%%' || s.citation || '%%' OR s.citation LIKE '%%' || r.reference_source || '%%'))"""), True,
              'la exportación guarda el texto')
    # 4. I12: every brand a Reference carries is a Brand's nombre (0089's foreign key).
    unknown = count(cursor, "SELECT 1 FROM mall_partcode r WHERE COALESCE(r.brand, '') <> '' AND NOT EXISTS "
                            "(SELECT 1 FROM mall_brand b WHERE b.nombre = r.brand)")
    check('marcas de referencias que no son una marca (I12)', unknown, unknown == 0, 'corrígelas en Marcas antes de 0089' if unknown else '')
    # 5. Revisión IA's verdicts: each one given on a proposal has its line (0089 drops proposal_id).
    if column_exists(cursor, 'mall_skureviewverdict', 'proposal_id'):
        orphans = count(cursor, 'SELECT 1 FROM mall_skureviewverdict WHERE proposal_id IS NOT NULL AND line_id IS NULL')
        check('veredictos de una propuesta sin línea', orphans, orphans == 0)
    # 6. MatchingCase: no legacy kind or status left (0086 converts them; 0089 adds the CHECKs).
    legacy = count(cursor, 'SELECT 1 FROM mall_matchingcase WHERE kind = ANY(%s) OR status = ANY(%s)', [list(LEGACY_KINDS), list(LEGACY_STATUSES)])
    check('casos de un tipo o estado anterior a S6', legacy, True, 'los convierte 0086 (y otra vez 0089)' if legacy else '')
    # 7. What 0088 touches.
    tables = [f'public.{table}' for table in present(cursor)]
    cursor.execute("""SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND (conrelid = ANY(%s::regclass[]) OR confrelid = ANY(%s::regclass[]))""",
                   [tables, tables])
    check('claves foráneas que tocan las tablas por archivar', cursor.fetchone()[0], True, '0088 las guarda en archive.s10_constraints y las quita')
    check('tablas por archivar que siguen en public', len(tables), True, ', '.join(table.split('.')[1] for table in tables))
    cursor.execute("SELECT count(*) FROM pg_namespace WHERE nspname = 'archive'")
    schema = cursor.fetchone()[0]
    check('esquema archive', schema, True, 'existe' if schema else 'lo crea 0088')
    return checks


def present(cursor):
    return [table for table in ARCHIVED if table_exists(cursor, table)]


# ---------------------------------------------------------------- export
def copy_out(cursor, sql, path):
    """COPY the rows of sql to path as JSON lines; returns (rows, sha256)."""
    digest, rows = hashlib.sha256(), 0
    with path.open('wb') as handle:
        with cursor.cursor.copy(f'COPY (SELECT row_to_json(t) FROM ({sql}) t) TO STDOUT') as copy:
            for data in copy:
                chunk = bytes(data)
                handle.write(chunk)
                digest.update(chunk)
                rows += chunk.count(b'\n')
    return rows, digest.hexdigest()


def export(directory, *, catalog_data=True, alias='default'):
    """The export (read only, one snapshot) and its manifest. Returns the manifest."""
    from django.conf import settings
    from django.db import connections, transaction
    from django.utils import timezone
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    connection = connections[alias]
    manifest = {'stage': 's10_archive', 'database': connection.settings_dict['NAME'], 'at': timezone.now().isoformat(),
                'tables': {}, 'columns': {}}
    outer = connection.in_atomic_block  # a caller's transaction (a test) has its own snapshot already
    with transaction.atomic(using=alias), connection.cursor() as cursor:
        if not outer:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        cursor.execute("SELECT name FROM django_migrations WHERE app = 'mall' ORDER BY id DESC LIMIT 1")
        manifest['leaf'] = (cursor.fetchone() or [''])[0]
        for table in present(cursor):
            rows, sha = copy_out(cursor, f'SELECT * FROM {qn(table)}', directory / f'{table}.jsonl')
            manifest['tables'][table] = {'file': f'{table}.jsonl', 'rows': rows, 'sha256': sha}
        for table, key, columns in DROPPED:
            kept = tuple(column for column in columns if column_exists(cursor, table, column))
            if not kept:
                continue
            rows, sha = copy_out(cursor, column_query(table, key, kept), directory / f'{table}.columns.jsonl')
            manifest['columns'][table] = {'file': f'{table}.columns.jsonl', 'key': key, 'columns': list(kept), 'rows': rows, 'sha256': sha}
    if catalog_data:
        from .output import main_checkout
        source = main_checkout() / 'output' / 'catalog_data'
        if source.exists():
            stamp = timezone.localtime().strftime('%Y%m%d-%H%M%S')
            target = main_checkout() / 'docker_backups' / f'catalog_data-{stamp}'
            shutil.copytree(source, target)
            manifest['catalog_data'] = str(target)
    (directory / 'export.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m mall.refactor.s10_archive', description=__doc__.split('\n')[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--check', action='store_true', help='Las precondiciones, solo lectura.')
    group.add_argument('--export', metavar='DIR', nargs='?', const='',
                       help='Exporta las tablas por archivar y las columnas por quitar a DIR (por omisión, donde las migraciones la buscan).')
    parser.add_argument('--no-catalog-data', action='store_true', help='No copia output/catalog_data a docker_backups.')
    options = parser.parse_args(argv)
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.db import connection, transaction
    if options.check:
        outer = connection.in_atomic_block
        with transaction.atomic(), connection.cursor() as cursor:
            if not outer:
                cursor.execute('SET TRANSACTION READ ONLY')
            checks = preconditions(cursor)
        failed = False
        for label, value, ok, detail in checks:
            failed |= not ok
            print(f"{'OK ' if ok else 'NO '} {label}: {value}{f' ({detail})' if detail else ''}")
        return 1 if failed else 0
    from .output import stage_dir
    folder = Path(options.export or os.environ.get('S10_EXPORT') or stage_dir('s10') / 'export')  # where 0088 and 0089 look by default
    if folder.suffix == '.json':
        folder = folder.parent
    manifest = export(folder, catalog_data=not options.no_catalog_data)
    for section in ('tables', 'columns'):
        for table, entry in manifest[section].items():
            print(f"{table}{' (columnas)' if section == 'columns' else ''}: {entry['rows']} filas -> {entry['file']}")
    if manifest.get('catalog_data'):
        print(f"output/catalog_data copiado a {manifest['catalog_data']}")
    print(f"Manifiesto: {folder / 'export.json'}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
