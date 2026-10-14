"""python -m mall.refactor.s4_library (inventory refactor S4): one Reference per OEMReference (uid = its id) on the line that carries it
or on a line of its own in referencia (one per clave and familia root), the carriers (an existing (clave, maker) Reference, a brandless
main, a familia brand, an sku_name link, two lines and their case), the sources, the re-pointing of what hangs from the numbers, the
blank texts of SKU lines, the manifest, the re-run, the rollback, and the finder's and the cases' fingerprints."""
import io
import json
import os
import tempfile
from unittest.mock import patch

from django.test import TestCase

from . import references
from .brand_models import Brand
from .catalog_suffixes import suffix_table
from .matching_models import MatchingCase
from .models import Part, Reference, ReferenceSource
from .oem_finder import OEMFinder, load_snapshot
from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, OEMReference, PartOEMLink
from .refactor import s4_library
from .test_factories import Fresh, before_s4_tighten, make_legacy_number, make_line, make_reference, make_user


class BackfillTests(Fresh, TestCase):
    """python -m mall.refactor.s4_library on the library as S2b leaves it: OEMReference rows, catalog lines with their mains."""

    def setUp(self):
        super().setUp()
        self.root = make_user('s4-cli', superuser=True)
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        patcher = patch.dict(os.environ, {'REFACTOR_OUTPUT': self.output.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        before_s4_tighten()  # the backfill runs between 0079 and 0080
        toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        Brand.objects.create(nombre='LEXUS', tipo='vehiculo', familia=toyota)
        # Carriers: an existing (clave, maker) alterno, a brandless main, a familia brand, an sku_name link, two lines.
        self.alterno_line = make_line('BOMBA-1')
        self.alterno = make_reference(self.alterno_line, '17801-30070', brand='TOYOTA', tipo='oem', source='CATÁLOGO')
        self.existing = make_legacy_number('TOYOTA', '1780130070', printed_forms=['17801-30070'], source_kind='catalog_approval', citation='',
                                           description='FILTRO AIRE', part_type='FILTRO AIRE')
        self.brandless = make_line('16100-79445', description='BOMBA AGUA')
        self.under_main = make_legacy_number('TOYOTA', '1610079445', printed_forms=['16100-79445'], source_kind='aftermarket_catalog',
                                             citation='GMB 2016', detail={'catalog': 'gmb'}, part_type='BOMBAS DE AGUA', applications='HILUX')
        self.familia_line = make_line('43512-60150-X')
        make_reference(self.familia_line, '43512-60150', brand='TOYOTA', tipo='oem')
        self.lexus = make_legacy_number('LEXUS', '4351260150', printed_forms=['43512-60150'], source_kind='aftermarket_catalog', citation='ATE')
        self.named = make_line('16100-39315-NPW')
        self.by_name = make_legacy_number('TOYOTA', '1610039315', printed_forms=['16100-39315'], source_kind='aftermarket_catalog', citation='GMB 2016')
        PartOEMLink.objects.create(part=self.named, legacy_reference=self.by_name, source='sku_name')
        self.shared_line = make_line('SHARED-OWN')
        self.shared_brandless = make_reference(self.shared_line, '90915-YZZE1')
        self.shared_named = make_line('90915-YZZE1-DENSO')
        self.shared = make_legacy_number('TOYOTA', '90915YZZE1', printed_forms=['90915-YZZE1'], source_kind='price_list', citation='LISTA')
        PartOEMLink.objects.create(part=self.shared_named, legacy_reference=self.shared, source='sku_name')
        # No carrier: a TOYOTA/LEXUS pair (one line), a KIA number, and one superseded by another.
        self.solo_toyota = make_legacy_number('TOYOTA', '0446560010', printed_forms=['04465-60010'], source_kind='aftermarket_catalog',
                                              citation='ATE', part_type='DISCOS DE FRENO', description='DISCO')
        self.solo_lexus = make_legacy_number('LEXUS', '0446560010', printed_forms=['04465-60010'], source_kind='oem_finder', citation='algo:x')
        self.kia = make_legacy_number('KIA', '517502S000', printed_forms=['51750-2S000'], source_kind='manual', citation='MANUAL',
                                      detail={'verified': True}, part_type='CUBOS DE RUEDA')
        self.old = make_legacy_number('KIA', '517502S001', printed_forms=['51750-2S001'], source_kind='manual', citation='MANUAL')
        OEMReference.objects.filter(pk=self.old.pk).update(superseded_by=self.kia)
        self.disputed = make_legacy_number('KIA', '517502S002', source_kind='manual', citation='M')
        OEMReference.objects.filter(pk=self.disputed.pk).update(status='disputed', dispute_note='NO ES CUBO')
        # Aftermarket codes, a tombstone and a catalog link hang from the numbers.
        self.cross = OEMCrossReference.objects.create(legacy_reference=self.under_main, brand='GMB', code='GWT-78A', citations=['GMB 2016'])
        self.tomb = OEMCrossReferenceRetired.objects.create(legacy_reference=self.kia, brand='X', number='X1', code='X-1')
        self.link = PartOEMLink.objects.create(part=self.alterno_line, legacy_reference=self.solo_toyota, source='catalog', citations=['ATE'])

    def run_cli(self, *args):
        out = io.StringIO()
        code = s4_library.main([*args], stdout=out)
        return code, out.getvalue()

    def manifest(self):
        with open(os.path.join(self.output.name, 'manifest.jsonl'), encoding='utf-8') as handle:
            return [json.loads(line) for line in handle]

    def test_the_dry_run_plans_every_carrier_and_writes_nothing(self):
        counts = (Part.objects.count(), Reference.all_objects.count(), ReferenceSource.objects.count())
        result = s4_library.plan()
        actions = {(entry['maker'], entry['code']): (entry['action'], entry.get('kind')) for entry in result['numbers']}
        self.assertEqual(actions[('TOYOTA', '1780130070')], ('existente', None))
        self.assertEqual(actions[('TOYOTA', '1610079445')], ('nueva_en_linea', 'principal_sin_marca'))
        self.assertEqual(actions[('LEXUS', '4351260150')], ('nueva_en_linea', 'familia'))
        self.assertEqual(actions[('TOYOTA', '1610039315')], ('nueva_en_linea', 'nombre_sku'))
        self.assertEqual(actions[('TOYOTA', '90915YZZE1')], ('nueva_en_linea', 'codigo_sin_marca'))
        self.assertEqual(actions[('KIA', '517502S000')], ('referencia', 'referencia'))
        final = result['final']
        self.assertEqual((final['numeros'], final['existente'], final['sin_portador'], final['lineas_referencia'], final['lineas_dos_numeros']),
                         (10, 1, 5, 4, 1))
        self.assertEqual((final['varios_portadores'], final['casos'], final['equivalencias'], final['vinculos']), (1, 1, 1, 3))
        self.assertEqual(counts, (Part.objects.count(), Reference.all_objects.count(), ReferenceSource.objects.count()))
        code, out = self.run_cli('--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('Simulación · S4', out)

    def test_apply_makes_one_reference_per_number_and_re_points_what_hangs_from_them(self):
        code, out = self.run_cli('--apply', '--user', 's4-cli')
        self.assertEqual(code, 0, out)
        for number in OEMReference.objects.all():  # C4: one Reference per OEMReference, uid = its id
            ref = Reference.all_objects.get(uid=number.pk)
            self.assertEqual((ref.brand, ref.tipo, references.clave(ref.code) in references.library_claves(number.code, number.printed_forms)),
                             (number.manufacturer, 'oem', True), number)
        self.alterno.refresh_from_db()
        self.assertEqual((self.alterno.uid, self.alterno.detail['description'], self.alterno.part_id), (self.existing.pk, 'FILTRO AIRE', self.alterno_line.pk))
        self.assertNotIn('origen', self.alterno.detail)
        on_main = Reference.objects.get(uid=self.under_main.pk)
        self.assertEqual((on_main.part_id, on_main.is_main, on_main.detail['origen']), (self.brandless.pk, False, 'biblioteca'))
        self.assertEqual(references.main_of(self.brandless).brand, '')
        self.brandless.refresh_from_db()
        self.assertEqual((self.brandless.subcategory, self.brandless.aplicaciones_texto, self.brandless.field_sources['subgrupo']['source']),
                         ('BOMBAS DE AGUA', 'HILUX', 'biblioteca'))  # blank fields of a SKU line filled from its number
        self.assertEqual(Reference.objects.get(uid=self.lexus.pk).part_id, self.familia_line.pk)
        self.assertEqual(Reference.objects.get(uid=self.by_name.pk).part_id, self.named.pk)
        shared = Reference.objects.get(uid=self.shared.pk)
        self.assertEqual((shared.part_id, shared.estado), (self.shared_line.pk, 'verificado'))  # the brandless holder keeps it
        case = MatchingCase.objects.get(method='shared_oem_number')
        self.assertEqual({row['id'] for row in [*case.sources, *case.candidates]}, {str(self.shared_line.pk), str(self.shared_named.pk)})
        # The TOYOTA/LEXUS pair shares one line in referencia; TOYOTA's number is its main.
        pair = Reference.objects.get(uid=self.solo_toyota.pk)
        self.assertEqual((pair.is_main, pair.part.estado, pair.part.sku, pair.part.subcategory), (True, 'referencia', '04465-60010', 'DISCOS DE FRENO'))
        lexus = Reference.objects.get(uid=self.solo_lexus.pk)
        self.assertEqual((lexus.part_id, lexus.is_main, lexus.estado), (pair.part_id, False, 'inferido'))
        kia, old = Reference.objects.get(uid=self.kia.pk), Reference.objects.get(uid=self.old.pk)
        self.assertEqual((kia.estado, old.reemplazado_por_id, kia.part.subcategory), ('verificado', kia.pk, 'CUBOS DE RUEDA'))
        disputed = Reference.objects.get(uid=self.disputed.pk)
        self.assertEqual((disputed.estado, disputed.dispute_note), ('disputa', 'NO ES CUBO'))
        # Sources, re-pointing.
        source = ReferenceSource.objects.get(reference=on_main, citation='GMB 2016')
        self.assertEqual((source.kind, source.detail['legacy_kind'], source.detail['catalog']), ('catalogo', 'aftermarket_catalog', 'gmb'))
        self.assertEqual(ReferenceSource.objects.get(reference=lexus).kind, 'buscador')
        self.cross.refresh_from_db()
        self.tomb.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual((self.cross.reference_id, self.tomb.reference_id, self.link.reference_id), (on_main.pk, kia.pk, pair.pk))
        self.assertFalse(OEMCrossReference.objects.filter(reference__isnull=True).exists())
        self.assertFalse(PartOEMLink.objects.filter(reference__isnull=True).exists())
        # Re-running changes nothing new.
        rows = len(self.manifest())
        code, out = self.run_cli('--apply', '--user', 's4-cli')
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.manifest()), rows, out)  # nothing to write: no manifest line either

    def test_rollback_puts_the_library_back(self):
        before = (Part.objects.count(), Reference.all_objects.count(), ReferenceSource.objects.count(), MatchingCase.objects.count())
        uid = self.alterno.uid
        self.assertEqual(self.run_cli('--apply', '--user', 's4-cli')[0], 0)
        with patch.object(s4_library, 'schema', return_value={'tipo': True, 'ref_id': True, 'mains': True, 'tightened': False}):
            code, out = self.run_cli('--rollback', '--user', 's4-cli')
        self.assertEqual(code, 0, out)
        self.assertEqual(before, (Part.objects.count(), Reference.all_objects.count(), ReferenceSource.objects.count(), MatchingCase.objects.count()), out)
        self.alterno.refresh_from_db()
        self.cross.refresh_from_db()
        self.assertEqual((self.alterno.uid, self.cross.reference_id), (uid, None))

    def test_the_finder_and_the_cases_keep_their_fingerprints(self):
        from .matching_engine import part_row
        from .oem_finder import part_snapshot
        filled = {str(pk) for pk in s4_library.plan()['fills']}  # lines whose blank subgrupo or applications the library fills
        self.assertEqual(filled, {str(self.brandless.pk), str(self.alterno_line.pk)})
        finder = OEMFinder(load_snapshot(), suffix_table())
        before = {pk: (result.get('tier'), part_snapshot(finder, pk)) for pk, result in finder.evaluate().items() if pk not in filled}
        rows_before = {str(line.pk): part_row(line) for line in Part.objects.prefetch_related('codes') if str(line.pk) not in filled}
        self.assertEqual(self.run_cli('--apply', '--user', 's4-cli')[0], 0)
        finder = OEMFinder(load_snapshot(), suffix_table())
        after = {pk: (result.get('tier'), part_snapshot(finder, pk)) for pk, result in finder.evaluate().items() if pk not in filled}
        self.assertEqual(before, after)  # lines in referencia and the numbers S4 puts on lines are library data, never claimants
        rows_after = {str(line.pk): part_row(line) for line in Part.objects.filter(pk__in=list(rows_before)).prefetch_related('codes')}
        self.assertEqual(rows_before, rows_after)

    def test_apply_refuses_without_s2b_or_0079(self):
        with patch.object(s4_library, 'schema', return_value={'tipo': True, 'ref_id': False, 'mains': True, 'tightened': False}):
            code, out = self.run_cli('--apply', '--user', 's4-cli')
        self.assertEqual(code, 1)
        self.assertIn('Faltan S2b', out)


class TightenTests(TestCase):
    """0080_library_lines_checks catches up what the code before S4 wrote (reference_id only) and stops for what needs a person."""

    def setUp(self):
        before_s4_tighten()  # the state 0080 runs in

    def catch_up(self):
        from .test_factories import run_migration_function
        return run_migration_function('0080_library_lines_checks', 'catch_up')

    def test_rows_old_code_wrote_are_re_pointed_and_one_key_keeps_one_row(self):
        number = make_legacy_number('TOYOTA', '1610079445', printed_forms=['16100-79445'], source_kind='aftermarket_catalog', citation='GMB')
        ref = references.add_library_number('TOYOTA', '16100-79445', source_kind='aftermarket_catalog', citation='GMB')[0]
        Reference.all_objects.filter(pk=ref.pk).update(uid=number.pk)  # as the backfill made it
        old = OEMCrossReference.objects.create(legacy_reference=number, brand='AISIN', code='WPT-1', citations=['GMB · pág. 1'])
        new = OEMCrossReference.objects.create(reference=ref, brand='AISIN', code='WPT 1', citations=['ATE · pág. 2'])
        line = make_line('BOMBA-1')
        link = PartOEMLink.objects.create(part=line, legacy_reference=number, source='catalog', citations=['GMB'])
        self.catch_up()
        old.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual((old.reference_id, old.citations, link.reference_id), (ref.pk, ['GMB · pág. 1', 'ATE · pág. 2'], ref.pk))
        self.assertFalse(OEMCrossReference.objects.filter(pk=new.pk).exists())  # the older row keeps the key, the citations add up
        self.catch_up()  # idempotent

    def test_a_number_without_its_reference_stops_the_migration(self):
        make_legacy_number('KIA', '517502S000', source_kind='manual', citation='M')
        with self.assertRaisesMessage(RuntimeError, '1 números de la tabla OEM sin su referencia'):
            self.catch_up()
