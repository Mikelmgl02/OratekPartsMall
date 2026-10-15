# Written by hand as step 1 of inventory refactor stage S10's contract (plan §1.2.5, §3 S10): the columns that go are made harmless
# while the code that lands with this migration stops reading and writing them. Nothing is dropped here, so the code before S10 still
# runs on this schema (a code-only revert):
# - Part.name, category and subcategory, PartType.category and Reference.ref_type and reference_source get a database default and,
#   in the state only, the names legacy_* (their columns kept by db_column), which frees name, category, subcategory and ref_type for
#   the properties that derive them (the description, the subgrupo, the tipo). Part.legacy_brand and SearchDocument.reach get a default.
# - Part.active and is_OEM, and Reference.ref_type, are written by triggers from estado, tipo and tipo, whatever writes the row: the
#   CHECKs line_active_iff_publicada, line_oem_iff_tipo and reference_tipo_matches_ref_type hold with no writer naming the columns
#   (0089, step 3, turns active and is_OEM into generated columns and drops ref_type).
# - Part.category and subcategory, and PartType.category, are written by triggers from the subgrupo, whatever writes it (a line's
#   part_type, a subgrupo's name or grupo, a grupo's nombre): the code of this stage writes only part_type, and the code before S10 reads
#   the text back (its Part.save resolves the subgrupo from it: mall.technical_data.bind_part_types), so after a code-only revert a
#   line classified since this migration keeps its subgrupo. A line whose subgrupo is cleared loses the text, unless the writer gave
#   text of its own (the code before S10 keeps a partial grupo). 0089 drops them with the columns.
# - Reference.brand becomes nullable (still '' for any brand until 0089 turns '' into NULL under the foreign key to Brand(nombre)).
# - PartType's old UNIQUE (Lower(category), Lower(name)) goes: the subgrupo's grupo is its FK (subgrupo_unique_in_grupo, 0082), and
#   the text no longer follows it.
# - MatchingCase: the legacy kinds and statuses become the S6 ones (convert_cases), each case keeping what it was in evidence['s10']:
#   supplier -> line (dismissed stays dismissed; resolved -> applied; review or unmatched -> dismissed when the item already hangs
#   from a Reference, else review under the key line:<item> that mall.reference_matching gives an item's line case), catalog,
#   catalog_reference and seed -> merge with their lines; any other unmatched -> review, resolved -> applied. 0089 adds the CHECKs.
# Reversing it puts every case back from evidence['s10'], drops the triggers and restores the defaults, the old unique constraint
# (the triggers kept each subgrupo's text its grupo's nombre, so it holds) and brand NOT NULL (it fails once a NULL brand exists: none
# does until 0089).

from django.db import migrations, models

from mall.refactor.migration_ops import with_lock_timeout

