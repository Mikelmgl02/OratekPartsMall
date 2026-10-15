"""The Search Document (inventory refactor S7): what each document holds, I8, the refresh hooks, the rebuild and its worker stage, the
CLI."""
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from . import lines, references, search_document
from .brand_models import Brand, BrandAlias
from .models import Part, Reference, SearchDocument, SearchDocumentRebuild, SupplierItem
from .technical_data import part_type_for
from .test_factories import make_line, make_supplier, make_user


def fresh_hooks():
    """Each test starts with no line waiting for the commit and no refresh callback registered (TestCase keeps one transaction open)."""
    pending = getattr(transaction.get_connection(), search_document.PENDING, None)
    if pending is not None:
        pending.clear()
    current = transaction.get_connection()
    current.run_on_commit = [entry for entry in current.run_on_commit if entry[1] is not search_document._flush]


class DocumentContentTests(TestCase):
    def setUp(self):
        fresh_hooks()

    def test_codes_hold_the_main_and_the_public_references_with_their_spellings_never_retired_or_disputed_ones(self):
        line = make_line('51750-J9000', description='CUBO DE RUEDA')
        alterno, _ = references.add(line, 'GWT-41A', 'GMB', tipo='aftermarket')
        references.add_spelling(alterno, 'GWT41-A')
        old, _ = references.add(line, 'OLD-1', '', tipo='otro')
        references.retire(old)
        doubtful, _ = references.add(line, 'DUDA-9', '', tipo='otro', estado='disputa', dispute_note='Otra línea lo tiene.')
        doc = search_document.documents([line])[str(line.pk)]
        self.assertEqual(doc['codes'], ' 51750-J9000 GWT-41A GWT41-A ')
        self.assertEqual(doc['compact'], ' 51750J9000 GWT41A ')
        self.assertNotIn('OLD-1', doc['codes'])
        self.assertNotIn('DUDA-9', doc['codes'])
        self.assertIn('GMB', doc['text'])  # a public Reference's brand is a word of the line, as the legacy search reads it

    def test_a_disputed_main_stays_the_lines_code(self):
        line = make_line('ABC-123', description='SOPORTE MOTOR')
        main = references.main_of(line)
        references.update(main, estado='disputa', dispute_note='Duplicado de otra línea: caso de unificación.')
        self.assertIn(' ABC-123 ', search_document.documents([line])[str(line.pk)]['codes'])

    def test_interno_codes_go_to_their_supplier_only(self):
        supplier = make_supplier('PROVEEDOR DOC')
        line = make_line('90915-YZZE1', description='FILTRO ACEITE')
        references.add(line, 'MM-0001', '', tipo='interno', supplier=supplier)
        doc = search_document.documents([line])[str(line.pk)]
        self.assertNotIn('MM-0001', doc['codes'])
        self.assertEqual(doc['internal'], {str(supplier.pk): ['MM-0001', 'MM0001']})

    def test_text_folds_accents_and_expands_brand_aliases_and_brand_is_the_mains(self):
        toyota = Brand.objects.create(nombre='TOYOTA', tipo='vehiculo')
        BrandAlias.objects.create(alias='TOY', brand=toyota)
        grupo_line = make_line('17801-0T030', description='Filtro de aire TOY Corolla bujía', part_type=part_type_for('FILTROS', 'FILTROS DE AIRE'))
        references.retype_main(grupo_line, 'oem', brand='TOYOTA')
        doc = search_document.documents([grupo_line])[str(grupo_line.pk)]
        self.assertTrue(doc['text'].startswith('FILTRO DE AIRE TOY COROLLA BUJIA'))
        self.assertTrue(doc['text'].endswith('TOYOTA'))
        self.assertEqual(doc['text'].count('TOYOTA'), 1)  # the main's brand is in it already: the alias adds nothing twice
        self.assertEqual((doc['brand'], doc['grupo'], doc['subgrupo']), ('TOYOTA', 'FILTROS', 'FILTROS DE AIRE'))
        plain = make_line('PLAIN-1', description='FILTRO TOY YARIS')
        self.assertEqual(search_document.documents([plain])[str(plain.pk)]['text'], 'FILTRO TOY YARIS TOYOTA')

    def test_no_supplier_item_data_is_in_a_document(self):
        supplier = make_supplier('PROVEEDOR CALIDAD')
        line = make_line('CAL-1', description='AMORTIGUADOR')
        SupplierItem.objects.create(part=line, supplier=supplier, supplier_invent_id='C-1', codigo='CAL-1', brand='', source='upload',
                                    matching_status='matched', calidad='oes')
        self.assertEqual(set(search_document.documents([line])[str(line.pk)]), set(search_document.FIELDS))  # calidad stays live

    def test_only_lines_on_sale_have_a_document(self):
        on_sale = make_line('ON-1', description='A')
        retired = make_line('OFF-1', description='B')
        lines.set_estado(retired, 'retirada')
        self.assertEqual(set(search_document.documents([on_sale, retired])), {str(on_sale.pk)})


