"""Every reader of the OEM line takes its lines from mall.lines with an explicit estado decision (inventory refactor S1, plan §3 S1
"Readers"). A line in referencia (a number of the OEM library with no product yet, S4) and one in preparación (a new product being
prepared, S6), both made by the factories, never reach a reader that lists the catalog or offers it for sale, even with matched stock
of their own; detail endpoints read a line in any estado and refuse writes to a unificada one; and the checks of who already holds a
codigo still see every line (UNIQUE(sku) covers them all until S4). One test per reader site."""
import uuid
from types import SimpleNamespace

from django.test import TestCase
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from . import test_requests as request_tests
from .analytics_models import UsageEvent
from .basket import requestable
from .models import Part, PartCode, PartType, WishlistItem
from .test_factories import make_library_number, make_line, make_reference, make_supplier, make_user, make_variant

HIDDEN = ('referencia', 'preparacion')


def hidden(sku, description='', **fields):
    """{estado: line}: a library number with no product yet (referencia) and a new product being prepared (preparacion), under sku and
    sku + '-P' (UNIQUE(sku) until S4) and described alike, so whatever finds the one finds the other."""
    return {'referencia': make_line(sku, name=sku, description=description, estado='referencia', origen='biblioteca', **fields),
            'preparacion': make_line(f'{sku}-P', name=f'{sku}-P', description=description, estado='preparacion', origen='proveedor',
                                     **fields)}


def ids(rows):
    return {str(row['id']) for row in rows}


