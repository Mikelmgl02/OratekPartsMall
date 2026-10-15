"""Grupo / Subgrupo (inventory refactor S5, S10): the Grupo table, PartType.grupo as a subgrupo's only grupo (S10: its text and the
lines' texts are gone), the writers that resolve or create a grupo (mall.technical_data, mall.lines.set_subgrupo)."""

from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APITestCase

from . import lines
from .models import Grupo, Part, PartType, TechnicalTemplate
from rest_framework.exceptions import ValidationError

from .technical_data import classification, grupo_for, part_type_for, subgrupos_for, text_subgrupo
from .test_factories import make_line, make_user


class GrupoTests(TestCase):
    def test_a_grupo_is_one_per_name_ignoring_case(self):
        frenos = grupo_for(' frenos ')
        self.assertEqual(frenos.nombre, 'FRENOS')
        self.assertEqual(grupo_for('Frenos'), frenos)
        self.assertEqual(Grupo.objects.count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Grupo.objects.create(nombre='frenos')

    def test_a_subgrupos_grupo_is_its_fk_and_its_text_is_no_longer_written(self):
        """S10: PartType.category is its grupo's nombre (a property); the code neither reads nor writes the old text column (0086's
        trigger writes it for the code before S10: mall.test_s10_legacy_text)."""
        pads = PartType.objects.create(grupo=grupo_for('frenos'), name='pastillas')
        self.assertEqual((pads.name, pads.category), ('PASTILLAS', 'FRENOS'))
        self.assertEqual(PartType.objects.filter(pk=pads.pk).values_list('legacy_category', flat=True).get(), 'FRENOS')
        pads.grupo = grupo_for('FRENADO')
        pads.save(update_fields=['grupo'])
        self.assertEqual(PartType.objects.select_related('grupo').get(pk=pads.pk).category, 'FRENADO')

    def test_renaming_a_grupo_renames_what_its_lines_show(self):
        line = make_line('S5-RENAME', part_type=part_type_for('FRENOS', 'DISCOS'))
        pads = part_type_for('FRENOS', 'PASTILLAS')
        other = make_line('S5-OTHER', part_type=part_type_for('FILTROS', 'ACEITE'))
        grupo = Grupo.objects.get(nombre='FRENOS')
        grupo.nombre = ' frenado '
        grupo.save()
        self.assertEqual(grupo.nombre, 'FRENADO')
        self.assertEqual(classification(Part.objects.select_related('part_type__grupo').get(pk=line.pk)), ('FRENADO', 'DISCOS'))
        self.assertEqual(classification(Part.objects.select_related('part_type__grupo').get(pk=other.pk)), ('FILTROS', 'ACEITE'))
        self.assertEqual(set(Grupo.objects.values_list('nombre', flat=True)), {'FRENADO', 'FILTROS'})
        self.assertEqual(part_type_for('frenado', 'pastillas'), pads)


class SubgrupoWriterTests(TestCase):
    def test_a_line_takes_its_subgrupo_and_shows_its_names(self):
        line = make_line('S5-A', part_type=part_type_for(' frenos ', 'discos'))
        self.assertEqual((line.part_type.grupo.nombre, line.part_type.name), ('FRENOS', 'DISCOS'))
        self.assertEqual((line.category, line.subcategory), ('FRENOS', 'DISCOS'))
        self.assertTrue(TechnicalTemplate.objects.filter(part_type=line.part_type).exists())
        self.assertEqual(make_line('S5-B', part_type=part_type_for('FRENOS', 'DISCOS')).part_type_id, line.part_type_id)
        self.assertEqual(Part.objects.filter(pk=line.pk).values_list('legacy_category', 'legacy_subcategory').get(), ('FRENOS', 'DISCOS'))

    def test_a_pair_of_texts_names_a_subgrupo_and_half_a_pair_is_refused(self):
        part_type = part_type_for('suspension', 'muelles')
        self.assertEqual(text_subgrupo('Suspension', ' muelles'), part_type)
        self.assertIsNone(text_subgrupo('', ''))
        with self.assertRaises(ValidationError):
            text_subgrupo('frenos', '')
        partial = make_line('S5-PARTIAL', category='frenos')  # the test factory drops half a pair
        self.assertEqual((partial.part_type_id, partial.category, partial.subcategory), (None, '', ''))
        self.assertEqual(classification(partial), ('', ''))
        with self.assertRaises(ValueError):
            part_type_for('', 'MUELLES')

    def test_set_subgrupo_and_the_projection_follow_the_fk(self):
        line = make_line('S5-SET')
        discs = part_type_for('FRENOS', 'DISCOS')
        fields = lines.set_subgrupo(line, discs)
        line.save(update_fields=fields)
        line = Part.objects.select_related('part_type__grupo').get(pk=line.pk)
        self.assertEqual((line.part_type_id, line.category, line.subcategory), (discs.pk, 'FRENOS', 'DISCOS'))
        lines.set_subgrupo(line, None)
        self.assertEqual((line.part_type, line.category, line.subcategory), (None, '', ''))
        discs.grupo = grupo_for('FRENADO')
        discs.save()
        self.assertEqual(lines.project_subgrupos([discs]), 1)  # its lines' Search Documents read the new names
        self.assertEqual(classification(Part.objects.select_related('part_type__grupo').get(pk=line.pk)), ('FRENADO', 'DISCOS'))

    def test_subgrupos_for_resolves_a_batch_in_a_few_queries(self):
        pairs = [('FRENOS', f'SUB {index % 3}') for index in range(30)]
        with self.assertNumQueries(7):  # grupos read, created, read; subgrupos read, created, read; templates
            found = subgrupos_for(pairs)
        self.assertEqual(len({part_type.pk for part_type in found.values()}), 3)
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
        PartType.objects.filter(pk=part_type.pk).update(legacy_category='TEXTO VIEJO')  # the FK is what the API shows
        self.assertEqual(self.client.get(f'{self.url}{part_type.pk}/').data['category'], 'CARROCERIA')
        duplicate = self.client.post(self.url, {'category': 'Carroceria', 'name': 'Faros'}, format='json')
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual([row['id'] for row in self.client.get(self.url, {'search': 'carroceria'}).data['results']], [str(part_type.pk)])

    def test_another_category_moves_the_subgrupo_and_its_lines_follow(self):
        line = make_line('S5-MOVE', part_type=part_type_for('FRENOS', 'DISCOS'))
        response = self.client.patch(f'{self.url}{line.part_type_id}/', {'category': 'frenado'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(PartType.objects.get(pk=line.part_type_id).grupo.nombre, 'FRENADO')
        self.assertEqual(Part.objects.filter(pk=line.pk).values_list('part_type__grupo__nombre', 'part_type__name').get(), ('FRENADO', 'DISCOS'))
        self.assertEqual(set(Grupo.objects.values_list('nombre', flat=True)), {'FRENOS', 'FRENADO'})


class GrupoTightenTests(TestCase):
    """0082: every subgrupo has a grupo and one name per grupo; its repair attaches the subgrupos old writers leave without one, and
    stops on what needs a person."""

    def test_a_subgrupo_needs_a_grupo_and_a_grupo_one_name(self):
        discs = part_type_for('FRENOS', 'DISCOS')
        with self.assertRaises(IntegrityError), transaction.atomic():
            PartType.objects.filter(pk=discs.pk).update(grupo=None)
        with self.assertRaises(IntegrityError), transaction.atomic():  # another text, so only the new constraint can refuse it
            PartType.objects.bulk_create([PartType(grupo=discs.grupo, name='discos')])
