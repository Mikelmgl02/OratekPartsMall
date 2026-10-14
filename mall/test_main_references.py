"""Inventory refactor S2b: every live line has one main Reference holding its codigo (mall.references.set_main and ensure_main), the
transition shims keep it so for the code that writes lines and References directly, readers leave the main out of code lists, and the
API shows the line's tipo and main."""
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from rest_framework.test import APITestCase

from . import references
from .catalog_grouping import merge_catalog_parts
from .catalog_identity import mark_exact_matches, prefer_oem
from .lines import bulk_create_lines, create_line
from .matching_engine import part_row
from .matching_models import MatchingCase
from .models import CatalogIdentityChange, Part, Reference
from .test_factories import Fresh, make_library_number, make_line, make_reference, make_user

INVARIANTS = {  # plan Appendix C, C2
    'I1': "SELECT count(*) FROM mall_part p WHERE p.estado <> 'unificada' AND (SELECT count(*) FROM mall_partcode r "
          "WHERE r.part_id = p.id AND r.is_main AND r.estado <> 'retirada') <> 1",
    'I2': "SELECT count(*) FROM mall_part p JOIN mall_partcode r ON r.part_id = p.id AND r.is_main AND r.estado <> 'retirada' "
          "WHERE p.estado <> 'unificada' AND p.sku <> r.code",
    'I3': "SELECT count(*) FROM mall_partcode r JOIN mall_part p ON p.id = r.part_id WHERE p.estado = 'unificada' AND r.estado <> 'retirada'",
    'I11': "SELECT count(*) FROM mall_part p JOIN mall_partcode r ON r.part_id = p.id AND r.is_main AND r.estado <> 'retirada' "
           "WHERE p.estado <> 'unificada' AND p.tipo <> CASE r.tipo WHEN 'oem' THEN 'OEM' WHEN 'aftermarket' THEN 'AFTERMARKET' ELSE 'UNKNOWN' END",
}


def broken_invariants():
    """{invariant: rows breaking it} for I1, I2, I3 and I11, empty when they all hold."""
    found = {}
    with connection.cursor() as cursor:
        for name, query in INVARIANTS.items():
            cursor.execute(query)
            count = cursor.fetchone()[0]
            if count:
                found[name] = count
    return found


def main(line):
    return references.main_of(line)


def alternos(line):
    return sorted(line.codes.filter(is_main=False).values_list('brand', 'code'))


class MainOfEveryLineTests(TestCase):
    def test_a_new_line_gets_its_main_with_its_tipo(self):
        plain, oem = make_line('ABC-1'), make_line('90915-YZZE1', is_OEM=True)
        self.assertEqual((main(plain).code, main(plain).brand, main(plain).tipo, plain.tipo), ('ABC-1', '', 'otro', 'UNKNOWN'))
        self.assertEqual((main(oem).code, main(oem).tipo, oem.tipo, oem.is_OEM), ('90915-YZZE1', 'oem', 'OEM', True))
        line = create_line('GWT-71A', main_brand='GMB', main_tipo='aftermarket')
        line.refresh_from_db()
        self.assertEqual((main(line).brand, main(line).tipo, line.tipo, line.is_OEM), ('GMB', 'aftermarket', 'AFTERMARKET', False))
        self.assertEqual(broken_invariants(), {})

    def test_create_line_refuses_a_held_key_when_strict_and_disputes_it_otherwise(self):
        holder = make_line('HOLDER')
        make_reference(holder, '17801-30070', brand='TOYOTA', tipo='oem')
        with self.assertRaises(references.ReferenceConflict):
            create_line('1780130070', strict=True)
        self.assertFalse(Part.objects.filter(sku='1780130070').exists())  # its transaction rolled back
        line = create_line('1780130070')
        self.assertEqual((main(line).estado, line.sku), ('disputa', '1780130070'))
        self.assertIn('HOLDER', main(line).dispute_note)

    def test_bulk_lines_get_their_mains_in_bulk(self):
        holder = make_line('HOLDER')
        make_reference(holder, 'BULK-2', brand='ACME')
        lines = bulk_create_lines([Part(sku=' bulk-1 '), Part(sku='BULK-2'), Part(sku='BULK-3', is_OEM=True)])
        mains = {line.sku: main(line) for line in lines}
        self.assertEqual({sku: (ref.code, ref.tipo, ref.estado) for sku, ref in mains.items()},
                         {'BULK-1': ('BULK-1', 'otro', 'declarado'), 'BULK-2': ('BULK-2', 'otro', 'disputa'), 'BULK-3': ('BULK-3', 'oem', 'declarado')})
        self.assertEqual(Part.objects.get(sku='BULK-3').tipo, 'OEM')
        self.assertEqual(broken_invariants(), {})

    def test_code_lists_leave_the_main_out(self):
        line = make_line('LIST-1', description='FILTRO')
        make_reference(line, 'LIST-ALT', brand='ACME', tipo='aftermarket')
        line = Part.objects.prefetch_related('codes').get(pk=line.pk)
        self.assertEqual([code['code'] for code in part_row(line)['codes']], ['LIST-ALT'])
        from .serializers import PartSerializer
        self.assertEqual([code['code'] for code in PartSerializer(line, context={'equivalents_by_part': {}}).data['codes']], ['LIST-ALT'])
        from .management import ManagedPartSerializer
        data = ManagedPartSerializer(line).data
        self.assertEqual([code['code'] for code in data['codes']], ['LIST-ALT'])
        self.assertEqual((data['tipo'], data['main']), ('UNKNOWN', {'id': main(line).pk, 'code': 'LIST-1', 'brand': '', 'tipo': 'otro',
                                                                    'estado': 'declarado'}))

    def test_a_line_without_a_main_yet_reads_as_before(self):
        line = make_line('NO-MAIN')
        make_reference(line, 'NO-MAIN-ALT')
        references.purge([main(line).pk])  # as before mall.refactor.s2b_main_refs
        line = Part.objects.prefetch_related('codes').get(pk=line.pk)
        from .management import ManagedPartSerializer
        data = ManagedPartSerializer(line).data
        self.assertEqual((data['main'], [code['code'] for code in data['codes']], data['sku']), (None, ['NO-MAIN-ALT'], 'NO-MAIN'))


