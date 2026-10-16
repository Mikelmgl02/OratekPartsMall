# Written by hand as the second part of step 3 of inventory refactor stage S10's contract (plan §1.2.5, §3 S10.3-6): what 0086 made
# harmless and 0087 took out of the state leaves the database.
# - Columns dropped: Part.name, category, subcategory and legacy_brand; Reference.ref_type, reference_source and oem_reference_id;
#   PartType.category; SkuReviewVerdict.proposal_id (with its unique constraint and index); SearchDocument.reach (with its index).
#   Only on a database whose values the export of python -m mall.refactor.s10_archive --export holds (export.json's columns, row counts
#   equal; found as 0088 finds it), or that has none (a new or test database).
# - Part.active and is_OEM become generated columns of estado and tipo (0086's trigger and the CHECKs line_active_iff_publicada and
#   line_oem_iff_tipo go). PostgreSQL cannot turn a column into a generated one, so each is dropped and added again (a rewrite of
#   mall_part); is_OEM's index is made again under the name Django gave it.
# - Reference.brand: '' (any brand) becomes NULL, then CHECK (brand <> '') and an immediate foreign key to Brand(nombre) with ON UPDATE
#   CASCADE, so renaming a brand renames its codes and no code carries a brand that is not one (I12; refused with the count otherwise).
#   An index on brand serves the key's checks.
# - MatchingCase: any legacy kind or status left (written by the code before S10 after 0086) is converted by 0086's own convert_cases,
#   then CHECKs on the S6 kinds and statuses.
# The code of this step must land in the same window, under the freeze: the code of step 2 writes '' for a code with no brand, which
# the CHECK refuses.
# Reversing it adds the columns back empty (with their defaults; active, is_OEM and ref_type filled from estado and tipo, a line's
# category and subcategory and a subgrupo's category from the subgrupo) with the triggers and CHECKs of 0086, turns NULL brands back
# into '' and drops the key: the other values come back from the export (python -m mall.refactor.s10_archive --rollback) or the dump
# taken before the window.
import importlib
import json
import os
from pathlib import Path

from django.db import migrations, models

from mall.refactor.migration_ops import with_lock_timeout
from mall.refactor.output import stage_dir

KINDS = ('reference', 'code_change', 'variant', 'line', 'merge')
STATUSES = ('review', 'dismissed', 'applied')
DROPPED = (('mall_part', ('name', 'category', 'subcategory', 'legacy_brand')),
           ('mall_partcode', ('ref_type', 'reference_source', 'oem_reference_id')),
           ('mall_parttype', ('category',)),
           ('mall_skureviewverdict', ('proposal_id',)),
           ('mall_searchdocument', ('reach',)))
DERIVED = {('mall_partcode', 'ref_type'): "ref_type <> CASE tipo WHEN 'oem' THEN 'oem' WHEN 'aftermarket' THEN 'company' ELSE 'unknown' END"}
STEP1 = importlib.import_module('mall.migrations.0086_contract_step1')  # its triggers (for the reverse) and its case conversion


def manifest(section):
    """The export manifest's entries of a section (tables or columns), or None when there is no manifest."""
    location = Path(os.environ.get('S10_EXPORT') or stage_dir('s10') / 'export')
    path = location if location.suffix == '.json' else location / 'export.json'
    if not path.exists():
        return None
    return json.loads(path.read_text()).get(section) or {}


def has_value(table, column):
    """The SQL condition of a column that holds something this migration would lose (mall.refactor.s10_archive.has_value)."""
    if (table, column) in DERIVED:
        return DERIVED[table, column]
    if column.endswith('_id'):
        return f'"{column}" IS NOT NULL'
    return f"COALESCE(\"{column}\"::text, '') <> ''"


def present_columns(cursor, table, columns):
    cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s", [table])
    have = {row[0] for row in cursor.fetchall()}
    return tuple(column for column in columns if column in have)


def require_export(cursor):
    """Refuse (RuntimeError, nothing changed) when values would go that the export does not hold."""
    wanted = {}
    for table, columns in DROPPED:
        kept = present_columns(cursor, table, columns)
        if kept:
            cursor.execute(f'SELECT count(*) FROM "{table}" WHERE ' + ' OR '.join(f'({has_value(table, column)})' for column in kept))
            rows = cursor.fetchone()[0]
            if rows:
                wanted[table] = rows
    if not wanted:
        return
    exported = manifest('columns')
    if exported is None:
        raise RuntimeError('0089 quita columnas con valores y no encuentra la exportación (export.json). Ejecuta primero '
                           'python -m mall.refactor.s10_archive --export /app/output/refactor/s10/export (o apunta S10_EXPORT a ella).')
    wrong = {table: (rows, (exported.get(table) or {}).get('rows')) for table, rows in wanted.items()
             if (exported.get(table) or {}).get('rows') != rows}
    if wrong:
        raise RuntimeError('0089: la exportación no coincide con la base de datos (tabla: filas con valor ahora, exportadas): '
                           + '; '.join(f'{table}: {now}, {then}' for table, (now, then) in sorted(wrong.items()))
                           + '. Exporta otra vez y vuelve a migrar.')