@override_settings(SEARCH_DOCUMENT_SYNC=False)  # what a rebuild writes is what the writers left unwritten
class RebuildTests(TestCase):
    def setUp(self):
        fresh_hooks()

    def test_rebuild_writes_what_changed_deletes_stale_documents_and_checks_i8(self):
        first, second = make_line('R-1', description='UNO'), make_line('R-2', description='DOS')
        summary = search_document.rebuild(origin='cli')
        self.assertEqual((summary['lines'], summary['written'], summary['deleted']), (2, 2, 0))
        self.assertEqual(summary['check'], {'missing': 0, 'stale': 0})
        self.assertEqual(search_document.rebuild()['written'], 0)  # nothing changed: nothing written
        Part.objects.filter(pk=second.pk).update(description='DOS CAMBIADA')  # a write that skips every hook
        lines.set_estado(first, 'retirada')
        self.assertEqual(search_document.check(), {'missing': 0, 'stale': 1})
        summary = search_document.rebuild()
        self.assertEqual((summary['written'], summary['deleted']), (1, 1))
        self.assertEqual(SearchDocument.objects.get().text, 'DOS CAMBIADA')
        self.assertEqual(SearchDocumentRebuild.objects.filter(finished_at__isnull=False, error='').count(), 3)

    def test_a_rebuild_never_writes_back_what_its_first_pass_read_over_a_fresher_document(self):
        line = make_line('RACE-1', description='ANTES')
        search_document.rebuild()
        stale = search_document.documents()  # what a rebuild's first pass read...
        Part.objects.filter(pk=line.pk).update(description='DESPUES')
        search_document.write([line.pk])  # ...before a writer committed and refreshed its document
        with patch('mall.search_document.documents', side_effect=[stale]):
            summary = search_document.rebuild()
        self.assertEqual(summary['written'], 1)  # it differed, so it was computed again under the lock
        self.assertEqual(SearchDocument.objects.get(line=line).text, 'DESPUES')

    def test_the_stage_records_any_failure_and_lets_the_runners_stop_through(self):
        from .matching_runner import WorkerStopping
        make_line('FAIL-1', description='UNO')
        with patch('mall.search_document.rebuild', side_effect=ValueError('bug')):
            self.assertEqual(search_document.stage(), {'error': 'rebuild failed'})
        with patch('mall.search_document.rebuild', side_effect=WorkerStopping('stop')):
            with self.assertRaises(WorkerStopping):
                search_document.stage()

    def test_write_upserts_lines_on_sale_and_deletes_the_others(self):
        line = make_line('W-1', description='ANTES')
        self.assertEqual(search_document.write([line.pk]), (1, 0))
        Part.objects.filter(pk=line.pk).update(description='DESPUES')
        search_document.write([line.pk])
        self.assertEqual(SearchDocument.objects.get(line=line).text, 'DESPUES')
        self.assertTrue(search_document.in_step(line))
        lines.set_estado(line, 'retirada')
        self.assertEqual(search_document.write([line.pk]), (0, 1))
        self.assertFalse(SearchDocument.objects.exists())

    @override_settings(SEARCH_DOCUMENT_REBUILD_HOURS=6)
    def test_the_worker_stage_rebuilds_every_six_hours_and_retries_a_failure_sooner(self):
        make_line('S-1', description='UNO')
        self.assertTrue(search_document.rebuild_due())
        self.assertEqual(search_document.stage()['lines'], 1)
        self.assertIsNone(search_document.stage())  # just rebuilt
        later = timezone.now() + timedelta(hours=6, minutes=1)
        self.assertTrue(search_document.rebuild_due(later))
        SearchDocumentRebuild.objects.update(error='OperationalError: x')
        self.assertFalse(search_document.rebuild_due(timezone.now() + timedelta(minutes=5)))
        self.assertTrue(search_document.rebuild_due(timezone.now() + timedelta(minutes=16)))
        with override_settings(SEARCH_DOCUMENT_REBUILD_HOURS=0):
            self.assertFalse(search_document.rebuild_due(later))

    def test_the_matching_worker_runs_the_stage_under_its_lock(self):
        from .matching_worker import process_queue
        make_line('P-1', description='UNO')
        process_queue(force=True, use_ai=False)
        self.assertEqual(SearchDocumentRebuild.objects.get().origin, 'worker')
        self.assertTrue(SearchDocument.objects.filter(line__sku='P-1').exists())