class StorefrontReaderTests(APITestCase):
    """What customers see and order: storefront_lines (publicada), whatever the scope, and only articles on a line on sale."""
    item = request_tests.SupplierRequestWorkflowTests.item
    payload = request_tests.SupplierRequestWorkflowTests.payload
    submit = request_tests.SupplierRequestWorkflowTests.submit
    url = request_tests.SupplierRequestWorkflowTests.url

    def setUp(self):
        request_tests.SupplierRequestWorkflowTests.setUp(self)  # self.part 58411-1R000 on sale, items of PROVEEDOR A and B
        self.hidden = hidden('58411-1R099', 'TAMBOR FRENO HYU ACCENT')
        self.hidden_items = {estado: make_variant(self.supplier_a, f'H-{estado}', line.sku, 8, actor=self.seller_a, brand='KSM',
                                                  description='TAMBOR', line=line) for estado, line in self.hidden.items()}

    def assertHidden(self, found):
        self.assertFalse({str(line.pk) for line in self.hidden.values()} & {str(value) for value in found})

    def test_the_catalog_list_suggest_and_facets(self):
        response = self.client.get('/api/v1/catalog/', {'search': '58411', 'include_facets': '1'})
        self.assertEqual(ids(response.data['results']), {str(self.part.pk)})
        facets = response.data['facets']
        self.assertEqual({row['label']: row['count'] for row in facets['supplier']}, {'PROVEEDOR A': 1, 'PROVEEDOR B': 1})
        self.assertEqual(sum(row['count'] for row in facets['availability'] if row['value'] == 'in_stock'), 1)
        self.assertEqual(ids(self.client.get('/api/v1/catalog/').data['results']), {str(self.part.pk)})
        self.assertHidden(ids(self.client.get('/api/v1/catalog/suggest/', {'search': 'TAMBOR'}).data))
        # A supplier-only member whose own matched items sit on them does not see them either.
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(ids(self.client.get('/api/v1/catalog/', {'account': str(self.supplier_a.pk)}).data['results']), {str(self.part.pk)})

    def test_offers_and_the_public_technical_sheet(self):
        self.assertEqual(self.client.get(f'/api/v1/catalog/{self.part.pk}/suppliers/').status_code, 200)
        self.assertEqual(self.client.get(f'/api/v1/catalog/{self.part.pk}/technical/').status_code, 200)
        for line in self.hidden.values():
            self.assertEqual(self.client.get(f'/api/v1/catalog/{line.pk}/suppliers/').status_code, 404, line.estado)
            self.assertEqual(self.client.get(f'/api/v1/catalog/{line.pk}/technical/').status_code, 404, line.estado)

    def test_the_wishlist(self):
        for line in self.hidden.values():
            response = self.client.put(f'/api/v1/wishlist/{line.pk}/')
            self.assertEqual(response.status_code, 400, line.estado)
            WishlistItem.objects.create(user=self.buyer, part=line)  # saved while it was on sale, say
        rows = self.client.get('/api/v1/wishlist/').data['results']
        self.assertEqual({row['part']['sku']: (row['available'], row['part']['availability']['supplier_count']) for row in rows},
                         {line.sku: (False, 0) for line in self.hidden.values()})  # their matched stock is not counted

    def test_the_request_state(self):
        base = f'/api/v1/accounts/{self.client_account.pk}/catalog'
        self.assertEqual(self.client.get(f'{base}/{self.part.pk}/request-state/').status_code, 200)
        for line in self.hidden.values():
            self.assertEqual(self.client.get(f'{base}/{line.pk}/request-state/').status_code, 404, line.estado)

    def test_analytics_events(self):
        def post(kind, **values):
            return self.client.post('/api/v1/analytics/events/', {'event_id': str(uuid.uuid4()), 'visit_id': str(uuid.uuid4()),
                                                                  'account_id': str(self.client_account.pk), 'kind': kind, **values}, format='json')
        self.assertEqual(post('search', term='58411').status_code, 202)
        self.assertEqual(UsageEvent.objects.get(kind='search').result_count, 1)
        for line in self.hidden.values():
            self.assertEqual(post('part_view', part_id=str(line.pk)).status_code, 404, line.estado)

    def test_the_cesta_and_the_solicitud_refuse_their_articles(self):
        basket = f'/api/v1/accounts/{self.client_account.pk}/basket/'
        for estado, item in self.hidden_items.items():
            item.refresh_from_db()
            self.assertFalse(requestable(item), estado)
            response = self.client.post(basket, {'changes': [{'supplier_item_id': str(item.pk), 'quantity': 1}]}, format='json')
            self.assertEqual(response.status_code, 400, estado)
            self.assertIn('ya no está disponible', str(response.data))
            refused = self.submit(self.payload([(item, 1)]))
            self.assertEqual(refused.status_code, 400, estado)
            self.assertIn('ya no está disponible', str(refused.data))

    def test_the_whatsapp_assistant_finds_and_offers_only_lines_on_sale(self):
        from .whatsapp_assistant import find, offers
        parts, _ = find(['58411'], [])
        self.assertEqual([part.pk for part in parts], [self.part.pk])
        said = []
        turn = SimpleNamespace(session=SimpleNamespace(step='parts', options=[], pending={}), account=self.client_account,
                               say=lambda *message: said.append(message))
        for line in self.hidden.values():
            offers(turn, str(line.pk))
            self.assertIn('ya no está en el catálogo', str(said.pop()), line.estado)

    def test_pedir_lo_que_falta_offers_only_lines_on_sale(self):
        from .reorder import offers_for
        order = SimpleNamespace(supplier_id=self.supplier_b.pk)
        self.assertEqual(set(offers_for(order, self.client_account, {self.part.pk})), {self.part.pk})
        self.assertEqual(offers_for(order, self.client_account, {line.pk for line in self.hidden.values()}), {})

    def test_a_load_matches_its_codes_to_lines_on_sale_only(self):
        from .services import matching_part
        self.assertEqual(matching_part('58411-1R000', ''), self.part)
        for line in self.hidden.values():
            self.assertIsNone(matching_part(line.sku, ''), line.estado)
            item = make_variant(self.supplier_b, f'LOAD-{line.estado}', line.sku, 1, actor=self.seller_b)
            self.assertNotEqual(item.part_id, line.pk)


