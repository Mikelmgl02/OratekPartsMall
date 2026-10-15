"""mall.line_merge (inventory refactor S2b): merge_lines moves every Reference (the source's main among them, absorbed when the
target holds its key), variant, image, favourite, link and dependent in the lock order; split_line gives each back, its former main as
its main; lines kept apart stay apart on canonical lines; the S2b backfill's compact duplicates merge on their own when they pass the
noun and spec checks (OD4) and open compact_duplicate cases otherwise; dismissing such a case keeps the lines apart."""
import uuid
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from . import line_merge, references, variants
from .catalog_assistant_models import CatalogAssistantJob, CatalogAssistantSuggestion
from .catalog_split import split_catalog_part, split_plan
from .catalog_split_models import CatalogSeparation
from .matching_models import MatchingDecision
from .models import CatalogMerge, Part, PartImage, Reference, ReferenceSource, SupplierItem, WishlistItem
from .oem_finder_models import OEMAutoCandidate, OEMReviewCase
from .oem_reference_models import PartOEMLink
from .supplier_import_models import SupplierInventoryImportJob
from .technical_models import PartType, PartSpecification, TechnicalField, TechnicalTemplate
from .test_factories import make_library_number, make_line, make_reference, make_supplier, make_user, make_variant, unhook_variants
from .test_main_references import broken_invariants


def main(line):
    return references.main_of(line)


def image(line, name):
    return PartImage.objects.create(part=line, image=f'catalog/test/{name}.webp', thumbnail=f'catalog/test/{name}-t.webp', width=1,
                                    height=1, size_bytes=1)


def restore_all(target):
    """Separar every line merged into target with the rows the plan suggests for it (its former main comes back on its own)."""
    plan = split_plan(target)
    entries = []
    for child in plan['children']:
        pk = str(child.pk)
        entries.append({'id': pk, 'codes': [code.pk for code in plan['codes'] if code.suggested == pk and not code.alias_of],
                        'items': [str(item.pk) for item in plan['items'] if item.suggested == pk],
                        'images': [str(row.pk) for row in plan['images'] if row.suggested == pk]})
    return entries