@override_settings(SEARCH_DOCUMENT_SYNC=False)
class RefreshHookTests(TestCase):
    def setUp(self):
        fresh_hooks()

    def test_writes_inside_a_transaction_wait_for_the_commit_and_one_callback_writes_them_all(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            line = make_line('H-1', description='GANCHO')
            references.add(line, 'H-ALT', 'GMB', tipo='aftermarket')
            self.assertIn(str(line.pk), search_document.connection_pending())
            self.assertFalse(SearchDocument.objects.filter(line=line).exists())
        self.assertEqual([callback for callback in callbacks if callback is search_document._flush], [search_document._flush])
        callbacks[[callback is search_document._flush for callback in callbacks].index(True)]()
        self.assertIn(' H-ALT ', SearchDocument.objects.get(line=line).codes)
        self.assertEqual(search_document.connection_pending(), set())

    def test_the_writers_named_by_the_plan_refresh_their_lines(self):
        target, source = make_line('M-1', description='DESTINO'), make_line('M-2', description='ORIGEN')
        search_document.rebuild()
        actor = make_user('hook-admin', superuser=True)
        cases = [
            (lambda: lines.set_estado(target, 'retirada'), {target.pk}),
            (lambda: lines.set_estado(target, 'publicada'), {target.pk}),
            (lambda: references.add(target, 'M-ALT', '', tipo='otro'), {target.pk}),
            (lambda: references.set_main(target, Reference.objects.get(code='M-ALT')), {target.pk}),
            (lambda: lines.bulk_update_lines([Part.objects.get(pk=source.pk)], ['description']), {source.pk}),
            (lambda: lines.project_subgrupos([self.subgrupo_of(source)]), {source.pk}),
        ]
        for run, expected in cases:
            fresh_hooks()
            run()
            self.assertTrue({str(pk) for pk in expected} <= search_document.connection_pending(), run)
        fresh_hooks()
        from .line_merge import merge_lines
        merge_lines(Part.objects.get(pk=target.pk), [Part.objects.get(pk=source.pk)], actor=actor, reason='prueba')
        self.assertTrue({str(target.pk), str(source.pk)} <= search_document.connection_pending())
        search_document.write(search_document.connection_pending())
        self.assertFalse(SearchDocument.objects.filter(line=source).exists())  # merged: off the storefront
        self.assertIn(' M-2 ', SearchDocument.objects.get(line=target).codes)

    def subgrupo_of(self, line):
        part_type = part_type_for('FRENOS', 'DISCOS')
        Part.objects.filter(pk=line.pk).update(part_type=part_type)
        return part_type

    def test_a_brand_alias_refreshes_the_documents_whose_text_names_it(self):
        from . import brands
        line = make_line('A-1', description='FILTRO NIS FRONTIER')
        search_document.rebuild()
        nissan = Brand.objects.create(nombre='NISSAN', tipo='vehiculo')
        fresh_hooks()
        brands.add_alias(nissan, 'NIS')
        self.assertIn(str(line.pk), search_document.connection_pending())
        search_document.write(search_document.connection_pending())
        self.assertEqual(SearchDocument.objects.get(line=line).text, 'FILTRO NIS FRONTIER NISSAN')

    def test_an_alias_refreshes_whole_words_only_wherever_it_is_written(self):
        monroe = Brand.objects.create(nombre='MONROE', tipo='repuesto')
        word, inside, phrase = (make_line('W-MO', description='AMORTIGUADOR MO TRASERO'), make_line('W-MOTOR', description='SOPORTE MOTOR'),
                                make_line('W-NEW', description='CASCO NEW-ERA'))
        search_document.rebuild()
        fresh_hooks()
        alias = BrandAlias.objects.create(alias='MO', brand=monroe)  # as references.ensure_brand or the Django admin write it
        self.assertEqual(search_document.connection_pending(), {str(word.pk)})
        fresh_hooks()
        alias.delete()
        self.assertEqual(search_document.connection_pending(), {str(word.pk)})
        fresh_hooks()
        BrandAlias.objects.create(alias='NEW ERA CAP', brand=Brand.objects.create(nombre='NEW ERA', tipo='repuesto'))
        self.assertEqual(search_document.connection_pending(), set())  # every word of the alias, in order
        BrandAlias.objects.create(alias='NEW  ERA.', brand=monroe)
        self.assertEqual(search_document.connection_pending(), {str(phrase.pk)})
        self.assertNotIn(str(inside.pk), search_document.connection_pending())
        self.assertEqual(search_document.word_pattern('S/M'), '(^|[^A-Z0-9])S[^A-Z0-9]+M([^A-Z0-9]|$)')
        self.assertIsNone(search_document.word_pattern('.'))

    def test_the_biblioteca_cross_reference_routes_refresh_the_lines_that_reach_the_number(self):
        from rest_framework.test import APIClient
        pump = make_line('BOMBA-4', description='BOMBA DE AGUA')
        number = references.add(pump, '16100-29156', 'TOYOTA', tipo='oem', source='CATÁLOGO GMB 2016')[0]  # a number the line carries
        client = APIClient()
        client.force_authenticate(make_user('biblioteca-admin', superuser=True))
        fresh_hooks()
        added = client.post(f'/api/v1/management/oem-references/{number.uid}/cross-references/', {'brand': 'GMB', 'code': 'GWT-79A'}, format='json')
        self.assertEqual(added.status_code, 201, added.data)
        self.assertIn(str(pump.pk), search_document.connection_pending())
        fresh_hooks()
        retired = client.delete(f'/api/v1/management/oem-references/{number.uid}/cross-references/{added.data["id"]}/')
        self.assertEqual(retired.status_code, 204)
        self.assertIn(str(pump.pk), search_document.connection_pending())

    def test_outside_a_transaction_the_document_is_written_at_once(self):
        from types import SimpleNamespace
        line = make_line('O-1', description='AFUERA')
        outside = SimpleNamespace(in_atomic_block=False, run_on_commit=[])  # autocommit: no transaction to wait for
        with patch('mall.search_document._connection', return_value=outside):
            search_document.refresh([line])
        self.assertTrue(SearchDocument.objects.filter(line=line).exists())


