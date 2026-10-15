"""Grupo / Subgrupo (inventory refactor S5): the Grupo table, PartType.grupo kept in step with its text, the writers that resolve or
create a grupo (mall.technical_data, mall.lines.set_subgrupo), and the backfill CLI (python -m mall.refactor.s5_grupos)."""

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from rest_framework.test import APITestCase

from . import lines
from .models import Grupo, Part, PartType, TechnicalTemplate
from .technical_data import bind_part_types, classification, grupo_for, part_type_for
from .test_factories import make_line, make_user, run_migration_function


def allow_no_grupo():
    """Inside a test's transaction, so it comes back when the test rolls back: lets a subgrupo have no grupo, as 0081 leaves every
    subgrupo before the backfill (0082 makes the grupo required). Call it before the test writes any subgrupo: PostgreSQL refuses to
    alter a table with pending (deferred foreign key) trigger events."""
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

    def test_renaming_a_grupo_renames_the_text_that_follows_it(self):
        """Until S10 a line's next save looks its subgrupo up by its text: a rename that left the text behind would bring the old grupo
        back and move the line there."""
        line = make_line('S5-RENAME', category='FRENOS', subcategory='DISCOS')
        pads = part_type_for('FRENOS', 'PASTILLAS')
        other = make_line('S5-OTHER', category='FILTROS', subcategory='ACEITE')
        grupo = Grupo.objects.get(nombre='FRENOS')
        grupo.nombre = ' frenado '
        grupo.save()
        self.assertEqual(grupo.nombre, 'FRENADO')
        self.assertEqual(dict(PartType.objects.values_list('name', 'category')), {'DISCOS': 'FRENADO', 'PASTILLAS': 'FRENADO', 'ACEITE': 'FILTROS'})
        self.assertEqual(Part.objects.filter(pk=line.pk).values_list('category', 'subcategory').get(), ('FRENADO', 'DISCOS'))
        self.assertEqual(Part.objects.filter(pk=other.pk).values_list('category', flat=True).get(), 'FILTROS')
        line = Part.objects.get(pk=line.pk)
        line.description = 'DISCO DE FRENO'
        line.save()  # a full save resolves the subgrupo by its text again
        self.assertEqual(Part.objects.get(pk=line.pk).part_type_id, PartType.objects.get(name='DISCOS').pk)
        self.assertEqual(set(Grupo.objects.values_list('nombre', flat=True)), {'FRENADO', 'FILTROS'})
        self.assertEqual(part_type_for('frenado', 'pastillas'), pads)


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


class GrupoTightenTests(TestCase):
    """0082: every subgrupo has a grupo and one name per grupo; its repair attaches the subgrupos old writers leave without one, and
    stops on what needs a person."""

    def test_a_subgrupo_needs_a_grupo_and_a_grupo_one_name(self):
        discs = part_type_for('FRENOS', 'DISCOS')
        with self.assertRaises(IntegrityError), transaction.atomic():
            PartType.objects.filter(pk=discs.pk).update(grupo=None)
        with self.assertRaises(IntegrityError), transaction.atomic():  # another text, so only the new constraint can refuse it
            PartType.objects.bulk_create([PartType(grupo=discs.grupo, category='FRENOS 2', name='discos')])

    def test_the_repair_attaches_what_old_writers_leave_and_passes_on_an_empty_catalog(self):
        run_migration_function('0082_grupo_checks', 'repair_grupos')  # nothing to repair
        allow_no_grupo()
        discs = part_type_for('FRENOS', 'DISCOS')
        oil = PartType.objects.create(category='FILTROS', name='ACEITE')
        PartType.objects.filter(pk=discs.pk).update(grupo=None, category='frenos')  # the code before S5, case aside
        PartType.objects.filter(pk=oil.pk).update(grupo=None)
        Grupo.objects.filter(nombre='FILTROS').delete()
        for _ in range(2):  # idempotent
            run_migration_function('0082_grupo_checks', 'repair_grupos')
        self.assertEqual({name: (grupo, category) for name, grupo, category in PartType.objects.values_list('name', 'grupo__nombre', 'category')},
                         {'DISCOS': ('FRENOS', 'FRENOS'), 'ACEITE': ('FILTROS', 'FILTROS')})
        blank = PartType.objects.create(category='X', name='SIN GRUPO')
        PartType.objects.filter(pk=blank.pk).update(grupo=None, category='')
        moved = PartType.objects.create(category='MOTOR', name='BIELAS')
        PartType.objects.filter(pk=moved.pk).update(category='TRANSMISION')
        twice = PartType.objects.create(category='TMP', name='discos')
        PartType.objects.filter(pk=twice.pk).update(grupo=None, category='FRENOS ')
        with self.assertRaisesMessage(RuntimeError, '1 subgrupos sin grupo y sin texto de grupo; 1 subgrupos cuyo texto nombra otro grupo '
                                                    'que el suyo; 2 subgrupos repetidos en un grupo'):
            run_migration_function('0082_grupo_checks', 'repair_grupos')
