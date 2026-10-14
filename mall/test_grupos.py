"""Grupo / Subgrupo (inventory refactor S5): the Grupo table, PartType.grupo kept in step with its text, the writers that resolve or
create a grupo (mall.technical_data, mall.lines.set_subgrupo), and the backfill CLI (python -m mall.refactor.s5_grupos)."""
import io
import json
import os
import tempfile
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.test import APITestCase

from . import lines
from .models import Grupo, Part, PartType, TechnicalTemplate
from .refactor import s5_grupos
from .technical_data import bind_part_types, classification, grupo_for, part_type_for
from .test_factories import make_line, make_user


def allow_no_grupo():
    """Inside a test's transaction, so it comes back when the test rolls back: lets a subgrupo have no grupo, as 0081 leaves every
    subgrupo before the backfill (0082 makes the grupo required)."""
    with connection.cursor() as cursor:
        nullable = {column.name: column.null_ok for column in connection.introspection.get_table_description(cursor, PartType._meta.db_table)}
        if nullable.get('grupo_id') is False:
            cursor.execute(f'ALTER TABLE {PartType._meta.db_table} ALTER COLUMN grupo_id DROP NOT NULL')


def as_0081_leaves_it():
    """Every subgrupo without a grupo and no grupo at all, the text kept: the rows the backfill starts from."""
    allow_no_grupo()
    PartType.objects.update(grupo=None)
    Grupo.objects.all().delete()


