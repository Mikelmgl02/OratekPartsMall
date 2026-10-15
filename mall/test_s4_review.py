"""Inventory refactor S4, review fixes: a short library number is found by its compact form (OPEL 5 69 036 is 569036) and written
without the punctuation an extract left at its edges, the rollback leaves what moved since the apply and gives the OEM table back what
the code wrote after landing, an absorbed number's codes and links follow it (and come back with Separar), a catalog replace drops a
number to a rejected line of its own and a later import brings it back, Editar celdas maps a subgrupo's name, a number variants hang
from never leaves its line, rule 3f locks no supplier, the automatic families and the import absorb or promote a line in referencia,
the Biblioteca refuses to dispute a catalog line's main, a code a person adds stops being library data, and the S0 tool that merged
the OEM table's twins refuses to write once S4 ran."""
import io
import json
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook
from rest_framework.test import APIClient

from . import catalog_identity, line_merge, references
from . import supplier_catalog_import as sci
from . import supplier_catalog_replace as scr
from .brand_models import Brand
from .catalog_grouping import merge_catalog_parts
from .lines import bulk_create_lines, create_line
from .models import CatalogMerge, Part, Reference
from .oem_reference_models import OEMCrossReference, OEMCrossReferenceRetired, OEMReference, PartOEMLink
from .refactor import s0_normalize_numbers, s4_library
from .supplier_import_models import SupplierInventoryImportJob
from .technical_data import part_type_for
from .test_factories import (Fresh, before_s4_tighten, make_legacy_number, make_line, make_reference, make_supplier, make_user,
                             make_variant)
from .test_supplier_catalog_import import extract, item

URL = '/api/v1/management/oem-references/'


class ShortNumberTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user('s4r-short', superuser=True)
        self.client = APIClient()
        self.client.force_authenticate(self.root)

    def test_a_short_number_is_the_library_number_of_its_compact_form(self):
        ref, created = references.add_library_number('OPEL', '5 69 036', printed=['5 69 036'], source_kind='aftermarket_catalog', citation='GMB')
        self.assertTrue(created)
        self.assertEqual((ref.code, ref.clave), ('5 69 036', '5 69 036'))  # a short number's clave is its printed spelling
        again, created = references.add_library_number('OPEL', '569036', source_kind='manual', citation='MANUAL')
        self.assertFalse(created)  # the same number (manufacturer, compact code), not a second line
        self.assertEqual(again.pk, ref.pk)
        self.assertEqual(Part.objects.filter(estado='referencia').count(), 1)
        rows = self.client.get(URL, {'q': '569036'}).json()
        self.assertEqual(rows['count'], 1)
        self.assertEqual(rows['results'][0]['code'], '569036')  # the legacy JSON keeps the compact number
        self.assertIn('5 69 036', rows['results'][0]['printed_forms'])
        self.assertEqual(sci.CatalogImport.library_ids(None, {('OPEL', '569036')}), {('OPEL', '569036'): ref.pk})
        self.assertEqual(Reference.objects.filter(references.compact_q(['569036'])).get().pk, ref.pk)
        self.assertEqual(Reference.objects.filter(references.compact_match_q('9036', exact=False)).get().pk, ref.pk)
        references.update(ref, estado='disputa', dispute_note='NO ES')
        self.assertEqual(catalog_identity.disputed_codes(['5-69-036']), {'569036'})
        supersede = self.client.patch(f'{URL}{ref.uid}/', {'expected_version': Reference.objects.get(pk=ref.pk).version,
                                                          'action': 'set_superseded_by', 'code': '5 69 037'}, format='json')
        self.assertEqual(supersede.status_code, 200, supersede.content)
        self.assertEqual(supersede.json()['superseded_by']['code'], '569037')

    def test_an_edge_comma_never_reaches_the_code(self):
        ref, _ = references.add_library_number('TOYOTA', '4405285', printed=['4405285,'], source_kind='aftermarket_catalog', citation='ATE')
        self.assertEqual((ref.code, ref.part.sku), ('4405285', '4405285'))
        self.assertIn('4405285,', ref.printed_forms)
        line = create_line('4405285', actor=self.root)  # Crear SKU with the number promotes its line
        self.assertEqual((line.pk, line.estado), (ref.part_id, 'publicada'))