def require_brands(cursor):
    """Refuse (RuntimeError, with the count) when a Reference carries a brand that is no Brand's nombre (I12): the key would fail."""
    cursor.execute("SELECT count(*), min(r.brand) FROM mall_partcode r WHERE COALESCE(r.brand, '') <> '' "
                   "AND NOT EXISTS (SELECT 1 FROM mall_brand b WHERE b.nombre = r.brand)")
    count, example = cursor.fetchone()
    if count:
        raise RuntimeError(f'0089 no puede exigir la marca: {count} referencias llevan una marca que no es una marca (por ejemplo {example}). '
                           'Agrégala en Marcas o corrige la referencia (python -m mall.refactor.s10_archive --check las cuenta).')


def forwards(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        require_export(cursor)
        require_brands(cursor)
    # Any legacy case the code before S10 wrote after 0086 (idempotent: a converted case is no legacy one any more).
    STEP1.convert_cases(apps, schema_editor)


DDL = [
    # The triggers and mirror CHECKs of 0086 (its subgrupo-text triggers write the columns that go).
    *STEP1.DROP_TRIGGERS,
    'ALTER TABLE mall_part DROP CONSTRAINT IF EXISTS line_active_iff_publicada',
    'ALTER TABLE mall_part DROP CONSTRAINT IF EXISTS line_oem_iff_tipo',
    'ALTER TABLE mall_partcode DROP CONSTRAINT IF EXISTS reference_tipo_matches_ref_type',
    # Part: the dropped columns, and active / is_OEM as generated columns (is_OEM's index under Django's name for it).
    'ALTER TABLE mall_part DROP COLUMN name, DROP COLUMN category, DROP COLUMN subcategory, DROP COLUMN legacy_brand, '
    'DROP COLUMN active, DROP COLUMN "is_OEM", '
    "ADD COLUMN active boolean GENERATED ALWAYS AS ((estado)::text = 'publicada') STORED, "
    "ADD COLUMN \"is_OEM\" boolean GENERATED ALWAYS AS ((tipo)::text = 'OEM') STORED",
    'CREATE INDEX "mall_part_is_OEM_027dd94b" ON mall_part ("is_OEM")',
    # Reference, PartType, the verdicts, the Search Document.
    'ALTER TABLE mall_partcode DROP COLUMN ref_type, DROP COLUMN reference_source, DROP COLUMN oem_reference_id',
    'ALTER TABLE mall_parttype DROP COLUMN category',
    'ALTER TABLE mall_skureviewverdict DROP COLUMN proposal_id',
    'ALTER TABLE mall_searchdocument DROP COLUMN reach',
    # Reference.brand: NULL for any brand, a Brand's nombre otherwise.
    "UPDATE mall_partcode SET brand = NULL WHERE brand = ''",
    "ALTER TABLE mall_partcode ADD CONSTRAINT reference_brand_set CHECK (brand <> '')",
    'CREATE INDEX reference_brand_idx ON mall_partcode (brand)',
    'ALTER TABLE mall_partcode ADD CONSTRAINT reference_brand_fk FOREIGN KEY (brand) REFERENCES mall_brand (nombre) ON UPDATE CASCADE',
    # MatchingCase: the S6 kinds and statuses only.
    "ALTER TABLE mall_matchingcase ADD CONSTRAINT matching_case_valid_kind CHECK (kind IN ('reference', 'code_change', 'variant', 'line', 'merge'))",
    "ALTER TABLE mall_matchingcase ADD CONSTRAINT matching_case_valid_status CHECK (status IN ('review', 'dismissed', 'applied'))",
]

REVERSE_DDL = [
    # The deferred key checks of the rows this rewrites run at once: an index cannot be built on a table with pending trigger events.
    'SET CONSTRAINTS ALL IMMEDIATE',
    'ALTER TABLE mall_matchingcase DROP CONSTRAINT IF EXISTS matching_case_valid_status',
    'ALTER TABLE mall_matchingcase DROP CONSTRAINT IF EXISTS matching_case_valid_kind',
    'ALTER TABLE mall_partcode DROP CONSTRAINT IF EXISTS reference_brand_fk',
    'DROP INDEX IF EXISTS reference_brand_idx',
    'ALTER TABLE mall_partcode DROP CONSTRAINT IF EXISTS reference_brand_set',
    "UPDATE mall_partcode SET brand = '' WHERE brand IS NULL",
    "ALTER TABLE mall_searchdocument ADD COLUMN reach text NOT NULL DEFAULT ''",
    'CREATE INDEX search_document_reach_trgm ON mall_searchdocument USING gin (reach gin_trgm_ops)',
    'ALTER TABLE mall_skureviewverdict ADD COLUMN proposal_id uuid NULL',
    'CREATE INDEX mall_skureviewverdict_proposal_id_4a5cd883 ON mall_skureviewverdict (proposal_id)',
    'CREATE INDEX sku_review_latest ON mall_skureviewverdict (proposal_id, created_at DESC)',
    'ALTER TABLE mall_skureviewverdict ADD CONSTRAINT sku_review_one_verdict_per_run UNIQUE (run_id, proposal_id)',
    "ALTER TABLE mall_parttype ADD COLUMN category varchar(120) NOT NULL DEFAULT ''",
    "ALTER TABLE mall_partcode ADD COLUMN ref_type varchar(20) NOT NULL DEFAULT 'unknown', "
    "ADD COLUMN reference_source varchar(500) NOT NULL DEFAULT '', ADD COLUMN oem_reference_id uuid NULL",
    'CREATE INDEX mall_partcode_ref_type_e8812049 ON mall_partcode (ref_type)',  # the names Django gave them
    'CREATE INDEX mall_partcode_ref_type_e8812049_like ON mall_partcode (ref_type varchar_pattern_ops)',
    'CREATE INDEX mall_partcode_oem_reference_id_b5d45754 ON mall_partcode (oem_reference_id)',
    "UPDATE mall_partcode SET ref_type = CASE tipo WHEN 'oem' THEN 'oem' WHEN 'aftermarket' THEN 'company' ELSE 'unknown' END",
    "ALTER TABLE mall_part DROP COLUMN active, DROP COLUMN \"is_OEM\", "
    "ADD COLUMN name varchar(200) NOT NULL DEFAULT '', ADD COLUMN category varchar(120) NOT NULL DEFAULT '', "
    "ADD COLUMN subcategory varchar(120) NOT NULL DEFAULT '', ADD COLUMN legacy_brand varchar(120) NOT NULL DEFAULT '', "
    "ADD COLUMN active boolean NOT NULL DEFAULT true, ADD COLUMN \"is_OEM\" boolean NOT NULL DEFAULT false",
    # The mirrors, and the subgrupo's text as 0086's triggers kept it (python -m mall.refactor.s10_archive --rollback then puts back what
    # the export holds), in one pass over mall_part.
    'UPDATE mall_parttype t SET category = g.nombre FROM mall_grupo g WHERE g.id = t.grupo_id',
    "UPDATE mall_part p SET active = (p.estado = 'publicada'), \"is_OEM\" = (p.tipo = 'OEM'), "
    "category = COALESCE((SELECT g.nombre FROM mall_parttype t JOIN mall_grupo g ON g.id = t.grupo_id WHERE t.id = p.part_type_id), ''), "
    "subcategory = COALESCE((SELECT t.name FROM mall_parttype t WHERE t.id = p.part_type_id), '')",
    'CREATE INDEX "mall_part_is_OEM_027dd94b" ON mall_part ("is_OEM")',
    "ALTER TABLE mall_part ADD CONSTRAINT line_active_iff_publicada CHECK ((active AND estado = 'publicada') OR (NOT active AND estado <> 'publicada'))",
    "ALTER TABLE mall_part ADD CONSTRAINT line_oem_iff_tipo CHECK ((\"is_OEM\" AND tipo = 'OEM') OR (NOT \"is_OEM\" AND tipo <> 'OEM'))",
    "ALTER TABLE mall_partcode ADD CONSTRAINT reference_tipo_matches_ref_type CHECK ((ref_type = 'oem' AND tipo = 'oem') OR "
    "(ref_type = 'company' AND tipo = 'aftermarket') OR (ref_type = 'unknown' AND tipo IN ('interno', 'otro')))",
    *STEP1.TRIGGERS,  # the mirrors the code of step 2 relies on (it writes neither active, is_OEM nor ref_type)
]


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0088_archive'),
    ]

    operations = with_lock_timeout([
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(DDL, reverse_sql=REVERSE_DDL)],
            state_operations=[
                migrations.AddIndex(model_name='reference', index=models.Index(fields=['brand'], name='reference_brand_idx')),
                migrations.AddConstraint(model_name='matchingcase', constraint=models.CheckConstraint(
                    condition=models.Q(kind__in=list(KINDS)), name='matching_case_valid_kind')),
                migrations.AddConstraint(model_name='matchingcase', constraint=models.CheckConstraint(
                    condition=models.Q(status__in=list(STATUSES)), name='matching_case_valid_status')),
            ]),
    ], timeout='10s')