class AdminReaderTests(APITestCase):
    """The admin's lists read catalog_lines (publicada and retirada) or the lines on sale; its detail endpoints read any estado and
    refuse writes to a unificada line; its uniqueness checks read every line."""

    def setUp(self):
        self.root = make_user('readers-root', superuser=True)
        self.client.force_authenticate(self.root)
        self.part = make_line('48654-0K040', description='BASE AMORTIGUADOR TOY HILUX', category='SUSPENSION', subcategory='BASES')
        self.retired = make_line('48654-0K041', description='BASE AMORTIGUADOR TOY HILUX', active=False)
        self.hidden = hidden('48654-0K099', 'BASE AMORTIGUADOR TOY HILUX', category='SUSPENSION', subcategory='BASES')

    def assertHidden(self, found):
        self.assertFalse({str(line.pk) for line in self.hidden.values()} & {str(value) for value in found})

    def test_the_grid_and_its_row_reread(self):
        rows = self.client.get('/api/v1/management/catalog/', {'search': '48654'}).data['results']
        self.assertEqual(ids(rows), {str(self.part.pk), str(self.retired.pk)})
        reread = self.client.get('/api/v1/management/catalog/', {'ids': ','.join(str(line.pk) for line in self.hidden.values())})
        self.assertEqual(reread.data['results'], [])

    def test_the_detail_reads_any_estado_and_a_unificada_line_is_read_only(self):
        for line in self.hidden.values():
            response = self.client.get(f'/api/v1/management/catalog/{line.pk}/')
            self.assertEqual((response.status_code, response.data['estado']), (200, line.estado))
        edited = self.client.patch(f'/api/v1/management/catalog/{self.hidden["preparacion"].pk}/', {'description': 'BASE AMORT HILUX'}, format='json')
        self.assertEqual(edited.status_code, 200, edited.data)
        merged = make_line('48654-0K040-GMB', merged_into=self.part)
        self.assertEqual(self.client.get(f'/api/v1/management/catalog/{merged.pk}/').data['estado'], 'unificada')
        refused = self.client.patch(f'/api/v1/management/catalog/{merged.pk}/', {'description': 'OTRA'}, format='json')
        self.assertEqual(refused.status_code, 409)
        self.assertIn(self.part.sku, refused.data['detail'])
        self.assertEqual(Part.objects.get(pk=merged.pk).description, '')

    def test_the_batch_edit_names_the_estado_of_a_line_off_the_catalog(self):
        response = self.client.post('/api/v1/management/catalog/bulk-edit/',
                                    {'rows': [{'id': str(line.pk), 'description': 'X'} for line in self.hidden.values()]}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual({error['sku']: error['detail'] for error in response.data['errors']},
                         {self.hidden['referencia'].sku: 'Está en referencia y no forma parte del catálogo.',
                          self.hidden['preparacion'].sku: 'Está en preparación y no forma parte del catálogo.'})

    def test_a_codigo_a_line_off_the_catalog_holds_stays_taken(self):
        for line in self.hidden.values():
            created = self.client.post('/api/v1/management/catalog/', {'sku': line.sku}, format='json')
            self.assertEqual(created.status_code, 400, line.estado)
            coded = self.client.patch(f'/api/v1/management/catalog/{self.part.pk}/', {'codes': [{'brand': '', 'code': line.sku}]}, format='json')
            self.assertEqual(coded.status_code, 400, line.estado)
            self.assertIn('ya pertenece a otro SKU', str(coded.data))

    def test_the_technical_sheet_and_the_subgrupo_counts(self):
        line = self.hidden['preparacion']
        self.assertEqual(self.client.get(f'/api/v1/management/catalog/{line.pk}/technical/').status_code, 200)
        merged = make_line('48654-0K040-RAZ', merged_into=self.part)
        self.assertEqual(self.client.get(f'/api/v1/management/catalog/{merged.pk}/technical/').status_code, 200)
        refused = self.client.patch(f'/api/v1/management/catalog/{merged.pk}/technical/', {'revision': 0}, format='json')
        self.assertEqual(refused.status_code, 409)
        bases = PartType.objects.get(pk=self.part.part_type_id)
        self.assertEqual(Part.objects.filter(part_type=bases).count(), 3)
        row = next(row for row in self.client.get('/api/v1/management/part-types/', {'search': 'BASES'}).data['results'] if row['id'] == str(bases.pk))
        self.assertEqual(row['part_count'], 1)

    def test_the_gallery_reads_any_estado_and_a_unificada_line_takes_no_image(self):
        for line in self.hidden.values():
            self.assertEqual(self.client.get(f'/api/v1/management/catalog/{line.pk}/images/').data, [])
        merged = make_line('48654-0K040-KOY', merged_into=self.part)
        path = f'/api/v1/management/catalog/{merged.pk}/images/'
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.post(path, {}, format='multipart').status_code, 409)
        self.assertEqual(self.client.delete(f'{path}{uuid.uuid4()}/').status_code, 409)

    def test_the_ai_oem_lookup(self):
        for line in self.hidden.values():
            self.assertEqual(self.client.post(f'/api/v1/management/catalog/{line.pk}/oem-lookup/', {}, format='json').status_code, 404)

    def test_the_merged_list_shows_catalog_lines_only(self):
        make_line('48654-0K040-NPW', merged_into=self.part)
        make_line('48654-0K099-NPW', merged_into=self.hidden['referencia'])
        rows = self.client.get('/api/v1/management/catalog/merged/').data['results']
        self.assertEqual([row['sku'] for row in rows], [self.part.sku])

    def test_the_alternate_review_and_the_mark_oem_plan(self):
        from .catalog_oem_mark import plan_rows
        for line in self.hidden.values():
            self.assertEqual(self.client.get(f'/api/v1/management/alternate-review/{line.pk}/').status_code, 404, line.estado)
        self.assertEqual([str(row['id']) for row in plan_rows([self.part.pk, *[line.pk for line in self.hidden.values()]])], [str(self.part.pk)])