class BackfillFixture(Fresh):
    def setUp(self):
        super().setUp()
        self.root = make_user('s4r-cli', superuser=True)
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        patcher = patch.dict(os.environ, {'REFACTOR_OUTPUT': self.output.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        before_s4_tighten()
        Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')

    def run_cli(self, *args):
        out = io.StringIO()
        with patch.object(s4_library, 'schema', return_value={'tipo': True, 'ref_id': True, 'mains': True, 'tightened': False}):
            code = s4_library.main([*args], stdout=out)
        return code, out.getvalue()

    def manifest(self):
        with open(os.path.join(self.output.name, 'manifest.jsonl'), encoding='utf-8') as handle:
            return [json.loads(line) for line in handle]


class BackfillReviewTests(BackfillFixture, TestCase):
    def test_the_backfill_writes_short_numbers_without_edge_punctuation_and_marks_the_pending_name_links(self):
        comma = make_legacy_number('TOYOTA', '4405285', printed_forms=['4405285,'], source_kind='aftermarket_catalog', citation='ATE')
        named = make_line('16100-79445', description='BOMBA AGUA')  # its codigo names the number, with no name link
        number = make_legacy_number('TOYOTA', '1610079445', printed_forms=['16100-79445'], source_kind='aftermarket_catalog', citation='GMB')
        self.assertEqual(self.run_cli('--apply', '--user', 's4r-cli')[0], 0)
        ref = Reference.objects.get(uid=comma.pk)
        self.assertEqual((ref.code, ref.part.sku, ref.printed_forms), ('4405285', '4405285', ['4405285,']))
        pending = Reference.objects.get(uid=number.pk)
        self.assertEqual((pending.part_id, pending.estado, pending.detail['regla_5b']['sku']), (named.pk, 'declarado', '16100-79445'))
        self.assertFalse(PartOEMLink.objects.filter(source='sku_name').exists())  # no link is created

    def test_a_line_an_open_case_shows_keeps_its_blank_subgrupo(self):
        from .matching_engine import part_row, save_case
        line = make_line('16100-79445', description='BOMBA AGUA')
        make_legacy_number('TOYOTA', '1610079445', printed_forms=['16100-79445'], source_kind='aftermarket_catalog', citation='GMB',
                           part_type='BOMBAS DE AGUA', applications='HILUX')
        other = make_line('BOMBA-OTRA', description='BOMBA AGUA')
        case = save_case(key='catalog:prueba', kind='catalog', sources=[part_row(other)], candidates=[part_row(line)], target=line.sku,
                         method='family_review', reason='prueba')
        planned = s4_library.plan()
        self.assertEqual(planned['final']['subgrupos_aplazados'], 1)
        self.assertNotIn('category', planned['fills'][line.pk]['after'])  # the applications are filled; part_row has none
        self.assertEqual(self.run_cli('--apply', '--user', 's4r-cli')[0], 0)
        line.refresh_from_db()
        self.assertEqual((line.subcategory, line.aplicaciones_texto), ('', 'HILUX'))
        refreshed = save_case(key='catalog:prueba', kind='catalog', sources=[part_row(other)], candidates=[part_row(Part.objects.get(pk=line.pk))],
                              target=line.sku, method='family_review', reason='prueba')
        self.assertEqual(refreshed.fingerprint, case.fingerprint)


class RollbackSafetyTests(BackfillFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.promoted = make_legacy_number('TOYOTA', '1610079445', printed_forms=['16100-79445'], source_kind='aftermarket_catalog', citation='GMB')
        self.absorbed = make_legacy_number('TOYOTA', '1780130070', printed_forms=['17801-30070'], source_kind='aftermarket_catalog', citation='GMB')
        self.free = make_legacy_number('TOYOTA', '0446560010', printed_forms=['04465-60010'], source_kind='aftermarket_catalog', citation='GMB')
        self.carrier = make_line('90915-YZZE1')  # its brandless main carries the number
        self.carried = make_legacy_number('TOYOTA', '90915YZZE1', printed_forms=['90915-YZZE1'], source_kind='price_list', citation='LISTA')
        self.assertEqual(self.run_cli('--apply', '--user', 's4r-cli')[0], 0)

    def test_the_rollback_leaves_what_moved_and_lists_it(self):
        promoted = Reference.objects.get(uid=self.promoted.pk)
        line = create_line('16100-79445', actor=self.root)  # Crear SKU promotes the number's line
        self.assertEqual(line.pk, promoted.part_id)
        sku = make_line('FILTRO-X')
        references.add(sku, '17801-30070', 'TOYOTA', tipo='oem', actor=self.root)  # rule 3f: its line is absorbed
        absorbed = Reference.objects.get(uid=self.absorbed.pk)
        self.assertEqual(absorbed.part_id, sku.pk)
        carried = Reference.objects.get(uid=self.carried.pk)
        references.set_main(self.carrier, carried, actor=self.root, reason='prueba')  # a carrier's number made its main
        free = Reference.objects.get(uid=self.free.pk)
        code, out = self.run_cli('--rollback', '--user', 's4r-cli')
        self.assertEqual(code, 0, out)
        self.assertEqual(references.main_of(line).pk, promoted.pk)  # the published line keeps its main (I1)
        self.assertEqual(Reference.objects.get(pk=absorbed.pk).part_id, sku.pk)  # the code the person added stays
        self.assertEqual(references.main_of(self.carrier).pk, carried.pk)
        self.assertFalse(Reference.all_objects.filter(pk=free.pk).exists())
        self.assertFalse(Part.objects.filter(pk=free.part_id).exists())
        deleted = {(row['model'], str(row['id'])) for row in self.manifest() if row.get('action') == 'delete'}
        self.assertIn(('mall.Reference', str(free.pk)), deleted)
        for model, pk in (('mall.Reference', promoted.pk), ('mall.Reference', absorbed.pk), ('mall.Reference', carried.pk),
                          ('mall.Part', line.pk)):
            self.assertNotIn((model, str(pk)), deleted)
        self.assertIn(f'referencia {promoted.pk}', out)
        again = self.run_cli('--rollback', '--user', 's4r-cli')[1]  # what was left is tried again, and listed again
        self.assertIn(f'referencia {promoted.pk}', again)

    def test_what_the_code_wrote_after_landing_goes_back_to_the_oem_table(self):
        promoted = Reference.objects.get(uid=self.promoted.pk)
        late = OEMCrossReference.objects.create(reference=promoted, brand='AISIN', code='WPT-9', number='WPT9', citations=['ATE'])
        young, _ = references.add_library_number('KIA', '58101-2SA00', source_kind='manual', citation='MANUAL', actor=self.root)
        orphan = OEMCrossReference.objects.create(reference=young, brand='MANDO', code='MBK-1', number='MBK1', citations=['MANDO'])
        code, out = self.run_cli('--rollback', '--user', 's4r-cli')
        self.assertEqual(code, 0, out)
        late.refresh_from_db()
        self.assertEqual((late.legacy_reference_id, late.reference_id), (self.promoted.pk, None))
        self.assertFalse(OEMCrossReference.objects.filter(pk=orphan.pk).exists())  # its number is younger than S4: no OEM table row
        recorded = [row for row in self.manifest() if row.get('model') == 'mall.OEMCrossReference' and row['id'] == orphan.pk]
        self.assertEqual((recorded[0]['action'], recorded[0]['before']['code']), ('delete', 'MBK-1'))
        self.assertIn('0 filas sin reference_id', out)
        self.assertFalse(Reference.all_objects.filter(pk=promoted.pk).exists())  # released, nothing points at it any more


class AbsorbRowsTests(Fresh, TestCase):
    def test_an_absorbed_numbers_codes_and_links_follow_it_and_come_back_with_separar(self):
        root = make_user('s4r-absorb', superuser=True)
        source, target, linked = make_line('LINEA-A'), make_line('LINEA-B'), make_line('ENLAZADA')
        ref = make_reference(source, '17801-30070', brand='TOYOTA', tipo='oem', detail={'origen': 'biblioteca'})  # a library number
        kept = make_reference(target, '17801-30070', brand='TOYOTA', tipo='aftermarket', estado='disputa',
                              dispute_note='El código 17801-30070 ya es de otro SKU (LINEA-A): caso de unificación.')
        moved = OEMCrossReference.objects.create(reference=ref, brand='AISIN', code='ARG-1', number='ARG1', citations=['ATE'])
        mine = OEMCrossReference.objects.create(reference=ref, brand='GMB', code='GWT-1', number='GWT1', citations=['ATE'])
        theirs = OEMCrossReference.objects.create(reference=kept, brand='GMB', code='GWT-1', number='GWT1', citations=['GMB'])
        tomb = OEMCrossReferenceRetired.objects.create(reference=ref, brand='X', code='X-1', number='X1')
        link = PartOEMLink.objects.create(part=linked, reference=ref, source='catalog', citations=['ATE'])
        merge = line_merge.merge_lines(target, [source], actor=root, reason='prueba')
        moved.refresh_from_db()
        tomb.refresh_from_db()
        link.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual((moved.reference_id, tomb.reference_id, link.reference_id), (kept.pk, kept.pk, kept.pk))
        self.assertEqual(theirs.citations, ['GMB', 'ATE'])  # a code the kept number has: it takes the citations
        self.assertEqual(Reference.objects.get(pk=kept.pk).tipo, 'oem')
        record = merge.sources[0]['absorbed'][0]
        self.assertEqual(record['library_rows']['codes'], [moved.pk])
        line_merge.split_line(target.pk, [{'id': str(source.pk), 'codes': [], 'items': [], 'images': []}], actor=root)
        moved.refresh_from_db()
        tomb.refresh_from_db()
        link.refresh_from_db()
        theirs.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual((moved.reference_id, tomb.reference_id, link.reference_id, mine.reference_id), (ref.pk, ref.pk, ref.pk, ref.pk))
        self.assertEqual(theirs.citations, ['GMB'])
        self.assertEqual(Reference.objects.get(pk=kept.pk).tipo, 'aftermarket')


class CatalogReplaceDropTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        make_user('root', superuser=True)
        old = extract([item('GWN-2A', 'water_pump', oem=[('21010-22222', 'NISSAN'), ('21010-33333', 'NISSAN')], cross_refs=[('AISIN', 'WPN-002')])])
        new = extract([item('GWN-2A', 'water_pump', oem=[('21010-22222', 'NISSAN')], cross_refs=[('AISIN', 'WPN-002')])])
        self.old, self.new = self.write(old), self.write(new)
        sci.run([self.old], apply=True, stdout=io.StringIO())

    def write(self, data):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8')
        json.dump(data, handle, ensure_ascii=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_a_dropped_number_waits_on_a_rejected_line_and_comes_back_with_its_codes(self):
        number = Reference.objects.get(references.library_q(), brand='NISSAN', clave='2101033333')
        self.assertTrue(OEMCrossReference.objects.filter(reference=number).exists())
        scr.run(self.new, apply=True, username='root', stdout=io.StringIO())
        number.refresh_from_db()
        line = Part.objects.get(pk=number.part_id)
        self.assertEqual((number.estado, number.is_main, line.estado, line.decision_reason), ('disputa', True, 'rechazada', 'bad_data'))
        self.assertIn(references.DROPPED, number.detail)
        self.assertFalse(OEMCrossReference.objects.filter(reference=number).exists())  # its codes went with it ...
        self.assertFalse(OEMCrossReferenceRetired.objects.filter(reference=number).exists())  # ... without a tombstone
        kept = Reference.objects.get(references.library_q(), brand='NISSAN', clave='2101022222')
        self.assertEqual(Part.objects.get(pk=kept.part_id).estado, 'referencia')
        sci.run([self.old], apply=True, stdout=io.StringIO())  # a catalog that prints it again brings it back
        number.refresh_from_db()
        self.assertEqual((number.estado, Part.objects.get(pk=number.part_id).estado), ('declarado', 'referencia'))
        self.assertNotIn(references.DROPPED, number.detail)
        self.assertTrue(OEMCrossReference.objects.filter(reference=number).exists())

    def test_a_familia_line_keeps_the_number_still_printed(self):
        toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        Brand.objects.create(nombre='LEXUS', tipo='vehiculo', familia=toyota)
        main, _ = references.add_library_number('TOYOTA', '04465-60010', source_kind='manual', citation='M')
        second = Reference.objects.get(pk=references.add(main.part_id, '04465-60010', 'LEXUS', tipo='oem', estado='declarado',
                                                         detail={'origen': 'biblioteca'}, absorb_library=False)[0].pk)
        dropped = references.drop_library_number(main, 'El catálogo ya no lo imprime.')
        second.refresh_from_db()
        self.assertEqual((second.part_id, second.is_main, Part.objects.get(pk=second.part_id).estado), (main.part_id, True, 'referencia'))
        self.assertNotEqual(dropped.part_id, main.part_id)
        self.assertEqual((dropped.estado, Part.objects.get(pk=dropped.part_id).estado), ('disputa', 'rechazada'))


class SubgrupoNameTests(Fresh, TestCase):
    def test_editar_celdas_maps_a_subgrupos_name(self):
        root = make_user('s4r-celdas', superuser=True)
        client = APIClient()
        client.force_authenticate(root)
        part_type_for('TRANSMISIÓN', 'CV JOINT')
        ref, _ = references.add_library_number('TOYOTA', '43460-69115', part_type='TERM CREM')
        self.assertIn('CV JOINT', client.get(f'{URL}part-types/').json()['results'])
        response = client.patch(f'{URL}{ref.uid}/', {'expected_version': Reference.objects.get(pk=ref.pk).version,
                                                     'action': 'edit', 'part_type': 'CV JOINT'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        line = Part.objects.get(pk=ref.part_id)
        self.assertEqual((line.category, line.subcategory), ('TRANSMISIÓN', 'CV JOINT'))
        self.assertNotIn('sin_mapa', line.field_sources.get('subgrupo', {}))


class ReviewGuardTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user('s4r-guard', superuser=True)

    def test_a_number_variants_hang_from_never_leaves_its_line(self):
        line = make_line('BOMBA-X')
        references.add_library_number('TOYOTA', '16100-39315')
        ref, _ = references.add(line, '16100-39315', 'TOYOTA', tipo='oem', actor=self.root)
        with patch.object(references, 'has_variants', return_value=True):
            with self.assertRaisesMessage(references.ReferenceConflict, 'tiene artículos de proveedores'):
                references.retire(ref, actor=self.root)
        self.assertEqual(Reference.objects.get(pk=ref.pk).part_id, line.pk)

    def test_rule_3f_takes_no_supplier_lock_and_ignores_an_import(self):
        supplier = make_supplier('S4R PROVEEDOR')
        line = make_line('FILTRO-Y')
        make_variant(supplier, 'INV-1', 'FILTRO-Y', 2, actor=self.root, line=line)
        SupplierInventoryImportJob.objects.create(supplier=supplier, filename='x.xlsx', status='importing', expires_at=timezone.now() + timedelta(hours=1))
        number, _ = references.add_library_number('TOYOTA', '17801-30070')
        ref, added = references.add(line, '17801-30070', 'TOYOTA', tipo='oem', actor=self.root)
        self.assertEqual(ref.pk, number.pk)  # the line in referencia was absorbed although the supplier is importing
        self.assertEqual(Part.objects.get(pk=line.pk).estado, 'publicada')
        self.assertNotIn('origen', ref.detail)  # the code a person added is the line's own from now on
        self.assertIn('adoptada', ref.detail)

    def test_the_biblioteca_does_not_dispute_a_catalog_lines_main(self):
        client = APIClient()
        client.force_authenticate(self.root)
        ref, _ = references.add_library_number('TOYOTA', '16100-79445')
        line = create_line('16100-79445', actor=self.root)
        response = client.patch(f'{URL}{ref.uid}/', {'expected_version': Reference.objects.get(pk=ref.pk).version, 'action': 'dispute',
                                                     'note': 'NO ES'}, format='json')
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(line.sku, json.dumps(response.json(), ensure_ascii=False))
        self.assertEqual(Reference.objects.get(pk=ref.pk).estado, 'declarado')

    def test_the_s0_twin_merge_refuses_to_write_after_s4(self):
        self.assertIsNone(s0_normalize_numbers.library_moved())
        number = make_legacy_number('TOYOTA', '1610079445', printed_forms=['16100-79445'])
        ref, _ = references.add_library_number('TOYOTA', '16100-79445')
        Reference.all_objects.filter(pk=ref.pk).update(uid=number.pk)  # as S4's backfill leaves it
        self.assertIn('S4', s0_normalize_numbers.library_moved())


class AutomaticPathTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.root = make_user('s4r-auto', superuser=True)

    def test_a_family_whose_base_is_a_library_number_is_proposed_and_its_parent_promotes_that_line(self):
        number, _ = references.add_library_number('TOYOTA', '16100-79445')
        make_line('16100-79445-AAA', description='BOMBA AGUA TOY HILUX')
        make_line('16100-79445-BBB', description='BOMBA AGUA TOY HILUX')
        # S6: the worker proposes no family on its own any more; Agrupar SKU creates the parent and promotes the library's line.
        result = merge_catalog_parts(actor=self.root, target_sku='16100-79445', source_skus=['16100-79445-AAA', '16100-79445-BBB'],
                                     create_target=True)
        target = CatalogMerge.objects.get(pk=result['merge_id']).target
        self.assertEqual((target.pk, target.estado), (number.part_id, 'publicada'))
        references.add_library_number('HONDA', '44310-STX-305')
        references.add_library_number('ACURA', '44310-STX-305')
        make_line('44310-STX-305-AAA', description='BOMBA AGUA HONDA')
        make_line('44310-STX-305-BBB', description='BOMBA AGUA HONDA')
        with self.assertRaisesMessage(ValidationError, 'indica la marca'):  # two makes share it: a person names the make
            merge_catalog_parts(actor=self.root, target_sku='44310-STX-305', source_skus=['44310-STX-305-AAA', '44310-STX-305-BBB'],
                                create_target=True)

    def test_a_bulk_line_with_the_number_in_another_spelling_absorbs_its_line(self):
        number, _ = references.add_library_number('TOYOTA', '16100-39315')
        line = Part(sku='1610039315', description='BOMBA')
        bulk_create_lines([line])
        main = references.main_of(line)
        self.assertEqual(main.estado, 'disputa')  # the bulk path cannot absorb
        self.assertEqual(references.ensure_mains()['status'], 'ok')  # the worker's pass absorbs it instead of opening a case
        main.refresh_from_db()
        number.refresh_from_db()
        self.assertEqual((main.estado, number.part_id), ('declarado', line.pk))
        self.assertEqual(Part.objects.get(pk=number.part.pk).estado, 'publicada')

    def test_the_catalog_import_absorbs_a_library_line_written_another_way(self):
        client = APIClient()
        client.force_authenticate(self.root)
        number, _ = references.add_library_number('TOYOTA', '16100-39315')
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['SKU', 'NOMBRE', 'DESCRIPCION', 'CODIGO_ALTERNO', 'MARCA_ALTERNO', 'ACTIVO'])
        sheet.append(['1610039315', 'BOMBA', 'BOMBA AGUA', '', '', 'SI'])
        stream = io.BytesIO()
        workbook.save(stream)
        url = '/api/v1/management/catalog/import/'
        preview = client.post(url, {'file': SimpleUploadedFile('inventario.xlsx', stream.getvalue())}, format='multipart')
        self.assertEqual(preview.status_code, 200, preview.content)
        imported = client.post(url, {'file': SimpleUploadedFile('inventario.xlsx', stream.getvalue()), 'mode': 'import',
                                     'preview_token': preview.data['preview_token']}, format='multipart')
        self.assertEqual(imported.status_code, 200, imported.content)
        line = Part.objects.get(sku='1610039315')
        number.refresh_from_db()
        self.assertEqual((references.main_of(line).estado, number.part_id), ('declarado', line.pk))
        self.assertFalse(OEMReference.objects.exists())  # nothing written to the frozen OEM table
