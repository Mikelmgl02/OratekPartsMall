"""References (inventory refactor S2a): PartCode renamed Reference on its own table, the generated clave against mall.references.clave(),
the brandless reservation, retire instead of delete (readers skip retired rows, the OEM mirror keeps the number), tipo and ref_type kept
in step, the writers going through mall.references, and the backfill CLI (python -m mall.refactor.s2a_references)."""
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.models import F
from django.test import TestCase
from rest_framework.test import APITestCase

from . import references
from .brand_models import Brand
from .catalog_grouping import merge_catalog_parts
from .catalog_import import build_plan, save_plan
from .models import Reference, ReferenceDeleteRefused, ReferenceSource
from .services import matching_part
from .test_factories import (make_client, make_code, make_line, make_reference, make_supplier, make_user, make_variant, run_migration_function)

ALGO = 'algo:oem-finder:v3:current:7'
CATALOG = 'GMB Water Pump Catalog 2016 · pág. 551'
# 0074's constraints, which a test of the backfill drops inside its own transaction to stage the rows 0073 leaves (tipo otro next to
# ref_type oem, both spellings of a duplicate live) before the backfill runs.
TIGHTEN = ('reference_one_live_line', 'reference_valid_tipo', 'reference_valid_estado', 'reference_tipo_matches_ref_type',
           'reference_interno_has_supplier', 'reference_code_set', 'reference_dispute_note', 'reference_not_own_replacement')


def drop_tighten_constraints():
    """Inside a test's transaction, so they come back when it rolls back (a no-op before 0074)."""
    with connection.cursor() as cursor:
        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')  # a line's main (S2b) leaves a deferred FK check pending, which ALTER TABLE refuses
        present = connection.introspection.get_constraints(cursor, Reference._meta.db_table)
        for name in TIGHTEN:
            if name in present:
                if present[name].get('check') or not present[name].get('index'):
                    cursor.execute(f'ALTER TABLE {Reference._meta.db_table} DROP CONSTRAINT {name}')
                else:
                    cursor.execute(f'DROP INDEX {name}')


def stored(ref):
    """(tipo, the JSON's ref_type, estado) of a Reference as stored: ref_type is derived from tipo since S10."""
    tipo, estado = Reference.all_objects.filter(pk=ref.pk).values_list('tipo', 'estado').get()
    return tipo, references.legacy_ref_type(tipo), estado


def storefront_codes(client, search):
    """The codes the storefront's catalog list shows for its first result, as a signed-in buyer sees them."""
    from django.contrib.auth import get_user_model
    buyer = get_user_model().objects.filter(username='ref-buyer').first()
    if buyer is None:
        buyer = make_user('ref-buyer')
        make_client('CLIENTE REFERENCIAS', buyer)
    client.force_login(buyer)
    response = client.get('/api/v1/catalog/', {'search': search})
    return response.json()['results'][0]['codes']


class ClaveTests(TestCase):
    CODES = ['17801-30070', '1780130070', '17801 30070', 'EC-01', 'EC01', 'ec-01', 'WPT-111', 'wpt 111', 'MD-703270.KOYO',
             '13780-61J00-NP-', '12345678', '1234567', 'ABCDEFGH12', 'ABCDEFG123', 'A1B2C3D4', ' 16100-39315 ', 'TM  001',
             'GWT-71A ', 'R177.27', '532 0418 10', 'Ñandú-123456', 'ßPIN-1234', 'X', '0-0-0-0-0-0-0-0', 'KX-100\t ', '  ab\t\tc  ',
             # the Unicode spaces PostgreSQL's \s matches under en_US.utf8, and the no-break spaces it does not
             'A\u2003B', 'X\u3000Y', 'K\u1680\u205fL', 'P\u2028Q', 'A\u00a0B', 'P\u2007Q', 'N\u202fO']

    def test_the_generated_clave_is_references_clave_on_every_code(self):
        line = make_line('CLAVE-LINE')
        rows = []
        for index, code in enumerate(self.CODES):
            ref = make_reference(line, f'TMP-{index}', brand=f'B{index}')
            Reference.all_objects.filter(pk=ref.pk).update(code=code)  # as written, past Reference.save's trim and upper case
            rows.append(ref.pk)
        found = dict(Reference.all_objects.filter(pk__in=rows).values_list('code', 'clave'))
        self.assertEqual(found, {code: references.clave(code) for code in self.CODES})

    def test_oem_like_codes_compact_and_short_ones_keep_their_printed_form(self):
        self.assertEqual(references.clave('17801-30070'), references.clave('1780130070'))
        self.assertEqual(references.clave('17801-30070'), '1780130070')
        self.assertNotEqual(references.clave('EC-01'), references.clave('EC01'))
        self.assertEqual((references.clave('ec-01'), references.clave(' TM  001 ')), ('EC-01', 'TM 001'))
        self.assertEqual(references.clave('ABCDEFGH12'), 'ABCDEFGH12')  # 2 digits: not OEM-like, but nothing to strip either
        self.assertEqual(references.clave('ABCD-EFG-12'), 'ABCD-EFG-12')
        self.assertEqual(references.clave('MD-703270.KOYO'), references.clave('MD-703270-KOYO'))

    def test_the_clave_follows_a_change_of_code(self):
        ref = make_reference(make_line('CLAVE-2'), '17801-30070', brand='TOYOTA')
        ref.code = 'ec-01'
        ref.save()
        self.assertEqual(Reference.objects.values_list('clave', flat=True).get(pk=ref.pk), 'EC-01')


