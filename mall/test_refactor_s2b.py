"""python -m mall.refactor.s2b_main_refs: every live line's main Reference by the plan's rules, the merge groups for the hook (part B),
the shared OEM number cases, the learned codes as sources, the line's tipo, the manifest and the rollback."""
import io
import json
import os
import tempfile
from unittest.mock import patch

from django.test import TestCase

from . import references
from .brand_models import Brand
from .catalog_suffix_models import CatalogCodeSuffix
from .matching_models import MatchingCase, SupplierCodeMapping
from .models import Part, Reference, ReferenceSource
from .oem_reference_models import PartOEMLink
from .refactor import s2b_main_refs
from .test_factories import Fresh, make_library_number, make_line, make_reference, make_supplier, make_user, make_variant
from .test_main_references import broken_invariants


class BackfillTests(Fresh, TestCase):
    """The lines as S2b's code finds them before the backfill: no main, tipo UNKNOWN (0075's default)."""

    def setUp(self):
        super().setUp()
        self.root = make_user('s2b-root', superuser=True)
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        patcher = patch.dict(os.environ, {'REFACTOR_OUTPUT': self.output.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for nombre in ('TOYOTA', 'MITSUBISHI', 'GMB'):
            Brand.objects.create(nombre=nombre)
        CatalogCodeSuffix.objects.create(token='ZQX', cls='TAG', tag_kind='brand', attribution='brand: GMB', confidence='high',
                                         status='owner_confirmed', owner_confirmed=True)
        CatalogCodeSuffix.objects.create(token='ZQY', cls='TAG', tag_kind='brand', attribution='brand: ZQY (unverified)',
                                         status='owner_confirmed', owner_confirmed=True)
        make_library_number('MITSUBISHI', 'MR-968365', source_kind='catalog_approval', citation='CATÁLOGO')
        number = make_library_number('TOYOTA', '43512-60150', source_kind='catalog_approval', citation='CATÁLOGO')
        self.supplier = make_supplier('S2B PROVEEDOR')
        self.own_oem = make_line('OEM-12345678', is_OEM=True)
        self.branded = make_reference(self.own_oem, 'OEM-12345678', brand='TOYOTA', tipo='oem', source='CATÁLOGO')
        self.maker = make_line('MR-968365')
        self.no_maker = make_line('90530126', is_OEM=True)
        self.tag = make_line('16100-39315-ZQX')
        self.mapped = make_reference(self.tag, 'ALT-MAP-1', brand='GMB', tipo='aftermarket')
        self.unknown_tag = make_line('16100-39316-ZQY')
        self.spelled = make_line('17801-30070')
        self.keeper = make_line('13780-75F00')
        self.duplicate = make_line('13780-75F00_')
        make_variant(self.supplier, 'INV-1', '13780-75F00', 5, actor=self.root, line=self.keeper)
        self.holder = make_line('OTHER-LINE')
        self.held = make_reference(self.holder, 'ALIAS-HELD-1')
        self.own = make_line('ALIAS-HELD-1')
        self.carrier, self.linked = make_line('43512-60150'), make_line('DISCO-X')
        PartOEMLink.objects.create(part=self.linked, reference=number, source='catalog')
        self.retired = make_line('RET-1', active=False)
        self.stocked, self.respelled = make_line('28113-F2000'), make_line('28113F2000')
        make_variant(self.supplier, 'INV-2', '28113-F2000', 3, actor=self.root, line=self.stocked)
        SupplierCodeMapping.objects.create(supplier=self.supplier, code='ALTMAP1', brand='GMB', part=self.tag)
        SupplierCodeMapping.objects.create(supplier=self.supplier, code='LOCAL9', brand='SIN MARCA', part=self.keeper)
        # The state the backfill finds: no main (the S2b code makes them, the old code did not), tipo at 0075's default.
        references.purge(Reference.all_objects.filter(is_main=True).values_list('pk', flat=True))
        self.spelling = make_reference(self.spelled, '1780130070')  # an alias of the codigo's key: reused and respelled
        self.loser = make_reference(self.respelled, '28113 F2000')  # reused by 28113F2000, which loses the key to 28113-F2000 (stock)
        for row in Reference.all_objects.filter(estado='disputa'):
            Reference.all_objects.filter(pk=row.pk).update(estado='declarado', dispute_note='')
        Part.objects.update(tipo='UNKNOWN')

    def run_cli(self, *args):
        out = io.StringIO()
        code = s2b_main_refs.main([*args], stdout=out)
        return code, out.getvalue()

    def manifest(self):
        with open(os.path.join(self.output.name, 'manifest.jsonl'), encoding='utf-8') as handle:
            return [json.loads(line) for line in handle]

    def main(self, line):
        return references.main_of(line)

    def test_the_dry_run_plans_every_rule_and_writes_nothing(self):
        result = s2b_main_refs.plan(hook=None)
        rules = {result['lines'][pk]['sku']: (main['action'], main['brand'], main['tipo'], main['rule'], main['estado'])
                 for pk, main in result['mains'].items()}
        self.assertEqual(rules['OEM-12345678'], ('reuse', 'TOYOTA', 'oem', 1, 'declarado'))
        self.assertEqual(rules['MR-968365'], ('create', 'MITSUBISHI', 'oem', 2, 'declarado'))
        self.assertEqual(rules['90530126'], ('create', '', 'oem', 3, 'declarado'))
        self.assertEqual(rules['16100-39315-ZQX'], ('create', 'GMB', 'aftermarket', 4, 'declarado'))
        self.assertEqual(rules['16100-39316-ZQY'], ('create', '', 'aftermarket', 5, 'declarado'))
        self.assertEqual(rules['17801-30070'], ('reuse', '', 'otro', 6, 'declarado'))
        self.assertEqual(rules['13780-75F00_'][4], 'disputa')
        self.assertEqual((rules['RET-1'][0], rules['ALIAS-HELD-1'][4]), ('create', 'declarado'))
        groups = {group['clave']: group for group in result['groups']}
        self.assertEqual(set(groups), {'1378075F00', 'ALIAS-HELD-1', '28113F2000'})
        self.assertEqual((groups['1378075F00']['keeper_sku'], groups['ALIAS-HELD-1']['keeper_sku']), ('13780-75F00', 'ALIAS-HELD-1'))
        self.assertEqual(list(result['disputed']), [self.held.pk])
        self.assertEqual([(group['keeper_sku'], group['numbers']) for group in result['shared']], [('43512-60150', ['4351260150'])])
        self.assertEqual({(m['code'], m['reference'] is not None) for m in result['mappings']}, {('ALTMAP1', True), ('LOCAL9', False)})
        self.assertEqual(result['final']['is_oem_cambia'], 2)  # MR-968365 and 43512-60150: their code is one library number
        self.assertFalse(Reference.all_objects.filter(is_main=True).exists())
        code, out = self.run_cli('--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('Simulación · S2b', out)
        self.assertIn('parte B decide', out)  # mall.line_merge.s2b_compact_duplicates: these lines have no description, so cases
        self.assertIn('Casos compact_duplicate (en disputa hasta que se decidan): 3', out)
        self.assertFalse(os.path.exists(os.path.join(self.output.name, 'manifest.jsonl')))

    def test_apply_gives_every_live_line_its_main_and_the_invariants_hold(self):
        self.assertIn('I1', broken_invariants())
        code, out = self.run_cli('--apply', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        self.assertEqual(broken_invariants(), {})
        self.assertEqual(self.main(self.own_oem).pk, self.branded.pk)
        maker = Part.objects.get(pk=self.maker.pk)
        self.assertEqual((self.main(maker).brand, maker.tipo, maker.is_OEM), ('MITSUBISHI', 'OEM', True))
        respelled = self.main(self.spelled)
        self.assertEqual((respelled.pk, respelled.code, respelled.printed_forms), (self.spelling.pk, '17801-30070', ['1780130070']))
        self.assertEqual((self.main(self.tag).brand, Part.objects.get(pk=self.tag.pk).tipo), ('GMB', 'AFTERMARKET'))
        duplicate = self.main(self.duplicate)
        self.assertEqual(duplicate.estado, 'disputa')
        self.assertIn('13780-75F00', duplicate.dispute_note)
        self.held.refresh_from_db()
        self.assertEqual((self.held.estado, self.main(self.own).estado), ('disputa', 'declarado'))
        self.assertEqual(self.main(self.retired).code, 'RET-1')  # retired lines keep their codes reserved (OD14)
        loser = self.main(self.respelled)
        self.assertEqual((loser.pk, loser.code, loser.estado, self.main(self.stocked).estado), (self.loser.pk, '28113F2000', 'disputa', 'declarado'))
        case = MatchingCase.objects.get(method='shared_oem_number')
        self.assertEqual((case.kind, case.target_sku, [row['sku'] for row in case.sources]), ('catalog', '43512-60150', ['DISCO-X']))
        source = ReferenceSource.objects.get(reference=self.mapped, kind='proveedor')
        self.assertTrue(source.citation.startswith('Código aprendido · S2B PROVEEDOR · '))
        interno = Reference.objects.get(tipo='interno')
        self.assertEqual((interno.part_id, interno.code, interno.brand, interno.supplier_id), (self.keeper.pk, 'LOCAL9', '', self.supplier.pk))
        lines = self.manifest()
        self.assertEqual(lines[0]['action'], 'run')
        self.assertTrue(any(line.get('role') == 'main' and line['action'] == 'create' for line in lines))
        # again: nothing new
        result = s2b_main_refs.plan(hook=None)
        self.assertEqual((result['final']['crear'], result['final']['reusar'], result['groups'], result['final']['internos']), (0, 0, [], 0))

    def test_the_merge_hook_gets_the_groups_and_its_absorbed_lines_open_no_case(self):
        calls = []

        def hook(groups, *, apply, actor, run, log):
            calls.append((apply, [[row['reference'] for row in group['rows']] for group in groups]))
            if apply:
                log.append({**run, 'hook': True, 'model': 'mall.Part', 'id': str(self.linked.pk), 'action': 'update', 'before': {}, 'after': {}})
            return {'absorbed': [str(self.linked.pk)]}

        result = s2b_main_refs.run(stdout=io.StringIO(), hook=hook)
        self.assertEqual(result['absorbed'], [str(self.linked.pk)])
        self.assertEqual([group for group in result['shared']], [])  # its lines go to the merge, not to a case
        self.assertTrue(calls[0][1] and not calls[0][0])
        self.assertIn(None, [ref for group in calls[0][1] for ref in group])  # the dry run's new mains have no id yet
        s2b_main_refs.run(apply=True, username=self.root.username, stdout=io.StringIO(), hook=hook)
        self.assertTrue(calls[-1][0])
        self.assertNotIn(None, [ref for group in calls[-1][1] for ref in group])
        self.assertFalse(MatchingCase.objects.filter(method='shared_oem_number').exists())
        self.assertTrue(any(line.get('hook') for line in self.manifest()))

    def test_rollback_puts_everything_back(self):
        before_refs = sorted(Reference.all_objects.values_list('pk', 'code', 'brand', 'tipo', 'estado', 'is_main', 'printed_forms'))
        before_lines = sorted(Part.objects.values_list('pk', 'tipo', 'is_OEM'))
        self.assertEqual(self.run_cli('--apply', '--user', self.root.username)[0], 0)
        code, out = self.run_cli('--rollback', '--user', self.root.username)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(Reference.all_objects.values_list('pk', 'code', 'brand', 'tipo', 'estado', 'is_main', 'printed_forms')), before_refs)
        self.assertEqual(sorted(Part.objects.values_list('pk', 'tipo', 'is_OEM')), before_lines)
        self.assertFalse(MatchingCase.objects.filter(method='shared_oem_number').exists())
        self.assertFalse(ReferenceSource.objects.filter(kind='proveedor').exists())
        self.assertIn('Rollback · S2b', out)

    def test_the_dry_run_simulates_before_0073_and_0075(self):
        result = s2b_main_refs.plan({'references': False, 'tipo': False}, hook=None)
        self.assertEqual(result['final']['lineas'], len([pk for pk, line in result['lines'].items() if s2b_main_refs.live(line)]))
        self.assertGreater(result['final']['crear'], 0)

    def test_apply_needs_its_user_and_the_expanded_schema(self):
        code, out = self.run_cli('--apply')
        self.assertEqual(code, 1)
        self.assertIn('--user', out)
        with patch.object(s2b_main_refs, 'schema', return_value={'references': True, 'tipo': False}):
            code, out = self.run_cli('--apply', '--user', self.root.username)
        self.assertEqual(code, 1)
        self.assertIn('0073–0075', out)


class KeeperTests(TestCase):
    """The clean spelling keeps a compact-duplicate group even with less stock (the owner, 2026-10-14)."""

    def test_the_clean_spelling_wins_over_stock_and_a_published_line_over_both(self):
        line = lambda pk, sku, estado='publicada': {'pk': pk, 'sku': sku, 'estado': estado}
        stats = {1: (2, 0), 2: (9, 3), 3: (0, 0)}
        clean, messy, retired = line(1, '16546-18000'), line(2, '16546-18000_'), line(3, '16546-18000', 'retirada')
        order = sorted([messy, retired, clean], key=lambda row: s2b_main_refs.keeper_key(row, stats, {}))
        self.assertEqual([row['pk'] for row in order], [1, 2, 3])
        for sku in ('16546-S0100-', 'MB-120389_', '13780-79201_', '11115-54073.'):
            self.assertTrue(s2b_main_refs.messy_spelling(sku), sku)
        for sku in ('16546-18000', '11115-54073.AS', '04111-54220/280-AS'):
            self.assertFalse(s2b_main_refs.messy_spelling(sku), sku)