class MergeLinesTests(TestCase):
    def setUp(self):
        self.root = make_user('lm-root', superuser=True)
        self.supplier = make_supplier('LM PROVEEDOR', self.root)
        self.target = make_line('58411-07000', description='TAMBOR FRENO KIA PICANTO')
        self.source = make_line('58411-07000-K', description='TAMBOR FRENO KIA PICANTO')

    def test_everything_the_source_holds_moves_and_is_recorded(self):
        alt = make_reference(self.source, 'KX-100', brand='KOREA', tipo='aftermarket', source='CATÁLOGO KOREA')
        item = make_variant(self.supplier, 'LM-1', '58411-07000-K', 4, actor=self.root, line=self.source)
        kept, moved = image(self.target, 'same'), image(self.source, 'own')
        image(self.source, 'same')  # the same file the target already shows: it stays where it is
        other = make_user('lm-other')
        WishlistItem.objects.create(user=self.root, part=self.target)
        duplicate = WishlistItem.objects.create(user=self.root, part=self.source)
        favourite = WishlistItem.objects.create(user=other, part=self.source)
        number = make_library_number('KIA', '58411-07000', source_kind='aftermarket_catalog', citation='CATÁLOGO KOREA')
        PartOEMLink.objects.create(part=self.target, reference=number, source='catalog', citations=['A · KX-1'])
        PartOEMLink.objects.create(part=self.source, reference=number, source='catalog', citations=['B · KX-100'])
        own = make_library_number('KIA', '58411-07001', citation='LISTA')
        link = PartOEMLink.objects.create(part=self.source, reference=own, source='catalog', citations=['C · KX-100'])
        review = OEMReviewCase.objects.create(part=self.source, fingerprint='f', tier='REVIEW')
        auto = OEMAutoCandidate.objects.create(part=self.source, tier='AUTO_FLAG_CURRENT', apply_tier='AUTO_FLAG_CURRENT', sku=self.source.sku,
                                               candidate='58411-07000', fingerprint='f', rules_version='v')
        job = CatalogAssistantJob.objects.create(owner=self.root, scope='all', model='m')
        suggestion = CatalogAssistantSuggestion.objects.create(job=job, source=self.source, position=0)
        former = main(self.source)

        merge = line_merge.merge_lines(self.target, [self.source], actor=self.root, reason='prueba')

        self.source.refresh_from_db()
        self.assertEqual((self.source.estado, self.source.merged_into_id, self.source.active), ('unificada', self.target.pk, False))
        alt.refresh_from_db()
        former.refresh_from_db()
        self.assertEqual((alt.part_id, former.part_id, former.is_main, main(self.target).code), (self.target.pk, self.target.pk, False, '58411-07000'))
        self.assertEqual(SupplierItem.objects.get(pk=item.pk).part_id, self.target.pk)
        self.assertEqual(sorted(PartImage.objects.filter(part=self.target).values_list('pk', flat=True)), sorted([kept.pk, moved.pk]))
        self.assertEqual(sorted(WishlistItem.objects.values_list('user_id', 'part_id')), sorted([(self.root.pk, self.target.pk), (other.pk, self.target.pk)]))
        self.assertFalse(WishlistItem.objects.filter(pk=duplicate.pk).exists())
        self.assertEqual(PartOEMLink.objects.get(part=self.target, reference=number).citations, ['A · KX-1', 'B · KX-100'])
        self.assertEqual(PartOEMLink.objects.get(pk=link.pk).part_id, self.target.pk)
        review.refresh_from_db()
        auto.refresh_from_db()
        suggestion.refresh_from_db()
        self.assertEqual((review.status, review.decision['reason'], auto.status, auto.reason, suggestion.status),
                         ('resolved', 'línea unificada', 'excluded', 'merged', 'dismissed'))
        self.assertEqual(broken_invariants(), {})
        entry = merge.sources[0]
        self.assertEqual((entry['main'], sorted(entry['references']), entry['codes']), (former.pk, sorted([alt.pk, former.pk]),
                                                                                         [{'brand': 'KOREA', 'code': 'KX-100'}]))
        self.assertEqual((entry['items'], entry['images'], entry['favorites']['moved'], entry['favorites']['dropped'], entry['links']),
                         ([str(item.pk)], [str(moved.pk)], [favourite.pk], [self.root.pk], [link.pk]))
        self.assertEqual((merge.summary['reason'], merge.summary['automatic'], merge.summary['estado']), ('prueba', False, {'before': 'publicada', 'after': 'publicada'}))

        # Separar gives all of it back: the former main as the source's main, its rows, favourites, links and dependents.
        restore = [{'id': str(self.source.pk), 'codes': [alt.pk], 'items': [str(item.pk)], 'images': [str(moved.pk)]}]
        result = line_merge.split_line(self.target.pk, restore, actor=self.root)
        self.source.refresh_from_db()
        self.assertEqual((self.source.estado, self.source.merged_into_id, main(self.source).pk), ('publicada', None, former.pk))
        self.assertEqual(Reference.objects.get(pk=alt.pk).part_id, self.source.pk)
        self.assertEqual(SupplierItem.objects.get(pk=item.pk).part_id, self.source.pk)
        self.assertEqual(sorted(WishlistItem.objects.filter(part=self.source).values_list('user_id', flat=True)), sorted([self.root.pk, other.pk]))
        self.assertEqual(PartOEMLink.objects.get(pk=link.pk).part_id, self.source.pk)
        review.refresh_from_db()
        auto.refresh_from_db()
        suggestion.refresh_from_db()
        self.assertEqual((review.status, auto.status, suggestion.status), ('review', 'pending', 'pending'))
        self.assertEqual((result['summary']['mains_returned'], result['summary']['favorites_returned']), (1, 2))
        self.assertEqual(broken_invariants(), {})
        self.assertTrue(line_merge.apart(self.target.pk, self.source.pk))

    def test_a_source_reference_whose_key_the_target_holds_is_absorbed_and_separar_brings_it_back(self):
        held = make_reference(self.target, '90915-YZZE1', brand='TOYOTA', tipo='oem')
        note = 'El código 90915YZZE1 ya es de otro SKU (58411-07000): caso de unificación.'  # a key two lines hold waits for a merge
        spelled = make_reference(self.source, '90915YZZE1', brand='TOYOTA', tipo='oem', source='CATÁLOGO TOYOTA', estado='disputa',
                                 dispute_note=note)
        references.cite(spelled, 'CATÁLOGO TOYOTA', kind='catalogo')
        merge = line_merge.merge_lines(self.target, [self.source], actor=self.root)
        spelled = Reference.all_objects.get(pk=spelled.pk)
        held.refresh_from_db()
        self.assertEqual((spelled.part_id, spelled.estado, spelled.reemplazado_por_id), (self.source.pk, 'retirada', held.pk))
        self.assertEqual(held.printed_forms, ['90915YZZE1'])
        self.assertTrue(ReferenceSource.objects.filter(reference=held, citation='CATÁLOGO TOYOTA').exists())  # its sources come along
        self.assertEqual(merge.summary['references_absorbed'], 1)
        self.assertEqual(broken_invariants(), {})
        line_merge.split_line(self.target.pk, [{'id': str(self.source.pk)}], actor=self.root)
        spelled = Reference.all_objects.get(pk=spelled.pk)
        held.refresh_from_db()
        self.assertEqual((spelled.estado, spelled.dispute_note, spelled.reemplazado_por_id, held.printed_forms), ('disputa', note, None, []))
        self.assertFalse(ReferenceSource.objects.filter(reference=held, citation='CATÁLOGO TOYOTA').exists())
        self.assertEqual(broken_invariants(), {})

    def test_the_merged_lines_estado_follows_the_plans_matrix(self):
        Line = lambda estado: Part(sku=str(uuid.uuid4()), estado=estado)
        publicada, retirada, prep, rechazada, referencia = (Line(e) for e in ('publicada', 'retirada', 'preparacion', 'rechazada', 'referencia'))
        self.assertEqual(line_merge.merged_estado([retirada, publicada], []), 'publicada')
        self.assertEqual(line_merge.merged_estado([referencia, prep], [prep]), 'preparacion')
        self.assertEqual(line_merge.merged_estado([retirada, retirada], [retirada]), 'retirada')  # a retired line's variants are catalog stock
        self.assertEqual(line_merge.merged_estado([rechazada, retirada, referencia], []), 'retirada')
        self.assertEqual(line_merge.merged_estado([referencia, rechazada], []), 'rechazada')
        self.assertEqual(line_merge.merged_estado([referencia, referencia], []), 'referencia')
        self.assertEqual(line_merge.merged_estado([prep, prep], []), 'preparacion')
        self.target.active = False
        self.target.save(update_fields=['active'])
        merge = line_merge.merge_lines(self.target, [self.source], actor=self.root)
        self.target.refresh_from_db()
        self.assertEqual((self.target.estado, self.target.active, merge.summary['estado']), ('publicada', True, {'before': 'retirada', 'after': 'publicada'}))

    def test_the_target_keeps_its_scalars_and_fills_its_blanks_when_the_sources_agree(self):
        blank = make_line('BLANK-1')
        first = make_line('BLANK-1-A', description='BOMBA AGUA', category='MOTOR')
        second = make_line('BLANK-1-B', description='BOMBA AGUA', category='REFRIGERACION')
        merge = line_merge.merge_lines(blank, [first, second], actor=self.root)
        blank.refresh_from_db()
        self.assertEqual((blank.description, blank.category), ('BOMBA AGUA', ''))  # they disagree on the category: it stays blank
        self.assertEqual(blank.field_sources['description'], {'source': 'merge', 'from': 'BLANK-1-A', 'merge': str(merge.pk)})
        self.assertEqual(merge.summary['filled'], {'description': ''})

    def test_what_would_break_the_catalog_is_refused(self):
        other = make_line('OTRO-SKU')
        make_reference(other, 'KEY-1', brand='ACME')
        make_reference(self.source, 'KEY-1')  # a brandless code another line holds under a brand: the reservation (I5)
        with self.assertRaisesMessage(ValidationError, 'KEY-1 pertenece a otro SKU'):
            line_merge.merge_lines(self.target, [self.source], actor=self.root)
        source = make_line('SRC-2')
        Reference.objects.create(part=source, code='OTRO-SKU')  # another line's codigo: it waits in disputa (the S2b shim)
        with self.assertRaisesMessage(ValidationError, 'OTRO-SKU identifica otro SKU'):
            line_merge.merge_lines(self.target, [source], actor=self.root)
        sheet = make_line('SHEET-1')
        part_type = PartType.objects.create(category='FRENOS', name='TAMBORES LM')
        template = TechnicalTemplate.objects.create(part_type=part_type)
        field = TechnicalField.objects.create(template=template, key='diametro', label='Diámetro', unit='mm', kind='number')
        PartSpecification.objects.create(part=sheet, field=field, number_value=200)
        # S6: specs move only onto a target with no subgrupo or the same one; another subgrupo's sheet waits (guard subgrupo).
        Part.objects.filter(pk=self.target.pk).update(part_type=PartType.objects.create(category='FRENOS', name='DISCOS LM'))
        self.target.refresh_from_db()
        with self.assertRaisesMessage(line_merge.MergeGuard, 'fichas técnicas'):
            line_merge.merge_lines(self.target, [sheet], actor=self.root)
        make_variant(self.supplier, 'LM-9', 'SRC-3', 1, actor=self.root, line=make_line('SRC-3'))
        SupplierInventoryImportJob.objects.create(supplier=self.supplier, owner=self.root, filename='x.xlsx', status='importing',
                                                  expires_at=timezone.now() + timedelta(minutes=5))
        with self.assertRaisesMessage(ValidationError, 'importando'):
            line_merge.merge_lines(self.target, [Part.objects.get(sku='SRC-3')], actor=self.root)
        self.assertFalse(CatalogMerge.objects.exists())
        self.assertEqual(broken_invariants(), {})

    def test_locks_follow_the_plans_order(self):
        make_variant(self.supplier, 'LM-2', '58411-07000-K', 1, actor=self.root, line=self.source)
        with CaptureQueriesContext(connection) as queries:
            line_merge.merge_lines(self.target, [self.source], actor=self.root)
        locked = [query['sql'] for query in queries.captured_queries if 'FOR UPDATE' in query['sql']]
        tables = []
        for sql in locked:
            table = sql.split(' FROM ', 1)[1].split()[0].strip('"')
            if table not in tables:
                tables.append(table)
        self.assertEqual(tables[:4], ['mall_account', 'mall_part', 'mall_partcode', 'mall_supplieritem'])

    def test_an_automatic_merge_refuses_lines_kept_apart_on_their_canonical_lines(self):
        apart, later = make_line('APART-1', description='TAMBOR'), make_line('LATER-1', description='TAMBOR')
        line_merge.keep_apart([self.source.pk, apart.pk], actor=self.root, reason='separar')
        line_merge.merge_lines(self.target, [self.source], actor=self.root)  # a person merges: the pair now reads target ↔ apart
        self.assertTrue(line_merge.apart(self.target.pk, apart.pk))
        with self.assertRaisesMessage(ValidationError, 'se mantienen separados'):
            line_merge.merge_lines(self.target, [apart], actor=self.root, automatic=True)
        merge = line_merge.merge_lines(self.target, [later], actor=self.root, automatic=True, reason='family')
        self.assertEqual((merge.actor.username, merge.summary['requested_by'], merge.summary['automatic']),
                         (line_merge.SERVICE_ACCOUNT, self.root.username, True))

    def test_lines_merged_into_a_source_are_flattened_and_come_back_with_it(self):
        child = make_line('58411-07000-KSM', description='TAMBOR FRENO KIA PICANTO')
        line_merge.merge_lines(self.source, [child], actor=self.root)
        line_merge.merge_lines(self.target, [self.source], actor=self.root)
        child.refresh_from_db()
        self.assertEqual(child.merged_into_id, self.target.pk)
        self.assertEqual(broken_invariants(), {})
        line_merge.split_line(self.target.pk, restore_all(self.target), actor=self.root)
        self.assertEqual({line.sku: main(line).code for line in Part.objects.filter(pk__in=[child.pk, self.source.pk])},
                         {'58411-07000-KSM': '58411-07000-KSM', '58411-07000-K': '58411-07000-K'})
        self.assertEqual(broken_invariants(), {})