class RenameTests(TestCase):
    def test_partcode_is_reference_on_the_same_table_and_relation(self):
        self.assertIs(Reference, Reference)
        self.assertEqual(Reference._meta.db_table, 'mall_partcode')
        line = make_line('REL-1')
        ref = make_reference(line, 'REL-ALT', brand='GMB', tipo='aftermarket')
        self.assertEqual(list(line.codes.filter(is_main=False)), [ref])
        self.assertEqual(type(ref.pk), int)
        self.assertIsNotNone(Reference.objects.filter(is_main=False).values_list('uid', flat=True).get(pk=ref.pk))  # the database fills it

    def test_new_columns_take_their_defaults_and_old_style_writes_keep_working(self):
        ref = make_code(part=make_line('OLD-1'), code='old-code', ref_type='company', brand=' gmb ')
        ref.refresh_from_db()
        self.assertEqual((ref.code, ref.brand, ref.tipo, ref.ref_type, ref.kind, ref.estado, ref.printed_forms, ref.detail, ref.version,
                          ref.is_main, ref.supplier), ('OLD-CODE', 'GMB', 'aftermarket', 'company', 'manufacturer', 'declarado', [], {}, 1,
                                                       False, None))
        self.assertIsNotNone(ref.uid)
        uids = {make_reference(make_line(f'UID-{n}'), f'UID-{n}').uid for n in range(3)}
        self.assertEqual(len(uids), 3)

    def test_the_json_keeps_ref_type_and_kind(self):
        line = make_line('JSON-1', name='JSON-1')
        make_reference(line, 'JSON-ALT', brand='GMB', tipo='aftermarket')
        make_reference(line, 'JSON-OEM-12345', brand='TOYOTA', tipo='oem')
        make_reference(line, 'JSON-INT', tipo='interno', supplier=make_supplier('JSON PROVEEDOR'))  # its supplier's alone (S3): not shown
        data = storefront_codes(self.client, 'JSON-1')
        self.assertEqual(sorted((c['code'], c['brand'], c['kind'], c['ref_type']) for c in data),
                         [('JSON-ALT', 'GMB', 'manufacturer', 'company'), ('JSON-OEM-12345', 'TOYOTA', 'oem', 'oem')])


class BrandlessReservationTests(TestCase):
    """A code with no brand reserves it for every brand: across lines an exclusive (clave, '') and (clave, X) clash; on one line they
    coexist; interno codes are exempt; disputa holds nothing."""

    def setUp(self):
        self.a, self.b, self.c = make_line('RES-A'), make_line('RES-B'), make_line('RES-C')

    def refused(self, *args, **kwargs):
        with self.assertRaises(references.ReferenceConflict) as caught, transaction.atomic():
            references.add(*args, **kwargs)
        return caught.exception.message

    def test_a_brandless_code_reserves_it_for_every_brand_on_other_lines(self):
        references.add(self.a, 'EC-01', '')
        self.assertIn('RES-A', self.refused(self.b, 'EC-01', 'TOYOTA'))
        self.assertIn('RES-A', self.refused(self.b, 'ec-01'))
        self.assertTrue(references.add(self.b, 'EC01', 'TOYOTA')[1])  # EC01 is another code: short codes keep their printed form
        references.add(self.a, '17801-30070', 'TOYOTA')
        self.assertIn('RES-A', self.refused(self.b, '1780130070', ''))  # the reverse: brandless against a branded one
        self.assertIn('RES-A', self.refused(self.b, '1780130070', 'TOYOTA'))
        self.assertTrue(references.add(self.a, '1780130070', '')[1])  # on its own line both coexist
        self.assertIn('RES-A', self.refused(self.b, '1780130070', 'LEXUS'))  # and now no brand may take it elsewhere
        references.add(self.a, '16100-39315', 'TOYOTA')
        self.assertTrue(references.add(self.b, '1610039315', 'LEXUS')[1])  # another brand may share a number nobody reserved

    def test_interno_codes_are_exempt_and_clash_only_with_their_own_supplier(self):
        motion, other = make_supplier('RES MOTION'), make_supplier('RES OTRO')
        references.add(self.a, '17801-30070', '')  # spellings of one key (unique_brand_code holds the printed one until 0074)
        self.assertTrue(references.add(self.b, '1780130070', '', tipo='interno', supplier=motion)[1])
        self.assertIn('RES-B', self.refused(self.c, '17801 30070', '', tipo='interno', supplier=motion))
        self.assertTrue(references.add(self.c, '1780-130070', '', tipo='interno', supplier=other)[1])
        with self.assertRaises(references.ReferenceConflict):
            references.build(self.a, 'X-1', tipo='interno')  # an interno code names its supplier

    def test_a_disputed_code_holds_no_key(self):
        references.add(self.a, 'EC-01', '')
        with self.assertRaises(references.ReferenceConflict):
            references.build(self.b, 'EC-01', 'TOYOTA', estado='disputa')  # a dispute says why
        ref, added = references.add(self.b, 'EC-01', 'TOYOTA', estado='disputa', dispute_note='Puede ser de otra medida')
        self.assertEqual((added, ref.estado), (True, 'disputa'))

    def test_the_same_key_on_its_line_is_found_and_a_new_spelling_kept(self):
        first, added = references.add(self.a, '17801-30070', 'TOYOTA', ref_type='oem', source=ALGO)
        self.assertEqual((added, first.estado, first.tipo), (True, 'inferido', 'oem'))
        again, added = references.add(self.a, '1780130070', 'TOYOTA')
        self.assertEqual((added, again.pk, again.code, again.printed_forms), (False, first.pk, '17801-30070', ['1780130070']))
        second, _ = references.add(self.a, 'EC-01', 'TOYOTA')
        with self.assertRaises(references.ReferenceConflict):
            references.update(second, code='17801 30070')  # the line already has that key under TOYOTA
        references.update(first, code='1780130070')  # a respelling keeps the old one in printed_forms
        first.refresh_from_db()
        self.assertEqual((first.code, first.printed_forms), ('1780130070', ['17801-30070']))

    def test_a_printed_duplicate_on_two_lines_is_refused_by_the_database(self):
        make_reference(self.a, 'DUP-1', brand='GMB')
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_reference(self.b, 'DUP-1', brand='GMB')