class CatalogToolReaderTests(TestCase):
    """The catalog's AI, grouping, identity, import and matching tools work on the lines on sale (or the catalog's), and see every
    line where a codigo another one holds must stay taken."""

    def setUp(self):
        self.root = make_user('tools-root', superuser=True)

    def test_the_catalog_assistant(self):
        from .catalog_assistant import eligible_parts
        part = make_line('04465-0K290', description='PASTILLA FRENO TOY HILUX')
        lines = hidden('04465-0K299', 'PASTILLA FRENO TOY HILUX')
        self.assertEqual(list(eligible_parts('all', '04465')), [part])
        self.assertEqual(list(eligible_parts('unclassified', 'PASTILLA')), [part])
        self.assertTrue(all(line.sku not in str(list(eligible_parts('all', ''))) for line in lines.values()))

    def test_grouping_suggestions_leave_them_out_but_their_codigos_block_a_parent(self):
        from .catalog_grouping_suggestions import suggested_families
        description = 'TAMBOR HYU ACCENT 11-16'
        make_line('58411-1R000', description=description)
        make_line('58411-1R000-KSM', description=description)
        make_line('58411-1R000-RAZ', description=description, estado='referencia')
        make_line('58411-1R000-GMB', description=description, estado='preparacion')
        make_line('90915-YZZD2-KSM', description='FILTRO ACEITE TOY', estado='referencia')  # no parent from lines off sale
        make_line('90915-YZZD2-RAZ', description='FILTRO ACEITE TOY', estado='preparacion')
        make_line('23300-0L041-KSM', description='FILTRO COMBUSTIBLE TOY')  # a parent another line holds is not proposed
        make_line('23300-0L041-RAZ', description='FILTRO COMBUSTIBLE TOY')
        make_line('23300-0L041', estado='referencia')
        families = suggested_families()
        self.assertEqual(set(families), {'58411-1R000'})
        self.assertEqual([row['sku'] for row in families['58411-1R000']['sources']], ['58411-1R000-KSM'])

    def test_grouping_refuses_a_line_off_sale_as_source_or_target(self):
        from .catalog_grouping import merge_catalog_parts
        part = make_line('17801-30070', description='FILTRO AIRE TOY')
        lines = hidden('17801-30070-KSM', 'FILTRO AIRE TOY')
        for line in lines.values():
            with self.assertRaises(ValidationError):
                merge_catalog_parts(actor=self.root, target_sku=part.sku, source_skus=[line.sku])
            with self.assertRaises(ValidationError):
                merge_catalog_parts(actor=self.root, target_sku=line.sku, source_skus=[part.sku])
        self.assertFalse(Part.objects.filter(merged_into__isnull=False).exists())

    def test_the_import_classifier_proposes_no_line_off_sale(self):
        from .catalog_classification import candidate_context
        part = make_line('MR-453383', description='EJE COMPLETO MIT')
        lines = hidden('MR-453399', 'EJE COMPLETO MIT')
        new = 'SUPPLIER-MR453383'
        codes = {('', part.sku): 2, **{('', line.sku): 3 for line in lines.values()}}
        description = ' '.join(['EJE', part.sku, *[line.sku for line in lines.values()]])
        incoming = {new: {'sku': new, 'row': 2, 'fields': {'name': new, 'description': description}, 'codes': codes}}
        _, candidates, allowed = candidate_context(incoming, [new])
        self.assertIn(part.sku, candidates)
        self.assertFalse({line.sku for line in lines.values()} & (set(candidates) | set(allowed[new])))

    def test_the_catalog_import_sees_every_line_its_skus_name(self):
        from .catalog_import import build_plan
        line = make_line('MR-968365', estado='referencia')
        plan, _ = build_plan({line.sku: {'sku': line.sku, 'row': 2, 'fields': {'description': 'EJE'}, 'codes': {}}}, [], 1)
        self.assertEqual(plan['summary']['created_skus'], 0)  # UNIQUE(sku) holds it: S4 decides what an import does with one

    def test_identity_marks_and_unmarks_only_catalog_lines(self):
        from .catalog_identity import exact_items, unmark_disputed
        ref = make_library_number('MITSUBISHI', 'MR453383')
        part = make_line('MR-453383', description='EJE MIT')
        draft = make_line('MR453383', description='EJE MIT', estado='preparacion')
        self.assertEqual(set(exact_items()), {part.pk})
        Part.objects.filter(pk__in=[part.pk, draft.pk]).update(is_OEM=True)
        self.assertEqual(unmark_disputed(ref), [part.sku])
        self.assertTrue(Part.objects.get(pk=draft.pk).is_OEM)

    def test_supplier_matching_never_lands_on_a_line_off_sale(self):
        from .matching_engine import MatchIndex, catalog_families, reconcile_items
        part = make_line('16100-39315', description='BOMBA AGUA TOY 22R')
        lines = hidden('16100-39399', 'BOMBA AGUA TOY 22R')
        index = MatchIndex()
        self.assertEqual(set(index.parts), {str(part.pk)})
        self.assertNotIn('16100-39399', str(catalog_families()))
        supplier = make_supplier('PROVEEDOR MATCH')
        items = [make_variant(supplier, f'M-{line.estado}', line.sku, 2, actor=self.root) for line in lines.values()]
        reconcile_items(MatchIndex())
        for item, line in zip(items, lines.values()):
            item.refresh_from_db()
            self.assertNotEqual(item.part_id, line.pk)

    def test_a_matching_review_targets_a_line_on_sale(self):
        from rest_framework.test import APIClient
        from .matching_engine import item_row, save_case
        supplier = make_supplier('PROVEEDOR CASO')
        item = make_variant(supplier, 'C-1', 'ZZ-SIN-SKU-1', 1, actor=self.root)
        case = save_case(key=f'supplier:{item.pk}', kind='supplier', sources=[item_row(item)], candidates=[], item=item, status='unmatched')
        client = APIClient()
        client.force_authenticate(self.root)
        for line in hidden('ZZ-SIN-SKU-9', 'ZZ').values():
            response = client.post(f'/api/v1/management/matching/{case.pk}/', {'action': 'approve', 'target_sku': line.sku,
                                                                              'fingerprint': case.fingerprint}, format='json')
            self.assertEqual(response.status_code, 409, line.estado)
            self.assertIn('Selecciona un SKU activo', str(response.data))