class SplitLegacyTests(TestCase):
    """Merges made before S2b: the target holds the ('', sku) alias of each source, which was its main then."""

    def setUp(self):
        self.root = make_user('lm-legacy', superuser=True)
        self.supplier = make_supplier('LM LEGACY')

    def legacy_merge(self, target, source, items=()):
        """What merge_catalog_parts did before S2b: the source's codes and stock move, its codigo becomes a brandless alias of the
        target, the source becomes unificada (by queryset, so no shim runs), and the merge records codes, items and images."""
        unhook_variants([main(source)])  # before S3 items hung from lines only
        references.purge([main(source).pk])
        alias = Reference.objects.create(part=target, code=source.sku, brand='')
        SupplierItem.objects.filter(pk__in=[item.pk for item in items]).update(part=target)
        for item in SupplierItem.objects.filter(pk__in=[item.pk for item in items]):
            variants.ensure_attached(item)  # what S3's backfill does with them since
        Part.objects.filter(pk=source.pk).update(merged_into=target, estado='unificada', active=False)
        CatalogMerge.objects.create(actor=self.root, target=target, summary={}, sources=[{'id': str(source.pk), 'sku': source.sku, 'codes': [],
                                    'items': [str(item.pk) for item in items], 'images': []}])
        return alias

    def test_the_brandless_alias_comes_back_as_the_main(self):
        target, source = make_line('LEG-1'), make_line('LEG-1-GMB')
        alias = self.legacy_merge(target, source)
        plan = split_plan(target)
        code = next(code for code in plan['codes'] if code.pk == alias.pk)
        self.assertEqual((code.alias_of, code.alias_returns), (str(source.pk), True))
        split_catalog_part(actor=self.root, target_id=target.pk, restore=[{'id': str(source.pk)}])
        self.assertEqual((main(source).pk, main(source).part_id), (alias.pk, source.pk))
        self.assertEqual(broken_invariants(), {})

    def test_an_old_merge_retires_the_restored_lines_code_it_left_on_the_target(self):
        """S9 review #3: a merge from before S2b recorded no References, so a target code that names the restored line (its codigo
        under a brand: the worker's ADVICS 47028-60020-ADVICS on 47028-60020) is that line's and leaves the target, retired when the
        merge did not record it as the line's; only a merge since S2b keeps such a code on the target."""
        target, source = make_line('47028-60020'), make_line('47028-60020-ADVICS')
        self.legacy_merge(target, source)
        branded = Reference.objects.create(part=target, code='47028-60020-ADVICS', brand='ADVICS')
        split_catalog_part(actor=self.root, target_id=target.pk, restore=[{'id': str(source.pk)}])
        branded = Reference.all_objects.get(pk=branded.pk)
        self.assertEqual(branded.estado, 'retirada')
        self.assertEqual(main(source).code, '47028-60020-ADVICS')
        self.assertEqual(broken_invariants(), {})

    def test_a_collapsed_spelling_splits_by_the_variants_own_codigo(self):
        target, source = make_line('13780-75F00', description='FILTRO'), make_line('13780-75F00_', description='FILTRO')
        own = make_variant(self.supplier, 'COL-1', '13780-75F00', 2, actor=self.root, line=target)
        theirs = make_variant(self.supplier, 'COL-2', '13780-75F00_', 3, actor=self.root, line=source)
        self.legacy_merge(target, source, items=[theirs])
        # the alias of one compact key is the target's main's other spelling (the shim, as the S2b backfill reuses it)
        self.assertEqual(main(target).printed_forms, ['13780-75F00_'])
        CatalogMerge.objects.filter(target=target).update(sources=[{'id': str(source.pk), 'sku': source.sku}])  # nothing recorded
        plan = split_plan(target)
        self.assertEqual({item.codigo: item.suggested for item in plan['items']}, {'13780-75F00': '', '13780-75F00_': str(source.pk)})
        split_catalog_part(actor=self.root, target_id=target.pk, restore=restore_all(target))
        self.assertEqual((SupplierItem.objects.get(pk=own.pk).part_id, SupplierItem.objects.get(pk=theirs.pk).part_id), (target.pk, source.pk))
        self.assertEqual((main(source).code, main(source).estado), ('13780-75F00_', 'disputa'))  # the target keeps the key: a case decides
        self.assertEqual(main(target).printed_forms, [])  # its spelling went back with it
        self.assertEqual(broken_invariants(), {})