class RetireTests(APITestCase):
    def setUp(self):
        self.root = make_user('ref-root', superuser=True)
        self.line = make_line('RET-LINE', name='RET-LINE')
        self.other = make_line('RET-OTHER')

    def test_nothing_deletes_a_reference(self):
        ref = make_reference(self.line, 'RET-1')
        for delete in (ref.delete, Reference.objects.filter(pk=ref.pk).delete, Reference.all_objects.filter(pk=ref.pk).delete,
                       self.line.codes.all().delete):
            with self.assertRaises(ReferenceDeleteRefused):
                delete()
        self.assertTrue(Reference.objects.filter(pk=ref.pk).exists())

    def test_a_retired_reference_leaves_every_reader_and_comes_back_when_added_again(self):
        ref = make_reference(self.line, 'RET-2', brand='GMB')
        supplier = make_supplier('RET PROVEEDOR')
        self.assertEqual(matching_part('RET-2', 'GMB'), self.line)
        self.assertEqual(references.retire(ref, actor=self.root, reason='prueba'), 1)
        self.assertEqual(references.retire(ref), 0)  # already retired
        ref.refresh_from_db()
        self.assertEqual((ref.estado, ref.detail['retired']['by'], ref.detail['retired']['reason']), ('retirada', 'ref-root', 'prueba'))
        self.assertFalse(self.line.codes.filter(is_main=False).exists())
        self.assertFalse(Reference.objects.filter(pk=ref.pk).exists())
        self.assertTrue(Reference.all_objects.filter(pk=ref.pk).exists())
        self.assertIsNone(matching_part('RET-2', 'GMB'))
        # A retired code matches nothing (S6: the item becomes a new product of its own, its short brandless code interno to it).
        variant = make_variant(supplier, 'RET-V', 'RET-2', 1, actor=self.root)
        self.assertEqual((variant.matching_status, variant.part_id != self.line.pk, variant.reference.tipo), ('pending', True, 'interno'))
        self.assertEqual(storefront_codes(self.client, 'RET-LINE'), [])
        self.client.logout()
        self.client.force_authenticate(self.root)
        self.assertEqual(self.client.get('/api/v1/management/catalog/', {'search': 'RET-2'}).json()['count'], 0)
        back, added = references.add(self.line, 'RET-2', 'GMB', ref_type='company')
        self.assertEqual((added, back.pk, back.estado, back.tipo), (True, ref.pk, 'declarado', 'aftermarket'))
        self.assertEqual(self.client.get('/api/v1/management/catalog/', {'search': 'RET-2'}).json()['count'], 1)

    def test_a_code_retired_on_one_line_can_go_to_another(self):
        ref = make_reference(self.line, 'RET-3', brand='GMB')
        references.retire(ref)
        moved, added = references.add(self.other, 'RET-3', 'GMB')
        self.assertTrue(added)
        self.assertEqual(list(self.other.codes.filter(is_main=False).values_list('code', flat=True)), ['RET-3'])
        # From 0074 a new row is added and the retired one stays on its line; before it (unique_brand_code) the retired row moves.
        self.assertEqual(Reference.all_objects.filter(code='RET-3', estado='retirada').count(), 1 if moved.pk != ref.pk else 0)

    def test_saving_a_lines_codes_retires_the_ones_not_sent(self):
        keep, drop = make_reference(self.line, 'RET-KEEP'), make_reference(self.line, 'RET-DROP', brand='GMB', tipo='aftermarket')
        self.client.force_authenticate(self.root)
        response = self.client.patch(f'/api/v1/management/catalog/{self.line.pk}/', {'codes': [{'brand': '', 'code': 'RET-KEEP'},
                                                                                              {'brand': 'TOYOTA', 'code': '17801-30070'}]},
                                     format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(sorted(code['code'] for code in response.json()['codes']), ['17801-30070', 'RET-KEEP'])
        self.assertEqual(Reference.all_objects.get(pk=drop.pk).estado, 'retirada')
        self.assertEqual(Reference.objects.get(pk=keep.pk).estado, 'declarado')
        response = self.client.patch(f'/api/v1/management/catalog/{self.line.pk}/', {'codes': [{'brand': '', 'code': 'RET-KEEP'},
                                                                                              {'brand': 'TOYOTA', 'code': '1780130070'}]},
                                     format='json')
        self.assertEqual(response.status_code, 200, response.content)
        respelled = Reference.objects.get(part=self.line, brand='TOYOTA')
        self.assertEqual((respelled.code, respelled.printed_forms), ('1780130070', ['17801-30070']))  # the same Reference, respelled
        response = self.client.patch(f'/api/v1/management/catalog/{self.line.pk}/', {'codes': [{'brand': '', 'code': 'EC-01'},
                                                                                              {'brand': '', 'code': 'EC01'},
                                                                                              {'brand': 'X', 'code': '1780130070'},
                                                                                              {'brand': 'X', 'code': '17801-30070'}]},
                                     format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('otra grafía', str(response.json()))

    def test_the_alternates_api_refuses_another_lines_key_and_retires_on_delete(self):
        make_reference(self.other, '17801-30070', brand='TOYOTA', tipo='oem')
        self.client.force_authenticate(self.root)
        url = '/api/v1/management/alternates/'
        refused = self.client.post(url, {'part': str(self.line.pk), 'code': '1780130070', 'brand': ''}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('ya pertenece a otro SKU', str(refused.json()))
        created = self.client.post(url, {'part': str(self.line.pk), 'code': 'ALT-9', 'brand': 'gmb', 'ref_type': 'company'}, format='json')
        self.assertEqual(created.status_code, 201, created.content)
        ref = Reference.objects.get(pk=created.json()['id'])
        self.assertEqual((ref.tipo, ref.created_by), ('aftermarket', self.root))
        patched = self.client.patch(f'{url}{ref.pk}/', {'ref_type': 'oem'}, format='json')
        self.assertEqual((patched.status_code, Reference.objects.get(pk=ref.pk).tipo), (200, 'oem'))
        self.assertEqual(self.client.delete(f'{url}{ref.pk}/').status_code, 204)
        self.assertEqual(Reference.all_objects.get(pk=ref.pk).estado, 'retirada')
        self.assertEqual(self.client.get(f'{url}{ref.pk}/').status_code, 404)  # a retired alterno is not listed or edited

    def test_an_oem_alterno_is_the_library_row_itself_and_nothing_mirrors_it(self):
        ref, _ = references.add(self.line, '17801-30070', 'TOYOTA', ref_type='oem', source=ALGO)
        self.assertTrue(Reference.objects.filter(references.library_q(), pk=ref.pk).exists())
        references.retire(ref)  # only the OEM finder backs it: no library number, so it retires as before
        self.assertFalse(Reference.objects.filter(references.library_q(), pk=ref.pk).exists())
        back, _ = references.add(self.line, '17801-30070', 'TOYOTA', ref_type='oem', source=ALGO)
        self.assertEqual((back.pk, back.estado), (ref.pk, 'inferido'))

    def test_merging_keeps_the_sources_codigo_as_a_reference_and_a_split_moves_it_back(self):
        from .catalog_split import split_catalog_part
        target, source = make_line('SPL-1'), make_line('SPL-1-GMB')
        first = references.main_of(source)
        merge_catalog_parts(actor=self.root, target_sku='SPL-1', source_skus=['SPL-1-GMB'])
        alias = Reference.objects.get(part=target, code='SPL-1-GMB')
        self.assertEqual((alias.pk, alias.brand, alias.tipo, alias.is_main), (first.pk, '', 'otro', False))  # the source's main moved (S2b)
        split_catalog_part(actor=self.root, target_id=target.pk, restore=[{'id': str(source.pk), 'codes': [], 'items': [], 'images': []}])
        back = Reference.objects.get(pk=alias.pk)
        self.assertEqual((back.part_id, back.is_main, back.estado), (source.pk, True, 'declarado'))  # moved back, never retired
        merge_catalog_parts(actor=self.root, target_sku='SPL-1', source_skus=['SPL-1-GMB'])
        self.assertEqual(Reference.objects.get(part=target, code='SPL-1-GMB').pk, alias.pk)  # merged again: the same row


class ReferenceAdminTests(TestCase):
    def setUp(self):
        self.root = make_user('ref-admin', superuser=True)
        self.client.force_login(self.root)
        self.line, self.other = make_line('ADM-1'), make_line('ADM-2')

    def test_the_admin_lists_retired_rows_writes_through_mall_references_and_deletes_nothing(self):
        live, gone = make_reference(self.line, 'ADM-LIVE'), make_reference(self.line, 'ADM-GONE')
        references.retire(gone)
        page = self.client.get('/admin/mall/reference/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'ADM-GONE')
        self.assertEqual(self.client.post(f'/admin/mall/reference/{live.pk}/delete/', {'post': 'yes'}).status_code, 403)
        form = {'part': str(self.line.pk), 'code': 'ADM-NEW', 'brand': 'gmb', 'tipo': 'aftermarket', 'estado': 'declarado', 'supplier': '',
                'reference_source': 'LISTA DE MOSTRADOR', 'dispute_note': '', 'notes': '',
                'sources-TOTAL_FORMS': '0', 'sources-INITIAL_FORMS': '0', 'sources-MIN_NUM_FORMS': '0', 'sources-MAX_NUM_FORMS': '1000'}
        self.assertEqual(self.client.post('/admin/mall/reference/add/', form).status_code, 302)
        added = Reference.objects.get(code='ADM-NEW')
        self.assertEqual((added.brand, added.tipo, added.ref_type, added.created_by, added.sources.get().kind),
                         ('GMB', 'aftermarket', 'company', self.root, 'admin'))
        clash = self.client.post('/admin/mall/reference/add/', {**form, 'part': str(self.other.pk), 'code': 'ADM-NEW'})
        self.assertEqual(clash.status_code, 200)  # the form shows mall.references.validate's refusal
        self.assertContains(clash, 'ya pertenece a otro SKU')
        retired = self.client.post(f'/admin/mall/reference/{live.pk}/change/', {**form, 'code': 'ADM-LIVE', 'brand': '', 'tipo': 'otro',
                                                                                  'estado': 'retirada', 'reference_source': ''})
        self.assertEqual(retired.status_code, 302)
        self.assertEqual(Reference.all_objects.get(pk=live.pk).estado, 'retirada')

    def test_the_admin_refuses_in_the_form_what_mall_references_refuses(self):
        form = {'part': str(self.line.pk), 'code': 'ADM-DISP', 'brand': '', 'tipo': 'otro', 'estado': 'disputa', 'supplier': '',
                'reference_source': '', 'dispute_note': '', 'notes': '',
                'sources-TOTAL_FORMS': '0', 'sources-INITIAL_FORMS': '0', 'sources-MIN_NUM_FORMS': '0', 'sources-MAX_NUM_FORMS': '1000'}
        refused = self.client.post('/admin/mall/reference/add/', form)
        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, 'motivo de la disputa')
        self.assertContains(self.client.post('/admin/mall/reference/add/', {**form, 'estado': 'declarado', 'tipo': 'interno'}),
                            'Un código interno lleva su proveedor')
        self.assertEqual(self.client.post('/admin/mall/reference/add/', {**form, 'dispute_note': 'OTRA MEDIDA'}).status_code, 302)
        added = Reference.objects.get(code='ADM-DISP')
        self.assertEqual((added.estado, added.dispute_note), ('disputa', 'OTRA MEDIDA'))
        gone = make_reference(self.line, 'ADM-BACK')
        references.retire(gone)
        make_reference(self.other, 'ADM-BACK', brand='GMB')  # another line took the key under a brand meanwhile
        back = self.client.post(f'/admin/mall/reference/{gone.pk}/change/', {**form, 'code': 'ADM-BACK', 'estado': 'declarado'})
        self.assertEqual(back.status_code, 200)  # a brandless code would reserve it again for every brand
        self.assertContains(back, 'ya pertenece a otro SKU')
        self.assertEqual(Reference.all_objects.get(pk=gone.pk).estado, 'retirada')


class WriterTests(TestCase):
    def setUp(self):
        self.root = make_user('writer-root', superuser=True)

    def test_the_catalog_import_adds_codes_through_build_and_reads_keys(self):
        held = make_line('IMP-HELD')
        make_reference(held, '17801-30070', brand='TOYOTA', tipo='oem')
        groups = {'IMP-NEW': {'sku': 'IMP-NEW', 'row': 2, 'fields': {'name': 'NUEVO'},
                              'codes': {('', 'IMP-ALT-1'): 2, ('GMB', 'GWT-71A'): 2, ('GMB', '16100-39315'): 2}},
                  'IMP-CLASH': {'sku': 'IMP-CLASH', 'row': 3, 'fields': {}, 'codes': {('', '1780130070'): 3}}}
        result, _ = build_plan(groups, [], 3)
        self.assertEqual([error['row'] for error in result['errors']], [3])  # 1780130070 is TOYOTA 17801-30070's key on IMP-HELD
        del groups['IMP-CLASH']
        result, _ = build_plan(groups, [], 2)
        self.assertTrue(result['valid'], result)
        save_plan(groups)
        codes = Reference.objects.filter(part__sku='IMP-NEW').order_by('code')
        self.assertEqual([(c.code, c.brand, c.tipo, c.ref_type, c.estado, c.is_main) for c in codes],
                         [('16100-39315', 'GMB', 'otro', 'unknown', 'declarado', False), ('GWT-71A', 'GMB', 'otro', 'unknown', 'declarado', False),
                          ('IMP-ALT-1', '', 'otro', 'unknown', 'declarado', False), ('IMP-NEW', '', 'otro', 'unknown', 'declarado', True)])
        again = {'IMP-NEW': {'sku': 'IMP-NEW', 'row': 2, 'fields': {}, 'codes': {('GMB', '1610039315'): 2, ('GMB', 'GWT 71A'): 2}}}
        build_plan(again, [], 1)
        self.assertEqual(again['IMP-NEW']['added'], [('GMB', 'GWT 71A')])  # 1610039315 is a code the line holds; GWT 71A (short) is not

    def test_a_supplier_catalog_code_a_line_holds_is_a_conflict(self):
        line, other = make_line('SCI-1'), make_line('SCI-2')
        make_reference(other, '17801-30070', brand='TOYOTA', tipo='oem')
        with self.assertRaises(references.ReferenceConflict):
            references.add(line.pk, '1780130070', '', ref_type='company', source=CATALOG)

    def test_sources_follow_the_a3_prefixes(self):
        cases = {ALGO: 'buscador', 'https://parts.example/17801': 'ia', 'Declarado por Motion Motors · producto nuevo 1a2b3c4d': 'proveedor',
                 'CONFIRMADO POR MIGUEL · ' + CATALOG: 'admin', 'Marcado como OEM por miguel': 'admin', CATALOG: 'catalogo',
                 'ATE K24-F Verschleißteile / Wear Parts 2017 · pág. 1612': 'catalogo', 'LISTA DE MOSTRADOR': 'admin'}
        self.assertEqual({text: references.source_of(text)[0] for text in cases}, cases)
        self.assertIsNone(references.source_of('  '))
        self.assertEqual(references.source_of(ALGO)[2], {'run': 7, 'rule': 'current', 'rules_version': 'v3'})
        line = make_line('SRC-1')
        ref, _ = references.add(line, 'SRC-ALT', 'GMB', ref_type='company', source=CATALOG, actor=self.root)
        source = ref.sources.get()
        self.assertEqual((source.kind, source.citation, source.created_by), ('catalogo', CATALOG, self.root))
        references.update(ref, reference_source='CONFIRMADO POR MIGUEL · ' + CATALOG)
        self.assertEqual(sorted(ref.sources.values_list('kind', flat=True)), ['admin', 'catalogo'])
        self.assertEqual(references.cite(ref, CATALOG), (source, False))  # an upsert


def brands_missing():
    """Appendix C, I12: the non-blank brands of References that are neither a Brand nombre nor an alias."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM mall_partcode r WHERE r.brand <> '' "
                       "AND NOT EXISTS (SELECT 1 FROM mall_brand b WHERE b.nombre = r.brand) "
                       "AND NOT EXISTS (SELECT 1 FROM mall_brandalias a WHERE a.alias = r.brand)")
        return cursor.fetchone()[0]


class WriterRuleTests(APITestCase):
    """The rules every write keeps from S2a: update() checks what the row ends up as, every brand a Reference carries is a brand (I12),
    bulk inserts take the clave locks, one Reference per key per line in the catalog import, a split keeps the brandless reservation,
    and an exact code matches through the spellings a Reference keeps."""

    def setUp(self):
        self.root = make_user('rules-root', superuser=True)
        self.a, self.b = make_line('RULE-A'), make_line('RULE-B')

    def test_bringing_a_retired_reference_back_validates_it_and_clears_its_replacement(self):
        ref, _ = references.add(self.a, 'EC-01', '')
        other, _ = references.add(self.a, 'EC-02', '')
        references.retire(ref, replaced_by=other)
        held, _ = references.add(self.b, 'EC-01', 'TOYOTA')  # free while the brandless one is retired
        with self.assertRaisesMessage(references.ReferenceConflict, 'ya pertenece a otro SKU (RULE-B)'):
            references.update(Reference.all_objects.get(pk=ref.pk), estado='declarado')
        references.retire(held)
        back = references.update(Reference.all_objects.get(pk=ref.pk), actor=self.root, estado='declarado')
        back.refresh_from_db()
        self.assertEqual((back.estado, back.reemplazado_por_id, back.detail['returned']['by']), ('declarado', None, 'rules-root'))

    def test_update_refuses_a_dispute_without_its_note_and_an_interno_code_without_its_supplier(self):
        ref, _ = references.add(self.a, 'UPD-2', '')
        with self.assertRaisesMessage(references.ReferenceConflict, 'motivo de la disputa'):
            references.update(ref, estado='disputa')
        with self.assertRaisesMessage(references.ReferenceConflict, 'Un código interno lleva su proveedor'):
            references.update(ref, tipo='interno')
        references.update(ref, estado='disputa', dispute_note='OTRA MEDIDA')
        self.assertEqual(stored(ref), ('otro', 'unknown', 'disputa'))

    def test_every_brand_a_reference_carries_is_a_brand_or_an_alias(self):
        ref, _ = references.add(self.a, 'BR-1', 'marca  nueva')
        brand = Brand.objects.get(nombre='MARCA NUEVA')
        self.assertEqual((brand.tipo, list(brand.aliases.values_list('alias', flat=True))), ('repuesto', ['MARCA  NUEVA']))
        references.update(ref, brand='Chery Oem', tipo='oem')
        self.assertEqual(Brand.objects.get(nombre='CHERY OEM').tipo, 'vehiculo')
        self.client.force_authenticate(self.root)
        response = self.client.patch(f'/api/v1/management/catalog/{self.a.pk}/', {'codes': [{'brand': 'OTRA MARCA', 'code': 'BR-2'}]},
                                     format='json')
        self.assertEqual(response.status_code, 200, response.content)
        save_plan({'RULE-NEW': {'sku': 'RULE-NEW', 'row': 2, 'existing': None, 'fields': {}, 'codes': {('IMPORTADA', 'BR-3'): 2}}}, self.root)
        self.assertEqual(brands_missing(), 0)
        self.assertTrue(Brand.objects.filter(nombre__in=['OTRA MARCA', 'IMPORTADA']).count() == 2)

    def test_a_bulk_insert_checks_every_key_again(self):
        references.add(self.a, '17801-30070', '')
        with self.assertRaisesMessage(references.ReferenceConflict, 'ya pertenece a otro SKU (RULE-A)'):
            references.bulk_create([references.build(self.b, '1780130070', 'TOYOTA')])  # the brandless reservation
        with self.assertRaisesMessage(references.ReferenceConflict, 'se repite con otra grafía'):
            references.bulk_create([references.build(self.b, 'BULK-12345', ''), references.build(self.b, 'BULK12345', '')])
        self.assertFalse(Reference.objects.filter(part=self.b, is_main=False).exists())  # only its main (S2b)

    def test_the_catalog_import_keeps_one_reference_per_key_and_brings_retired_ones_back(self):
        gone = make_reference(self.a, 'IMP-R-1', brand='GMB')
        references.retire(gone)
        elsewhere = make_reference(self.b, 'IMP-R-2', brand='GMB')
        references.retire(elsewhere)
        groups = {'RULE-A': {'sku': 'RULE-A', 'row': 2, 'fields': {}, 'codes': {
            ('GMB', 'IMP-R-1'): 2, ('GMB', 'IMP-R-2'): 2, ('', '17801-30070'): 2, ('', '1780130070'): 2}}}
        result, _ = build_plan(groups, [], 1, lock=True)
        self.assertTrue(result['valid'], result)
        save_plan(groups, self.root)
        self.assertEqual(Reference.objects.get(pk=gone.pk).estado, 'declarado')  # retired on this line: it comes back
        moved = Reference.objects.get(part=self.a, code='IMP-R-2')  # retired on another line (unique_brand_code until 0074)
        self.assertEqual(moved.estado, 'declarado')
        new = Reference.objects.get(part=self.a, code='17801-30070')
        self.assertEqual((new.printed_forms, new.created_by), (['1780130070'], self.root))  # the second spelling joins it
        again = {'RULE-A': {'sku': 'RULE-A', 'row': 2, 'fields': {}, 'codes': {('', '17801 30070'): 2}}}
        build_plan(again, [], 1, lock=True)
        save_plan(again)
        new.refresh_from_db()
        self.assertEqual(new.printed_forms, ['1780130070', '17801 30070'])
        self.assertEqual(Reference.objects.filter(part=self.a, clave='1780130070').count(), 1)

    def test_a_split_refuses_a_code_that_would_leave_the_brandless_reservation_broken(self):
        from rest_framework.exceptions import ValidationError
        from .catalog_split import split_catalog_part
        target, source = make_line('SPL-2'), make_line('SPL-2-GMB')
        merge_catalog_parts(actor=self.root, target_sku='SPL-2', source_skus=['SPL-2-GMB'])
        references.add(target, '17801-99999', '')
        branded, _ = references.add(target, '1780199999', 'TOYOTA')  # on one line both may coexist
        with self.assertRaisesMessage(ValidationError, 'quedaría en SPL-2-GMB'):
            split_catalog_part(actor=self.root, target_id=target.pk,
                               restore=[{'id': str(source.pk), 'codes': [branded.pk], 'items': [], 'images': []}])
        self.assertEqual(Reference.objects.get(pk=branded.pk).part_id, target.pk)

    def test_an_exact_code_matches_through_the_spellings_a_reference_keeps(self):
        from .supplier_import import stock_plan
        ref, _ = references.add(self.a, '13780-61J00-NP', '')
        references.add_spelling(ref, '13780-61J00-NP-')
        self.assertEqual(matching_part('13780-61J00-NP-', 'SUZUKI'), self.a)
        rows = [{'row': 2, 'data': {'supplier_invent_id': 'PF-1', 'codigo': '13780-61J00-NP-', 'brand': '', 'quantity': 1}}]
        plan, _ = stock_plan(make_supplier('RULE PROVEEDOR'), rows)
        self.assertEqual([(row['matching_status'], row['part_id']) for row in plan['preview']], [('matched', str(self.a.pk))])

    def test_a_catalog_replace_keeps_a_number_a_catalog_line_holds(self):
        from .supplier_catalog_replace import held
        ref, _ = references.add(self.a, '17801-30070', 'TOYOTA', ref_type='oem', source=ALGO)
        self.assertTrue(held(ref, 'GMB Water Pump Catalog 2016'))  # S4: the number is one of a catalog line's codes
        alone, _ = references.add_library_number('TOYOTA', '17801-30071', source_kind='aftermarket_catalog', citation='GMB Water Pump Catalog 2016')
        self.assertFalse(held(alone, 'GMB Water Pump Catalog 2016'))  # on its own line in referencia, the catalog's alone

    def test_add_moves_a_retired_row_only_for_unique_brand_code(self):
        gone = make_reference(self.b, 'MOVE-1', brand='GMB')
        references.retire(gone)
        with patch.object(Reference, 'save', side_effect=IntegrityError('otra restricción')):
            with self.assertRaisesMessage(references.ReferenceConflict, 'ya está registrado'):
                references.add(self.a, 'MOVE-1', 'GMB')
        self.assertEqual(Reference.all_objects.get(pk=gone.pk).part_id, self.b.pk)  # never moved for another refusal

    def test_the_code_before_s2a_can_delete_a_code_with_sources_and_replacements(self):
        author = make_user('rules-author')
        ref, _ = references.add(self.a, 'DEL-1', 'GMB', ref_type='company', source=CATALOG, actor=author)
        old = make_reference(self.a, 'DEL-0', brand='GMB')
        references.retire(old, replaced_by=ref)
        with connection.cursor() as cursor:  # as the code before S2a deletes: it knows none of these relations (0073 DELETE_ACTIONS)
            cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
            cursor.execute('DELETE FROM mall_partcode WHERE id = %s', [ref.pk])
        self.assertFalse(ReferenceSource.objects.filter(reference_id=ref.pk).exists())
        self.assertIsNone(Reference.all_objects.get(pk=old.pk).reemplazado_por_id)
        kept, _ = references.add(self.a, 'DEL-2', 'GMB', ref_type='company', source=CATALOG, actor=author)
        with connection.cursor() as cursor:
            cursor.execute('DELETE FROM mall_user WHERE id = %s', [author.pk])
        self.assertEqual((Reference.objects.get(pk=kept.pk).created_by_id, kept.sources.get().created_by_id), (None, None))


class TightenTests(TestCase):
    """0074_reference_constraints: tipo repaired from ref_type, then one live Reference per key and the CHECKs."""
    MIGRATION = '0074_reference_constraints'

    def refused(self, write):
        with self.assertRaises(IntegrityError), transaction.atomic():
            write()

    def test_one_live_reference_per_key_and_a_retired_or_disputed_one_holds_nothing(self):
        a, b = make_line('TIGHT-A'), make_line('TIGHT-B')
        held = make_reference(a, '17801-30070', brand='TOYOTA', tipo='oem')
        self.refused(lambda: make_reference(b, '1780130070', brand='TOYOTA', tipo='oem'))  # the same clave, printed another way
        self.refused(lambda: make_reference(a, '17801 30070', brand='TOYOTA'))  # on its own line too: one Reference per key
        make_reference(b, '1780130070', brand='TOYOTA', estado='disputa', dispute_note='OTRO MODELO')
        references.retire(held)
        make_reference(b, '17801-30070', brand='TOYOTA', tipo='oem')  # the retired one keeps its printed form as history
        self.assertEqual(Reference.all_objects.filter(code='17801-30070', brand='TOYOTA').count(), 2)
        supplier = make_supplier('TIGHT PROVEEDOR')
        make_reference(a, 'EC-01', tipo='interno', supplier=supplier)
        self.refused(lambda: make_reference(b, 'EC-01', tipo='interno', supplier=supplier))  # NULLS NOT DISTINCT keeps brand/supplier keys

    def test_the_checks_refuse_what_the_transition_does_not_allow(self):
        line = make_line('TIGHT-C')
        ref = make_reference(line, 'TIGHT-1', brand='GMB', tipo='aftermarket')
        rows = Reference.all_objects.filter(pk=ref.pk)
        self.refused(lambda: rows.update(tipo='interno'))  # an interno code names its supplier (S10: ref_type follows tipo by itself)
        self.refused(lambda: rows.update(supplier=make_supplier('TIGHT OTRO')))
        self.refused(lambda: rows.update(estado='disputa'))  # a dispute says why
        self.refused(lambda: rows.update(estado='perdida'))
        self.refused(lambda: rows.update(code=''))
        self.refused(lambda: rows.update(reemplazado_por=F('id')))
        rows.update(estado='disputa', dispute_note='OTRA MEDIDA')
        self.assertEqual(stored(ref), ('aftermarket', 'company', 'disputa'))

    def test_the_reverse_refuses_while_one_printed_code_is_held_twice(self):
        run_migration_function(self.MIGRATION, 'refuse_printed_duplicates')
        first = make_reference(make_line('TIGHT-D1'), 'TIGHT-DUP', brand='GMB')
        references.retire(first)
        make_reference(make_line('TIGHT-D2'), 'TIGHT-DUP', brand='GMB')
        with self.assertRaisesMessage(RuntimeError, '1 marcas y códigos están dos veces (GMB TIGHT-DUP)'):
            run_migration_function(self.MIGRATION, 'refuse_printed_duplicates')