class LibraryReaderTests(TestCase):
    """The OEM library links a number to the catalog's lines; the OEM review and Productos nuevos read the lines on sale, and every
    line where it asks who already holds a code."""

    def test_name_links_go_to_catalog_lines(self):
        from .oem_links import refresh_name_links
        make_library_number('TOYOTA', '17801-30070')
        hidden('17801-30070-G', 'FILTRO AIRE TOY HILUX')
        self.assertEqual(refresh_name_links(apply=False)['skus'], 0)
        make_line('17801-30070-KSM', description='FILTRO AIRE TOY HILUX')
        self.assertEqual(refresh_name_links(apply=False)['skus'], 1)

    def test_a_number_is_linked_by_catalog_lines_only(self):
        from .oem_reference import linked_parts, linked_q, reference_pair
        from .oem_reference_models import OEMReference
        ref = make_library_number('TOYOTA', '17801-30070')
        lines = hidden('17801-30070', 'FILTRO AIRE TOY')
        make_reference(lines['preparacion'], '1780130070', tipo='otro')
        linked = lambda: OEMReference.objects.annotate(pair=reference_pair()).filter(linked_q()).exists()
        self.assertEqual((linked_parts([ref])[ref.pk], linked()), ([], False))
        make_line('17801 30070', description='FILTRO AIRE TOY')
        self.assertEqual(([row['sku'] for row in linked_parts([ref])[ref.pk]], linked()), (['17801 30070'], True))

    def test_the_oem_review_reads_lines_on_sale_and_every_owner(self):
        from .oem_review import key_owners, merge_family, sku_claimants
        part = make_line('48654-0K040', description='BASE AMORT TOY')
        sibling = make_line('48654-0K040-KSM', description='BASE AMORT TOY')
        lines = {'referencia': make_line('48654-0K040-NAK', estado='referencia'), 'preparacion': make_line('48654-0K040-RAZ', estado='preparacion')}
        case = SimpleNamespace(evidence={}, part_id=part.pk, candidate='48654-0K040', brand='TOYOTA', written_form='')
        self.assertEqual([sku for _, sku in sku_claimants(part, case, '486540K040')], [sibling.sku])
        case.evidence = {'primary': {'key': '486540K040', 'conflicts': [{'part_id': str(line.pk), 'kind': 'active_sku'} for line in lines.values()]},
                         'shared_with': [line.sku for line in lines.values()]}
        self.assertEqual(merge_family(case)['source_skus'], [])
        owner = make_line('486540K040', estado='referencia')  # who holds the number: every estado
        self.assertEqual(key_owners(part, '48654-0K040'), [(str(owner.pk), owner.sku)])

    def test_productos_nuevos_owners_and_links(self):
        from .sku_proposal_models import SkuProposal
        from .sku_proposals import catalog_owners, link_existing
        lines = hidden('MR-968365', 'EJE MIT')
        self.assertEqual(catalog_owners('MR968365'), [])
        self.assertEqual(catalog_owners('MR968365-P'), [])
        make_reference(lines['referencia'], 'MR-968366')
        self.assertEqual(catalog_owners('MR968366'), [])
        part = make_line('MR-968365-KSM', description='EJE MIT')
        self.assertEqual([owner[1] for owner in catalog_owners('MR968365KSM')], [part.sku])
        supplier, root = make_supplier('PROVEEDOR PN'), make_user('pn-root', superuser=True)
        item = make_variant(supplier, 'PN-1', 'MR-968377', 1, actor=root)
        proposal = SkuProposal.objects.create(key='MR968377|', sku='MR-968377', fingerprint='x')
        proposal.items.create(item=item)
        for line in lines.values():
            with self.assertRaises(ValidationError):
                link_existing(proposal.pk, root, proposal.version, line.pk)

    def test_the_ai_review_reads_lines_on_sale(self):
        from .sku_proposal_models import SkuProposal
        from .sku_review import candidates, siblings, vocabulary
        hidden('F02-3501116-01AA', 'ZZQUIRKY DISCO')
        part = make_line('FX12-3501116-01AA', description='DISCO FRENO')
        proposal = SkuProposal.objects.create(key='FX11|', sku='FX11-3501116-01AA', fingerprint='x')
        self.assertEqual([row['codigo'] for row in siblings(proposal, [])], [part.sku])
        hidden('FX11-3501116-01AB', 'ZZQUIRKY DISCO')
        self.assertEqual([row['sku'] for row in candidates(proposal) if row['kind'] == 'part'], [])
        self.assertNotIn('ZZQUIRKY', vocabulary())

    def test_the_supplier_catalog_import_counts_shafts_on_sale(self):
        from . import supplier_catalog_import as sci
        hidden('MR-580474', 'EJE COMPLETO MIT MONTERO')
        self.assertNotIn('MR580474', sci.shaft_numbers())
        make_line('MR-580475', description='EJE COMPLETO MIT MONTERO')
        self.assertIn('MR580475', sci.shaft_numbers())

    def test_an_excel_load_matches_lines_on_sale_only(self):
        from .supplier_import import stock_plan
        supplier = make_supplier('PROVEEDOR EXCEL')
        lines = hidden('58411-1R077', 'TAMBOR')
        part = make_line('58411-1R078', description='TAMBOR')
        rows = [{'row': n, 'data': {'supplier_invent_id': f'X-{n}', 'codigo': sku, 'brand': '', 'quantity': 1}}
                for n, sku in enumerate([part.sku, *[line.sku for line in lines.values()]], 2)]
        plan, _ = stock_plan(supplier, rows)
        self.assertEqual([(row['codigo'], row['matching_status'], row['part_id']) for row in plan['preview']],
                         [(part.sku, 'matched', str(part.pk)), *[(line.sku, 'pending', None) for line in lines.values()]])

    def test_the_alternos_prune_reads_catalog_lines(self):
        from . import prune_catalog_alternos as prune
        from .oem_reference import upsert_reference
        catalog = 'GMB Water Pump Catalog 2016'
        upsert_reference('TOYOTA', '16100-39315', source_kind='aftermarket_catalog', citation=catalog, detail={'catalog': 'gmb_wp_2016'})
        lines = hidden('16100-39315-RAZ', 'BOMBA AGUA TOY 22R')
        for code, line in zip(('GWT-41A', 'GWT-41B'), lines.values()):
            PartCode.objects.create(part=line, brand='GMB', code=code, ref_type='company', reference_source=f'{catalog} · pág. 212')
        self.assertEqual(prune.plan(), {'remove': [], 'review': {}})