class SeparationTests(APITestCase):
    def setUp(self):
        self.root = make_user('lm-sep', superuser=True)
        self.client.force_authenticate(self.root)

    def test_pairs_are_read_on_canonical_lines(self):
        a, b, c = make_line('SEP-A'), make_line('SEP-B'), make_line('SEP-C')
        self.assertEqual(line_merge.keep_apart([a.pk, b.pk], actor=self.root, reason='separar'), 1)
        self.assertEqual(line_merge.keep_apart([b.pk, a.pk]), 0)  # each pair once
        line_merge.merge_lines(c, [a], actor=self.root)
        self.assertIn(line_merge.pair(c.pk, b.pk), line_merge.separated_pairs())
        self.assertEqual(line_merge.kept_apart_among([c.pk, b.pk]), [line_merge.pair(c.pk, b.pk)])
        self.assertTrue(line_merge.apart(a.pk, b.pk))

    def test_dismissing_a_compact_duplicate_or_shared_number_case_keeps_its_lines_apart(self):
        from .matching_engine import part_row, save_case
        lines = [make_line(f'CASE-{n}') for n in range(3)]
        rows = [part_row(Part.objects.prefetch_related('codes').get(pk=line.pk)) for line in lines]
        shared = save_case(key='catalog:shared:x', kind='catalog', sources=rows[1:], candidates=rows[:1], target='CASE-0',
                           method='shared_oem_number', reason='r')
        family = save_case(key='catalog:FAM', kind='catalog', sources=rows[1:2], candidates=rows[:1], target='CASE-0', method='family_review',
                           reason='r')
        for case in (shared, family):
            response = self.client.post(f'/api/v1/management/matching/{case.pk}/', {'action': 'dismiss', 'fingerprint': case.fingerprint},
                                        format='json')
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(CatalogSeparation.objects.filter(case=shared).count(), 3)  # every pair of its three lines
        self.assertEqual(set(CatalogSeparation.objects.values_list('reason', flat=True)), {'shared_oem_number'})  # a family dismissal writes none
        self.assertTrue(line_merge.apart(lines[1].pk, lines[2].pk))
        self.assertEqual(MatchingDecision.objects.get(case=shared).evidence['separated'], 3)