TRIGGERS = [
    """CREATE OR REPLACE FUNCTION mall_part_s10_mirrors() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
           NEW.active := (NEW.estado = 'publicada');
           NEW."is_OEM" := (NEW.tipo = 'OEM');
           RETURN NEW;
       END $$""",
    'DROP TRIGGER IF EXISTS mall_part_s10_mirrors ON mall_part',
    'CREATE TRIGGER mall_part_s10_mirrors BEFORE INSERT OR UPDATE ON mall_part FOR EACH ROW EXECUTE FUNCTION mall_part_s10_mirrors()',
    """CREATE OR REPLACE FUNCTION mall_reference_s10_ref_type() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
           NEW.ref_type := CASE NEW.tipo WHEN 'oem' THEN 'oem' WHEN 'aftermarket' THEN 'company' ELSE 'unknown' END;
           RETURN NEW;
       END $$""",
    'DROP TRIGGER IF EXISTS mall_reference_s10_ref_type ON mall_partcode',
    'CREATE TRIGGER mall_reference_s10_ref_type BEFORE INSERT OR UPDATE ON mall_partcode FOR EACH ROW EXECUTE FUNCTION mall_reference_s10_ref_type()',
    # The subgrupo's text, for the code before S10 (above). A line: from its subgrupo when part_type changes.
    """CREATE OR REPLACE FUNCTION mall_part_s10_subgrupo_text() RETURNS trigger LANGUAGE plpgsql AS $$
       DECLARE grupo_text text; subgrupo_text text;
       BEGIN
           IF TG_OP = 'INSERT' OR NEW.part_type_id IS DISTINCT FROM OLD.part_type_id THEN
               IF NEW.part_type_id IS NOT NULL THEN
                   SELECT g.nombre, t.name INTO grupo_text, subgrupo_text
                     FROM mall_parttype t JOIN mall_grupo g ON g.id = t.grupo_id WHERE t.id = NEW.part_type_id;
                   IF FOUND THEN
                       NEW.category := grupo_text;
                       NEW.subcategory := subgrupo_text;
                   END IF;
               ELSIF TG_OP = 'UPDATE' AND NEW.category IS NOT DISTINCT FROM OLD.category
                     AND NEW.subcategory IS NOT DISTINCT FROM OLD.subcategory THEN
                   NEW.category := '';
                   NEW.subcategory := '';
               END IF;
           END IF;
           RETURN NEW;
       END $$""",
    'DROP TRIGGER IF EXISTS mall_part_s10_subgrupo_text ON mall_part',
    'CREATE TRIGGER mall_part_s10_subgrupo_text BEFORE INSERT OR UPDATE ON mall_part FOR EACH ROW EXECUTE FUNCTION mall_part_s10_subgrupo_text()',
    # A subgrupo: its grupo's nombre when it gets a grupo (a grupo renamed writes the new one, below).
    """CREATE OR REPLACE FUNCTION mall_parttype_s10_grupo_text() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
           IF TG_OP = 'INSERT' OR NEW.grupo_id IS DISTINCT FROM OLD.grupo_id THEN
               NEW.category := COALESCE((SELECT g.nombre FROM mall_grupo g WHERE g.id = NEW.grupo_id), NEW.category);
           END IF;
           RETURN NEW;
       END $$""",
    'DROP TRIGGER IF EXISTS mall_parttype_s10_grupo_text ON mall_parttype',
    'CREATE TRIGGER mall_parttype_s10_grupo_text BEFORE INSERT OR UPDATE ON mall_parttype FOR EACH ROW EXECUTE FUNCTION mall_parttype_s10_grupo_text()',
    # A subgrupo renamed or moved to another grupo (or its grupo renamed, below): its lines' text follows.
    """CREATE OR REPLACE FUNCTION mall_parttype_s10_lines_text() RETURNS trigger LANGUAGE plpgsql AS $$
       DECLARE grupo_text text;
       BEGIN
           SELECT g.nombre INTO grupo_text FROM mall_grupo g WHERE g.id = NEW.grupo_id;
           IF FOUND THEN
               UPDATE mall_part SET category = grupo_text, subcategory = NEW.name
                WHERE part_type_id = NEW.id AND (category IS DISTINCT FROM grupo_text OR subcategory IS DISTINCT FROM NEW.name);
           END IF;
           RETURN NULL;
       END $$""",
    'DROP TRIGGER IF EXISTS mall_parttype_s10_lines_text ON mall_parttype',
    'CREATE TRIGGER mall_parttype_s10_lines_text AFTER UPDATE ON mall_parttype FOR EACH ROW '
    'WHEN (OLD.name IS DISTINCT FROM NEW.name OR OLD.grupo_id IS DISTINCT FROM NEW.grupo_id OR OLD.category IS DISTINCT FROM NEW.category) '
    'EXECUTE FUNCTION mall_parttype_s10_lines_text()',
    # A grupo renamed: its subgrupos' text (and through the trigger above, their lines').
    """CREATE OR REPLACE FUNCTION mall_grupo_s10_text() RETURNS trigger LANGUAGE plpgsql AS $$
       BEGIN
           UPDATE mall_parttype SET category = NEW.nombre WHERE grupo_id = NEW.id AND category IS DISTINCT FROM NEW.nombre;
           RETURN NULL;
       END $$""",
    'DROP TRIGGER IF EXISTS mall_grupo_s10_text ON mall_grupo',
    'CREATE TRIGGER mall_grupo_s10_text AFTER UPDATE ON mall_grupo FOR EACH ROW WHEN (OLD.nombre IS DISTINCT FROM NEW.nombre) '
    'EXECUTE FUNCTION mall_grupo_s10_text()',
]

DROP_TRIGGERS = [
    'DROP TRIGGER IF EXISTS mall_grupo_s10_text ON mall_grupo',
    'DROP FUNCTION IF EXISTS mall_grupo_s10_text()',
    'DROP TRIGGER IF EXISTS mall_parttype_s10_lines_text ON mall_parttype',
    'DROP FUNCTION IF EXISTS mall_parttype_s10_lines_text()',
    'DROP TRIGGER IF EXISTS mall_parttype_s10_grupo_text ON mall_parttype',
    'DROP FUNCTION IF EXISTS mall_parttype_s10_grupo_text()',
    'DROP TRIGGER IF EXISTS mall_part_s10_subgrupo_text ON mall_part',
    'DROP FUNCTION IF EXISTS mall_part_s10_subgrupo_text()',
    'DROP TRIGGER IF EXISTS mall_reference_s10_ref_type ON mall_partcode',
    'DROP FUNCTION IF EXISTS mall_reference_s10_ref_type()',
    'DROP TRIGGER IF EXISTS mall_part_s10_mirrors ON mall_part',
    'DROP FUNCTION IF EXISTS mall_part_s10_mirrors()',
]