class SetMainTests(TestCase):
    def setUp(self):
        self.actor = make_user('main-root', superuser=True)
        self.line = make_line('OLD-1', name='OLD-1')
        self.oem = make_reference(self.line, '12345-67890', brand='TOYOTA', tipo='oem', source='CATÁLOGO TOYOTA')

    def test_the_codigo_follows_the_main_and_the_old_main_stays(self):
        old = main(self.line)
        references.set_main(self.line, self.oem, actor=self.actor, reason='prueba')
        line = Part.objects.get(pk=self.line.pk)
        self.assertEqual((line.sku, line.name, line.tipo, line.is_OEM), ('12345-67890', '12345-67890', 'OEM', True))
        self.assertEqual(main(line).pk, self.oem.pk)
        self.assertEqual(alternos(line), [('', 'OLD-1')])
        old.refresh_from_db()
        self.assertEqual((old.is_main, old.estado), (False, 'declarado'))
        change = CatalogIdentityChange.objects.get(part=line)
        self.assertEqual((change.previous_sku, change.sku, change.actor, change.reference['action'], change.reference['previous_main']),
                         ('OLD-1', '12345-67890', self.actor, 'set_main', old.pk))
        self.assertEqual(broken_invariants(), {})

    def test_a_codigo_another_line_has_is_refused_and_can_open_a_merge_case(self):
        merged = make_line('TAKEN-1')
        target = make_line('TARGET-1')
        merged.merged_into = target
        merged.save(update_fields=['merged_into'])  # its main retires (I3), its codigo stays its own until S4
        ref, _ = references.add(self.line, 'TAKEN-1', '')
        with self.assertRaises(references.MainConflict) as refused:
            references.set_main(self.line, ref, open_case=True)
        self.assertEqual(refused.exception.line.pk, merged.pk)
        case = MatchingCase.objects.get(pk=refused.exception.case.pk)
        self.assertEqual((case.kind, case.method, case.target_sku), ('catalog', 'main_codigo', 'TAKEN-1'))
        self.assertEqual(Part.objects.get(pk=self.line.pk).sku, 'OLD-1')

    def test_only_a_live_reference_of_the_line_can_be_the_main(self):
        other = make_line('OTHER-1')
        with self.assertRaises(references.ReferenceConflict):
            references.set_main(other, self.oem)
        references.retire(self.oem)
        with self.assertRaises(references.ReferenceConflict):
            references.set_main(self.line, self.oem.pk)

    def test_a_main_is_neither_retired_nor_moved_nor_recoded_by_hand(self):
        with self.assertRaises(references.ReferenceConflict):
            references.retire(main(self.line))
        with self.assertRaises(references.ReferenceConflict):
            references.update(main(self.line), part=make_line('ELSEWHERE'))
        with self.assertRaises(references.ReferenceConflict):
            references.update(main(self.line), code='OLD-2')
        references.update(main(self.line), notes='REVISADO')  # anything else changes as on any Reference

    def test_prefer_oem_makes_the_verified_oem_the_main(self):
        prefer_oem(self.line, actor=self.actor)
        line = Part.objects.get(pk=self.line.pk)
        self.assertEqual((line.sku, main(line).pk, line.is_OEM), ('12345-67890', self.oem.pk, True))
        self.assertIn(('', 'OLD-1'), alternos(line))
        self.assertEqual(broken_invariants(), {})

    def test_retype_main_marks_through_the_main_and_takes_the_brand_only_when_free(self):
        line = make_line('MR-968365')
        references.retype_main(line, 'oem', brand='MITSUBISHI', actor=self.actor)
        line.refresh_from_db()
        self.assertEqual((main(line).brand, main(line).tipo, line.tipo, line.is_OEM), ('MITSUBISHI', 'oem', 'OEM', True))
        other = make_line('MB-1')
        make_reference(other, '28113-F2000', brand='HYUNDAI', tipo='oem')
        line = create_line('28113F2000')  # disputa: the key is held, so no brand can be taken either
        references.retype_main(line, 'oem', brand='HYUNDAI')
        self.assertEqual((main(line).brand, main(line).tipo), ('', 'oem'))