class GrupoTests(TestCase):
    def test_a_grupo_is_one_per_name_ignoring_case(self):
        frenos = grupo_for(' frenos ')
        self.assertEqual(frenos.nombre, 'FRENOS')
        self.assertEqual(grupo_for('Frenos'), frenos)
        self.assertEqual(Grupo.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Grupo.objects.create(nombre='frenos')

    def test_a_subgrupo_saved_with_its_text_finds_or_creates_its_grupo(self):
        pads = PartType.objects.create(category='frenos', name='PASTILLAS')
        self.assertEqual((pads.grupo.nombre, pads.category), ('FRENOS', 'FRENOS'))
        discs = PartType.objects.create(category='FRENOS', name='DISCOS')
        self.assertEqual(discs.grupo_id, pads.grupo_id)
        filtros = grupo_for('FILTROS')
        oil = PartType.objects.create(grupo=filtros, category='OTRO TEXTO', name='ACEITE')  # the FK wins
        self.assertEqual(oil.category, 'FILTROS')

    def test_a_loaded_subgrupo_moves_by_its_text_or_by_its_grupo(self):
        pads = PartType.objects.create(category='FRENOS', name='PASTILLAS')
        loaded = PartType.objects.get(pk=pads.pk)
        loaded.category = 'FRENADO'  # the code before S5 moves a subgrupo by its text
        loaded.save()
        self.assertEqual(PartType.objects.get(pk=pads.pk).grupo.nombre, 'FRENADO')
        loaded = PartType.objects.get(pk=pads.pk)
        loaded.grupo = grupo_for('FRENOS')
        loaded.save(update_fields=['grupo'])  # the text follows even on a partial save
        self.assertEqual(PartType.objects.filter(pk=pads.pk).values_list('category', flat=True).get(), 'FRENOS')


class SubgrupoWriterTests(TestCase):
    def test_a_line_names_its_subgrupo_by_text_and_takes_its_spelling(self):
        line = make_line('S5-A', category=' frenos ', subcategory='discos')
        self.assertEqual((line.part_type.grupo.nombre, line.part_type.name), ('FRENOS', 'DISCOS'))
        self.assertEqual((line.category, line.subcategory), ('FRENOS', 'DISCOS'))
        self.assertTrue(TechnicalTemplate.objects.filter(part_type=line.part_type).exists())
        other = make_line('S5-B', category='FRENOS', subcategory='DISCOS')
        self.assertEqual(other.part_type_id, line.part_type_id)

    def test_a_new_line_given_only_its_subgrupo_takes_its_text_and_half_a_pair_stays_text(self):
        part_type = part_type_for('suspension', 'muelles')
        line = make_line('S5-TYPED', part_type=part_type)
        self.assertEqual((line.category, line.subcategory, line.part_type_id), ('SUSPENSION', 'MUELLES', part_type.pk))
        partial = make_line('S5-PARTIAL', category='frenos')
        self.assertEqual((partial.part_type_id, partial.category, partial.subcategory), (None, 'FRENOS', ''))
        self.assertEqual(classification(partial), ('FRENOS', ''))
        self.assertEqual(part_type_for('SUSPENSION', 'MUELLES'), part_type)
        with self.assertRaises(ValueError):
            part_type_for('', 'MUELLES')

    def test_a_subgrupo_with_no_grupo_yet_takes_the_grupo_its_text_names(self):
        allow_no_grupo()
        orphan = PartType.objects.create(category='FRENOS', name='DISCOS')
        PartType.objects.filter(pk=orphan.pk).update(grupo=None)
        line = make_line('S5-ORPHAN', category='FRENOS', subcategory='DISCOS')
        self.assertEqual(line.part_type_id, orphan.pk)
        orphan.refresh_from_db()
        self.assertEqual(orphan.grupo.nombre, 'FRENOS')

    def test_set_subgrupo_and_the_projection_follow_the_fk(self):
        line = make_line('S5-SET')
        discs = part_type_for('FRENOS', 'DISCOS')
        fields = lines.set_subgrupo(line, discs)
        line.save(update_fields=fields)
        line.refresh_from_db()
        self.assertEqual((line.part_type_id, line.category, line.subcategory), (discs.pk, 'FRENOS', 'DISCOS'))
        lines.set_subgrupo(line, None, grupo='frenos')
        self.assertEqual((line.part_type, line.category, line.subcategory), (None, 'FRENOS', ''))
        line = Part.objects.get(pk=line.pk)
        discs.grupo = grupo_for('FRENADO')
        discs.save()
        self.assertEqual(lines.project_subgrupos([discs]), 1)
        self.assertEqual(Part.objects.filter(pk=line.pk).values_list('category', 'subcategory').get(), ('FRENADO', 'DISCOS'))
        self.assertEqual(lines.project_subgrupos([discs]), 0)

    def test_bind_part_types_resolves_a_batch_in_a_few_queries(self):
        batch = [Part(sku=f'S5-BATCH-{index}', category='FRENOS', subcategory=f'SUB {index % 3}') for index in range(30)]
        with self.assertNumQueries(7):  # grupos read, created, read; subgrupos read, created, read; templates
            bind_part_types(batch)
        self.assertEqual(len({line.part_type_id for line in batch}), 3)
        self.assertEqual(Grupo.objects.count(), 1)


class PartTypeApiTests(APITestCase):
    url = '/api/v1/management/part-types/'

    def setUp(self):
        self.client.force_authenticate(make_user('s5-root', superuser=True))

    def test_category_is_the_grupo_and_a_new_one_creates_it(self):
        response = self.client.post(self.url, {'category': 'carroceria', 'name': 'faros'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        part_type = PartType.objects.get(pk=response.data['id'])
        self.assertEqual((response.data['category'], part_type.grupo.nombre), ('CARROCERIA', 'CARROCERIA'))
        PartType.objects.filter(pk=part_type.pk).update(category='TEXTO VIEJO')  # the FK is what the API shows
        self.assertEqual(self.client.get(f'{self.url}{part_type.pk}/').data['category'], 'CARROCERIA')
        duplicate = self.client.post(self.url, {'category': 'Carroceria', 'name': 'Faros'}, format='json')
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual([row['id'] for row in self.client.get(self.url, {'search': 'carroceria'}).data['results']], [str(part_type.pk)])

    def test_another_category_moves_the_subgrupo_and_its_lines_follow(self):
        line = make_line('S5-MOVE', category='FRENOS', subcategory='DISCOS')
        response = self.client.patch(f'{self.url}{line.part_type_id}/', {'category': 'frenado'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(PartType.objects.get(pk=line.part_type_id).grupo.nombre, 'FRENADO')
        self.assertEqual(Part.objects.filter(pk=line.pk).values_list('category', 'subcategory').get(), ('FRENADO', 'DISCOS'))
        self.assertEqual(set(Grupo.objects.values_list('nombre', flat=True)), {'FRENOS', 'FRENADO'})


class GrupoBackfillTests(TestCase):
    """python -m mall.refactor.s5_grupos on rows as 0081 leaves them: subgrupos with their text and no grupo."""

    def setUp(self):
        self.root = make_user('s5-backfill-root', superuser=True)
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        patcher = patch.dict(os.environ, {'REFACTOR_OUTPUT': self.output.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.discs = make_line('S5-DISC', category='FRENOS', subcategory='DISCOS').part_type
        self.pads = PartType.objects.create(category='FRENOS', name='PASTILLAS')
        self.oil = PartType.objects.create(category='FILTROS', name='ACEITE')
        as_0081_leaves_it()
        PartType.objects.filter(pk=self.pads.pk).update(category='Frenos')  # one grupo, spelled two ways

    def run_cli(self, *args):
        out = io.StringIO()
        code = s5_grupos.main([*args], stdout=out)
        return code, out.getvalue()

    def manifest(self):
        with open(os.path.join(self.output.name, 'manifest.jsonl'), encoding='utf-8') as handle:
            return [json.loads(line) for line in handle]

    def stored(self):
        return {name: (grupo, category) for name, grupo, category in PartType.objects.values_list('name', 'grupo__nombre', 'category')}

    def test_a_dry_run_counts_and_writes_nothing(self):
        code, out = self.run_cli('--dry-run')
        self.assertEqual(code, 0, out)
        self.assertIn('Simulación · S5', out)
        self.assertIn('FILTROS 1 (nuevo) · FRENOS 2 (nuevo)', out)
        self.assertIn('Líneas cuyo texto difiere de su subgrupo: 0 (las facetas no cambian)', out)
        self.assertFalse(Grupo.objects.exists())
        self.assertFalse(os.path.exists(os.path.join(self.output.name, 'manifest.jsonl')))

    def test_apply_gives_every_subgrupo_its_grupo_once_and_logs_every_row(self):
        self.assertEqual(self.run_cli('--apply')[0], 1)  # --user is required
        filtros = Grupo.objects.create(nombre='FILTROS')  # a grupo the code already created is reused
        code, out = self.run_cli('--apply', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.stored(), {'DISCOS': ('FRENOS', 'FRENOS'), 'PASTILLAS': ('FRENOS', 'FRENOS'), 'ACEITE': ('FILTROS', 'FILTROS')})
        self.assertEqual(PartType.objects.get(name='ACEITE').grupo_id, filtros.pk)
        rows = self.manifest()
        self.assertEqual(rows[0]['action'], 'run')
        self.assertEqual([row['after']['nombre'] for row in rows if row.get('model') == 'mall.Grupo'], ['FRENOS'])
        updates = {row['name']: row for row in rows if row.get('model') == 'mall.PartType'}
        self.assertEqual(set(updates), {'DISCOS', 'PASTILLAS', 'ACEITE'})
        self.assertEqual(updates['PASTILLAS']['before'], {'grupo_id': None, 'category': 'Frenos'})
        code, out = self.run_cli('--apply', '--user', self.root.username)  # the catch-up after landing: nothing new
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.manifest()), len(rows))

    def test_what_needs_a_person_is_listed_and_left(self):
        blank = PartType.objects.create(category='X', name='SIN GRUPO')
        PartType.objects.filter(pk=blank.pk).update(grupo=None, category='')
        twice = PartType.objects.create(category='TMP', name='discos')  # FRENOS / DISCOS again, its text not normalized
        PartType.objects.filter(pk=twice.pk).update(grupo=None, category='FRENOS ')
        moved = PartType.objects.create(category='MOTOR', name='BIELAS')
        PartType.objects.filter(pk=moved.pk).update(category='TRANSMISION')  # a writer of the text alone
        code, out = self.run_cli('--dry-run')
        self.assertEqual(code, 0, out)
        self.assertIn('Subgrupos sin texto de grupo (1)', out)
        self.assertIn('Subgrupos repetidos en un grupo (1)', out)
        self.assertIn('FRENOS / discos: 2', out)
        self.assertIn('Subgrupos cuyo texto nombra otro grupo (1)', out)
        code, out = self.run_cli('--apply', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        self.assertEqual(PartType.objects.filter(pk=blank.pk).values_list('grupo', 'category').get(), (None, ''))
        self.assertEqual(PartType.objects.filter(pk=moved.pk).values_list('grupo__nombre', 'category').get(), ('MOTOR', 'TRANSMISION'))
        self.assertEqual(set(PartType.objects.filter(pk__in=[self.discs.pk, twice.pk]).values_list('grupo', flat=True)), {None})
        self.assertEqual(PartType.objects.get(pk=self.pads.pk).grupo.nombre, 'FRENOS')

    def test_the_lines_text_is_checked_against_its_subgrupo(self):
        Part.objects.filter(sku='S5-DISC').update(category='OTRO')
        make_line('S5-HALF', category='FRENOS')
        code, out = self.run_cli('--dry-run')
        self.assertEqual(code, 0, out)
        self.assertIn('Líneas cuyo texto difiere de su subgrupo: 1 (las facetas cambiarían', out)
        self.assertIn('clasificaciones parciales (grupo sin subgrupo): 1', out)

    def test_rollback_puts_back_what_the_apply_changed(self):
        self.run_cli('--apply', '--user', self.root.username)
        later = PartType.objects.create(category='FILTROS', name='AIRE')  # the code from S5 adds a subgrupo to a created grupo
        code, out = self.run_cli('--rollback', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        self.assertEqual(PartType.objects.filter(pk=self.pads.pk).values_list('grupo', 'category').get(), (None, 'Frenos'))
        self.assertEqual(PartType.objects.filter(pk=self.discs.pk).values_list('grupo', 'category').get(), (None, 'FRENOS'))
        self.assertEqual(list(Grupo.objects.values_list('nombre', flat=True)), ['FILTROS'])  # it has a subgrupo since: kept
        self.assertIn('grupo FILTROS: cambió o tiene subgrupos', out)
        self.assertEqual(PartType.objects.get(pk=later.pk).grupo.nombre, 'FILTROS')
        code, out = self.run_cli('--rollback')  # again, and without --user (plan §1.2.3): nothing left to put back
        self.assertEqual(code, 0, out)
        self.assertIn('0 subgrupos restaurados · 0 grupos borrados', out)
        self.assertEqual(self.run_cli('--rollback', '--user', 'nadie')[0], 1)

    def test_rollback_keeps_a_subgrupo_changed_since(self):
        self.run_cli('--apply', '--user', self.root.username)
        PartType.objects.filter(pk=self.oil.pk).update(grupo=grupo_for('MOTOR'), category='MOTOR')
        code, out = self.run_cli('--rollback', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        self.assertIn('subgrupo ACEITE', out)
        self.assertEqual(PartType.objects.get(pk=self.oil.pk).grupo.nombre, 'MOTOR')

    def test_rollback_refuses_while_0082_is_applied(self):
        with patch.object(s5_grupos, 'tightened', return_value=True):
            code, out = self.run_cli('--rollback', '--user', self.root.username)
        self.assertEqual(code, 1)
        self.assertIn('0082', out)

    def test_a_manifest_of_another_database_is_refused(self):
        with open(os.path.join(self.output.name, 'manifest.jsonl'), 'w', encoding='utf-8') as handle:
            handle.write(json.dumps({'stage': 's5', 'database': 'partsmall_rehearsal', 'action': 'run'}) + '\n')
        code, out = self.run_cli('--apply', '--user', self.root.username)
        self.assertEqual(code, 1)
        self.assertIn('otra base', out)
        self.assertFalse(Grupo.objects.exists())