MERGE_KINDS = ('catalog', 'catalog_reference', 'seed')


def convert_cases(apps, schema_editor):
    """The legacy cases as S6 kinds and statuses (idempotent: a converted case is no legacy one any more). Returns nothing."""
    MatchingCase = apps.get_model('mall', 'MatchingCase')
    Part = apps.get_model('mall', 'Part')
    with schema_editor.connection.cursor() as cursor:
        # A supplier case: its item's line case. One whose item already hangs from a Reference waits for nothing.
        cursor.execute("""
            UPDATE mall_matchingcase c SET
                evidence = c.evidence || jsonb_build_object('s10', jsonb_build_object('kind', c.kind, 'status', c.status)),
                kind = 'line',
                status = CASE WHEN c.status = 'dismissed' THEN 'dismissed'
                              WHEN c.status IN ('resolved', 'applied') THEN 'applied'
                              WHEN c.item_id IS NULL THEN 'dismissed'
                              WHEN EXISTS (SELECT 1 FROM mall_supplieritem i WHERE i.id = c.item_id AND i.reference_id IS NOT NULL)
                                   THEN 'dismissed'
                              ELSE 'review' END
            WHERE c.kind = 'supplier'
        """)
        # A line case still waiting takes the key mall.reference_matching gives its item's line case, unless one has it already.
        cursor.execute("""
            UPDATE mall_matchingcase c SET
                evidence = jsonb_set(c.evidence, '{s10,key}', to_jsonb(c.key)),
                key = 'line:' || c.item_id::text
            WHERE c.kind = 'line' AND c.status = 'review' AND c.item_id IS NOT NULL AND c.evidence ? 's10'
              AND c.key <> 'line:' || c.item_id::text
              AND NOT EXISTS (SELECT 1 FROM mall_matchingcase o WHERE o.key = 'line:' || c.item_id::text)
        """)
        cursor.execute("""
            UPDATE mall_matchingcase c SET status = 'dismissed',
                evidence = jsonb_set(c.evidence, '{s10,reason}', to_jsonb(c.reason)),
                reason = left('Ya hay un caso de línea para este artículo. ' || c.reason, 2000)
            WHERE c.kind = 'line' AND c.status = 'review' AND c.item_id IS NOT NULL AND c.evidence ? 's10'
              AND c.key <> 'line:' || c.item_id::text
        """)
        # Any other legacy status.
        cursor.execute("""
            UPDATE mall_matchingcase c SET
                evidence = c.evidence || jsonb_build_object('s10', jsonb_build_object('kind', c.kind, 'status', c.status)),
                status = CASE c.status WHEN 'unmatched' THEN 'review' ELSE 'applied' END
            WHERE c.status IN ('unmatched', 'resolved') AND c.kind <> ALL(%s)
        """, [list(MERGE_KINDS)])
    # A catalog, catalog_reference or seed case: a merge case over the lines its rows name.
    for case in MatchingCase.objects.filter(kind__in=MERGE_KINDS).order_by('pk'):
        ids = [row.get('id') for row in [*(case.sources or []), *(case.candidates or [])] if isinstance(row, dict) and row.get('id')]
        lines = list(Part.objects.filter(pk__in=ids).values_list('pk', flat=True))
        before = set(case.lines.values_list('pk', flat=True))
        case.evidence = {**(case.evidence or {}), 's10': {'kind': case.kind, 'status': case.status,
                                                         'lines': sorted(str(pk) for pk in set(lines) - before)}}
        case.kind = 'merge'
        case.status = {'unmatched': 'review', 'resolved': 'applied'}.get(case.status, case.status)
        case.save(update_fields=['kind', 'status', 'evidence'])
        case.lines.add(*lines)