class ShimTests(TestCase):
    def test_a_rename_by_old_code_moves_the_main_and_keeps_the_old_code(self):
        line = make_line('REN-1')
        line.sku = 'REN-2'
        line.save()
        self.assertEqual((main(line).code, alternos(line)), ('REN-2', [('', 'REN-1')]))
        self.assertEqual(broken_invariants(), {})

    def test_is_oem_written_alone_retypes_the_main_and_prefers_a_branded_oem_of_the_codigo(self):
        line = make_line('17801-30070')
        branded = make_reference(line, '17801-30070', brand='TOYOTA', tipo='oem', source='CATÁLOGO')
        line.is_OEM = True
        line.save(update_fields=['is_OEM'])
        line.refresh_from_db()
        self.assertEqual((line.tipo, main(line).pk), ('OEM', branded.pk))
        plain = make_line('PLAIN-1')
        plain.is_OEM = True
        plain.save(update_fields=['is_OEM'])
        self.assertEqual((main(plain).tipo, main(plain).brand), ('oem', ''))
        plain.is_OEM = False
        plain.save(update_fields=['is_OEM'])
        self.assertEqual((main(plain).tipo, Part.objects.get(pk=plain.pk).tipo), ('otro', 'UNKNOWN'))
        self.assertEqual(broken_invariants(), {})

    def test_a_merge_by_old_code_retires_the_main_and_bringing_the_line_back_revives_it(self):
        line, target = make_line('MERGED-1'), make_line('TARGET-1')
        first = main(line)
        line.merged_into = target
        line.save(update_fields=['merged_into'])
        self.assertIsNone(main(line))
        self.assertEqual(Reference.all_objects.get(pk=first.pk).estado, 'retirada')
        line.merged_into, line.estado, line.active = None, 'publicada', True
        line.save(update_fields=['merged_into', 'estado', 'active'])
        self.assertEqual(main(line).pk, first.pk)  # the same row comes back, brand and tipo as they were
        self.assertEqual(broken_invariants(), {})

    def test_an_insert_of_the_mains_key_updates_the_main(self):
        line = make_line('90915-YZZE1')
        first = main(line)
        ref = Reference.objects.create(part=line, code='90915-YZZE1', ref_type='oem', reference_source='CATÁLOGO')
        self.assertEqual(ref.pk, first.pk)
        first.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual((first.is_main, first.tipo, first.reference_source, line.tipo, line.is_OEM), (True, 'oem', 'CATÁLOGO', 'OEM', True))
        alias = Reference.objects.create(part=line, code='90915YZZE1', ref_type='unknown')  # another spelling, an alias: no downgrade
        self.assertEqual((alias.pk, Reference.objects.get(pk=first.pk).tipo, Reference.objects.get(pk=first.pk).printed_forms),
                         (first.pk, 'oem', ['90915YZZE1']))
        other = make_line('OTHER-ADOPT')
        clash = Reference.objects.create(part=other, code='90915 YZZE1')  # another line's main holds the key
        self.assertEqual(clash.estado, 'disputa')
        self.assertEqual(broken_invariants(), {})


class EnsureMainsTests(TestCase):
    def test_nothing_happens_before_the_backfill_then_lines_without_a_main_get_one(self):
        lines = [make_line(f'ENS-{n}') for n in range(3)]
        references.purge(Reference.all_objects.values_list('pk', flat=True))
        self.assertEqual(references.ensure_mains(), {'status': 'backfill_pending', 'lines': 0})
        references.ensure_main(lines[0])
        Part.objects.filter(pk=lines[0].pk).update(sku='ENS-RENAMED')  # estado: a rename the shim never saw
        self.assertEqual(references.ensure_mains(), {'status': 'ok', 'lines': 3})
        self.assertEqual(main(lines[0]).code, 'ENS-RENAMED')
        self.assertEqual(broken_invariants(), {})


