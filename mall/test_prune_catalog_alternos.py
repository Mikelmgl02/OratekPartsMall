import csv
import io
import os
import tempfile

from django.test import TestCase

from . import prune_catalog_alternos as prune
from .models import Part, PartCode
from .oem_links import store_cross_references
from .oem_reference import upsert_reference
from .test_oem_finder import Fresh

CATALOG = 'GMB Water Pump Catalog 2016'
PAGE = f'{CATALOG} · pág. 212'


class PruneTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        ref = upsert_reference('TOYOTA', '16100-39315', source_kind='aftermarket_catalog', citation=CATALOG, detail={'catalog': 'gmb_wp_2016'})[0]
        store_cross_references({(ref.pk, 'GMB', 'GWT-41A'): [PAGE], (ref.pk, 'AISIN', 'WPT-111'): [PAGE]})
        self.copied = Part.objects.create(sku='16100-39315-RAZ', description='BOMBA AGUA TOY 22R')  # its code names the number
        self.copies = [self.code(self.copied, 'GMB', 'GWT-41A'), self.code(self.copied, 'AISIN', 'WPT 111')]
        self.typed = self.code(self.copied, 'FEBEST', 'TM-1', source='FICHA TÉCNICA')  # typed by a person
        self.oem = self.code(self.copied, 'TOYOTA', '16100-39315', ref_type='oem', source=PAGE)
        self.lone = Part.objects.create(sku='BOMBA-B', description='BOMBA AGUA')  # reaches no number
        self.unreached = self.code(self.lone, 'GMB', 'GWT-71A')
        self.mixed = Part.objects.create(sku='16100-39315-GMB', description='BOMBA AGUA TOY 22R')
        self.mixed_codes = [self.code(self.mixed, 'GMB', 'GWT 41A'), self.code(self.mixed, 'AIRTEX', 'AW-1')]

    def code(self, part, brand, code, *, ref_type='company', source=PAGE):
        return PartCode.objects.create(part=part, brand=brand, code=code, ref_type=ref_type, reference_source=source)

    def test_only_catalog_copies_a_sku_reaches_through_its_numbers_are_removed(self):
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False) as handle:
            path = handle.name
        try:
            self.assertEqual(prune.run(report_path=path, stdout=io.StringIO()), {'remove': 2, 'removed': 0, 'review_skus': 2, 'review_alternos': 3})
            self.assertEqual(PartCode.objects.count(), 7)  # a dry run removes nothing
            with open(path, encoding='utf-8') as report:
                rows = list(csv.DictReader(report))
        finally:
            os.unlink(path)
        self.assertEqual(sorted((r['accion'], r['sku'], r['codigo']) for r in rows), [
            ('retirado', '16100-39315-RAZ', 'GWT-41A'), ('retirado', '16100-39315-RAZ', 'WPT 111'),
            ('revisar', '16100-39315-GMB', 'AW-1'), ('revisar', '16100-39315-GMB', 'GWT 41A'), ('revisar', 'BOMBA-B', 'GWT-71A')])
        self.assertEqual(prune.run(apply=True, stdout=io.StringIO())['removed'], 2)
        self.assertEqual(set(PartCode.objects.values_list('pk', flat=True)),
                         {self.typed.pk, self.oem.pk, self.unreached.pk, *[code.pk for code in self.mixed_codes]})
        self.assertEqual(prune.run(apply=True, stdout=io.StringIO())['removed'], 0)  # nothing more on a second run

    def test_a_grouped_sku_is_left_to_the_sku_it_joined(self):
        Part.objects.filter(pk=self.copied.pk).update(merged_into=self.lone, active=False)
        self.assertEqual(prune.run(stdout=io.StringIO())['remove'], 0)