def restore_cases(apps, schema_editor):
    """convert_cases undone from evidence['s10']: kind, status, key and reason as they were, and the lines a merge case got taken off."""
    MatchingCase = apps.get_model('mall', 'MatchingCase')
    for case in MatchingCase.objects.filter(kind='merge', evidence__has_key='s10').order_by('pk'):
        added = (case.evidence['s10'] or {}).get('lines') or []
        if added:
            case.lines.remove(*added)
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            UPDATE mall_matchingcase SET kind = evidence->'s10'->>'kind', status = evidence->'s10'->>'status',
                key = COALESCE(evidence->'s10'->>'key', key), reason = COALESCE(evidence->'s10'->>'reason', reason),
                evidence = evidence - 's10'
            WHERE evidence ? 's10'
        """)


class Migration(migrations.Migration):

    dependencies = [
        ('mall', '0085_search_document'),
    ]

    operations = with_lock_timeout([
        # PartType's old unique text pair (first: it names category, which the state renames below).
        migrations.RemoveConstraint(model_name='parttype', name='unique_part_type_group_name'),
        # The doomed columns: a database default (the inserts of the code that no longer names them), and legacy_* names in the state.
        migrations.AlterField(
            model_name='part', name='name',
            field=models.CharField(blank=True, db_column='name', db_default='', default='', editable=False, max_length=200,
                                   verbose_name='nombre (anterior a S10)'),
        ),
        migrations.AlterField(
            model_name='part', name='category',
            field=models.CharField(blank=True, db_column='category', db_default='', default='', editable=False, max_length=120,
                                   verbose_name='categoría (anterior a S10)'),
        ),
        migrations.AlterField(
            model_name='part', name='subcategory',
            field=models.CharField(blank=True, db_column='subcategory', db_default='', default='', editable=False, max_length=120,
                                   verbose_name='subcategoría (anterior a S10)'),
        ),
        migrations.AlterField(
            model_name='part', name='legacy_brand',
            field=models.CharField(blank=True, db_default='', default='', editable=False, max_length=120, verbose_name='marca histórica'),
        ),
        migrations.AlterField(
            model_name='parttype', name='category',
            field=models.CharField(blank=True, db_column='category', db_default='', default='', editable=False, max_length=120,
                                   verbose_name='grupo (texto anterior a S10)'),
        ),
        migrations.AlterField(
            model_name='reference', name='ref_type',
            field=models.CharField(choices=[('unknown', 'SIN CLASIFICAR'), ('oem', 'OEM'), ('company', 'EMPRESA / FABRICANTE')],
                                   db_column='ref_type', db_default='unknown', db_index=True, default='unknown', editable=False, max_length=20),
        ),
        migrations.AlterField(
            model_name='reference', name='reference_source',
            field=models.CharField(blank=True, db_column='reference_source', db_default='', default='', editable=False, max_length=500,
                                   verbose_name='fuente de la equivalencia (anterior a S10)'),
        ),
        # The names (state only: each column stays, named by db_column). The CHECK on ref_type names the renamed field afterwards; its
        # SQL is the same, so the database keeps it as it is.
        migrations.SeparateDatabaseAndState(state_operations=[
            migrations.RemoveConstraint(model_name='reference', name='reference_tipo_matches_ref_type'),
            migrations.RenameField(model_name='part', old_name='name', new_name='legacy_name'),
            migrations.RenameField(model_name='part', old_name='category', new_name='legacy_category'),
            migrations.RenameField(model_name='part', old_name='subcategory', new_name='legacy_subcategory'),
            migrations.RenameField(model_name='parttype', old_name='category', new_name='legacy_category'),
            migrations.RenameField(model_name='reference', old_name='ref_type', new_name='legacy_ref_type'),
            migrations.RenameField(model_name='reference', old_name='reference_source', new_name='legacy_reference_source'),
            migrations.AddConstraint(
                model_name='reference',
                constraint=models.CheckConstraint(
                    condition=models.Q(models.Q(('legacy_ref_type', 'oem'), ('tipo', 'oem')),
                                       models.Q(('legacy_ref_type', 'company'), ('tipo', 'aftermarket')),
                                       models.Q(('legacy_ref_type', 'unknown'), ('tipo__in', ['interno', 'otro'])), _connector='OR'),
                    name='reference_tipo_matches_ref_type')),
        ]),
        migrations.AlterField(
            model_name='searchdocument', name='reach',
            field=models.TextField(blank=True, db_default='', default='',
                                   help_text='Hasta S9: los números OEM vinculados a la línea y los códigos posventa archivados bajo sus números, compactos.',
                                   verbose_name='números alcanzados'),
        ),
        # Reference.brand: nullable (0089 turns '' into NULL under the foreign key to Brand(nombre)).
        migrations.AlterField(
            model_name='reference', name='brand',
            field=models.CharField(blank=True, default='', max_length=120, null=True, verbose_name='marca opcional'),
        ),
        # PartType: the grupo FK is its only record of the grupo.
        migrations.AlterModelOptions(name='parttype', options={'ordering': ['grupo__nombre', 'name']}),
        migrations.AlterField(
            model_name='parttype', name='grupo',
            field=models.ForeignKey(on_delete=models.deletion.PROTECT, related_name='subgrupos', to='mall.grupo', verbose_name='grupo'),
        ),
        migrations.RunSQL(TRIGGERS, reverse_sql=DROP_TRIGGERS),
        migrations.RunPython(convert_cases, restore_cases),
    ])
