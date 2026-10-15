"""Inventory refactor S10, steps 1 and 2 (0086): the code of this stage writes only a line's subgrupo (part_type), but the code before
S10 reads the text columns back (its Part.save resolves the subgrupo from mall_part.category / subcategory), so 0086's triggers keep
that text, and PartType.category, in step with the subgrupo whatever writes it. A code-only revert then finds every line classified as
it is. The columns, and these tests, go with 0089."""
from django.db import connection
from django.test import TestCase

from .lines import set_subgrupo
from .models import Part
from .technical_data import part_type_for
from .technical_models import Grupo, PartType
from .test_factories import make_line


def text_of(line):
    with connection.cursor() as cursor:
        cursor.execute('SELECT category, subcategory FROM mall_part WHERE id = %s', [line.pk])
        return cursor.fetchone()


def grupo_text(part_type):
    with connection.cursor() as cursor:
        cursor.execute('SELECT category FROM mall_parttype WHERE id = %s', [part_type.pk])
        return cursor.fetchone()[0]


class SubgrupoTextTests(TestCase):
    def setUp(self):
        self.pads = part_type_for('FRENOS', 'PASTILLAS')
        self.filters = part_type_for('FILTROS', 'FILTRO DE ACEITE')

    def test_a_new_subgrupo_and_a_new_line_carry_the_text(self):
        self.assertEqual((grupo_text(self.pads), grupo_text(self.filters)), ('FRENOS', 'FILTROS'))
        line = make_line('S10-TEXT-1', part_type=self.pads)
        self.assertEqual(text_of(line), ('FRENOS', 'PASTILLAS'))
        self.assertEqual(text_of(make_line('S10-TEXT-2')), ('', ''))

    def test_a_line_moved_to_another_subgrupo_or_out_of_any_follows(self):
        line = make_line('S10-TEXT-3', part_type=self.pads)
        set_subgrupo(line, self.filters)
        line.save()
        self.assertEqual(text_of(line), ('FILTROS', 'FILTRO DE ACEITE'))
        Part.objects.filter(pk=line.pk).update(part_type=self.pads)  # a queryset update too
        self.assertEqual(text_of(line), ('FRENOS', 'PASTILLAS'))
        line.refresh_from_db()
        set_subgrupo(line, None)
        line.save()
        self.assertEqual(text_of(line), ('', ''))

    def test_text_written_with_the_subgrupo_cleared_stays(self):
        # The code before S10 keeps a partial classification (a grupo typed without a subgrupo yet) in the text.
        line = make_line('S10-TEXT-4', part_type=self.pads)
        with connection.cursor() as cursor:
            cursor.execute("UPDATE mall_part SET part_type_id = NULL, category = 'FRENOS', subcategory = '' WHERE id = %s", [line.pk])
        self.assertEqual(text_of(line), ('FRENOS', ''))

    def test_a_renamed_or_moved_subgrupo_and_a_renamed_grupo_reach_the_lines(self):
        line = make_line('S10-TEXT-5', part_type=self.pads)
        self.pads.name = 'PASTILLAS DE FRENO'
        self.pads.save()
        self.assertEqual(text_of(line), ('FRENOS', 'PASTILLAS DE FRENO'))
        PartType.objects.filter(pk=self.pads.pk).update(grupo=self.filters.grupo)
        self.assertEqual((grupo_text(self.pads), text_of(line)), ('FILTROS', ('FILTROS', 'PASTILLAS DE FRENO')))
        grupo = Grupo.objects.get(pk=self.filters.grupo_id)
        grupo.nombre = 'filtración'
        grupo.save()
        self.assertEqual((grupo_text(self.filters), grupo_text(self.pads)), ('FILTRACIÓN', 'FILTRACIÓN'))
        self.assertEqual(text_of(line), ('FILTRACIÓN', 'PASTILLAS DE FRENO'))