class MarkTests(Fresh, TestCase):
    def test_mark_exact_matches_marks_through_the_main(self):
        make_library_number('TOYOTA', '90915-YZZE1', citation='LISTA 2026')
        line = make_line('90915-YZZE1', description='FILTRO ACEITE TOY')
        self.assertEqual(len(mark_exact_matches()), 1)
        line.refresh_from_db()
        self.assertEqual((line.is_OEM, line.tipo, main(line).tipo, main(line).brand), (True, 'OEM', 'oem', 'TOYOTA'))
        self.assertEqual(CatalogIdentityChange.objects.get(part=line).reference['action'], 'exact_match')


class MergeTests(TestCase):
    def test_a_merge_absorbs_the_source_main_and_a_split_brings_it_back(self):
        actor = make_user('merge-root', superuser=True)
        target, source = make_line('58411-07000', description='TAMBOR'), make_line('58411-07000-K', description='TAMBOR')
        first = main(source)
        merge_catalog_parts(actor=actor, target_sku=target.sku, source_skus=[source.sku])
        first.refresh_from_db()
        alias = Reference.objects.get(part=target, code='58411-07000-K')
        self.assertEqual((first.estado, first.reemplazado_por_id, alias.is_main, main(target).code), ('retirada', alias.pk, False, '58411-07000'))
        self.assertEqual(broken_invariants(), {})
        from .catalog_split import split_catalog_part
        split_catalog_part(actor=actor, target_id=target.pk, restore=[{'id': str(source.pk), 'codes': [alias.pk], 'items': [], 'images': []}])
        self.assertEqual(main(source).code, '58411-07000-K')
        self.assertEqual(broken_invariants(), {})


class ManagementTests(APITestCase):
    def setUp(self):
        self.root = make_user('main-admin', superuser=True)
        self.client.force_authenticate(self.root)

    def test_crear_sku_creates_the_main_and_refuses_a_held_key(self):
        holder = make_line('HOLDER')
        make_reference(holder, 'ABC-123456', brand='ACME')
        refused = self.client.post('/api/v1/management/catalog/', {'sku': 'ABC123456'}, format='json')
        self.assertEqual(refused.status_code, 400)
        created = self.client.post('/api/v1/management/catalog/', {'sku': 'new-1', 'codes': [{'brand': 'ACME', 'code': 'N-1'}]}, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual((created.data['main']['code'], created.data['tipo'], [c['code'] for c in created.data['codes']]),
                         ('NEW-1', 'UNKNOWN', ['N-1']))

    def test_saving_codes_never_touches_the_main_and_a_rename_is_a_new_main(self):
        line = make_line('KEEP-1')
        first = main(line)
        url = f'/api/v1/management/catalog/{line.pk}/'
        self.assertEqual(self.client.patch(url, {'codes': []}, format='json').status_code, 200)
        self.assertEqual(main(line).pk, first.pk)
        self.assertEqual(self.client.patch(url, {'codes': [{'brand': '', 'code': 'KEEP-1'}]}, format='json').status_code, 200)
        self.assertEqual((main(line).pk, alternos(line)), (first.pk, []))  # the main's own key: it stays the main
        renamed = self.client.patch(url, {'sku': 'KEEP-2'}, format='json')
        self.assertEqual(renamed.status_code, 200, renamed.data)
        self.assertEqual((renamed.data['sku'], renamed.data['main']['code'], [c['code'] for c in renamed.data['codes']]), ('KEEP-2', 'KEEP-2', ['KEEP-1']))
        self.assertTrue(CatalogIdentityChange.objects.filter(part=line, previous_sku='KEEP-1', sku='KEEP-2', reference__action='set_main').exists())
        self.assertEqual(broken_invariants(), {})

    def test_the_alternos_grid_neither_lists_nor_edits_a_main(self):
        line = make_line('GRID-1')
        alt = make_reference(line, 'GRID-ALT')
        rows = self.client.get('/api/v1/management/alternates/').data['results']
        self.assertEqual([row['code'] for row in rows], ['GRID-ALT'])
        self.assertEqual(self.client.delete(f'/api/v1/management/alternates/{main(line).pk}/').status_code, 404)
        self.assertEqual(self.client.delete(f'/api/v1/management/alternates/{alt.pk}/').status_code, 204)


class AIRankingTests(TestCase):
    def test_ai_ranking_is_off_until_s6(self):
        from . import matching_worker
        stats = {}
        self.assertEqual((matching_worker.AI_LIMIT, matching_worker.analyze_ambiguous(matching_worker.AI_LIMIT, stats=stats)), (0, 0))
        self.assertEqual(stats['ai_status'], 'paused')
        with patch('mall.matching_worker.call_provider') as provider:
            matching_worker.analyze_ambiguous(0)
        provider.assert_not_called()
