from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from .matching_engine import item_row, save_case
from .matching_models import MatchingCase, MatchingDecision, MatchingQueue, SupplierCodeMapping
from .models import Account, Membership, Part, PartCode, PartType, Role, SupplierItem, User
from .oem_reference_models import OEMCrossReference, OEMReference
from .sku_proposal_models import SkuProposal, SkuProposalItem
from .sku_proposals import ProposalConflict, approve, edit, link_existing, propose, proposals_stage, reject, vehicle_makes


@override_settings(CATALOG_EXTRA_MAKES=['KAIYI'])
class SkuProposalTests(APITestCase):
    def setUp(self):
        self.root = User.objects.create_superuser('root', 'root@example.com', 'SecureRoot938!')
        self.dealer = self.supplier('Motion Motors')
        self.other = self.supplier('Repuestos Chinos')
        self.client.force_authenticate(self.root)

    def supplier(self, name):
        account = Account.objects.create(name=name)
        account.roles.add(Role.objects.get(code='supplier_wholesale'))
        return account

    def item(self, account, invent_id, codigo, brand, description='', case='unmatched', **fields):
        item = SupplierItem.objects.create(supplier=account, supplier_invent_id=invent_id, codigo=codigo, brand=brand, description=description,
                                           source='apiag', **fields)
        if case:
            save_case(key=f'supplier:{item.pk}', kind='supplier', sources=[item_row(item)], candidates=[] if case != 'review' else [{'sku': 'X'}],
                      item=item, status=case)
        return item

    def proposal(self, sku):
        return SkuProposal.objects.get(sku=sku)

    def test_unmatched_items_become_one_proposal_per_product(self):
        genuine = self.item(self.dealer, '10', '025CHA-1502606', 'KAIYI', 'O-RING DEL CUERPO DE VALVULAS KAIYI GS11', reported_quantity=4)
        same = self.item(self.other, 'A7', '025CHA 1502606', 'kaiyi', 'O-RING KAIYI', reported_quantity=1)   # another supplier, same product
        dfsk = self.item(self.dealer, '11', '10045678-00', 'DFSK', 'FILTRO DE ACEITE DFSK C31')
        wurth = self.item(self.dealer, '12', '0500-900-013', 'WURTH', 'CLIP DE COVER DE MOTOR')
        self.item(self.dealer, '13', 'XX-1', 'ACME', case='review')       # has candidates: Agrupación inteligente handles it
        self.item(self.dealer, '14', 'XX-2', 'ACME', case=None)           # not examined by matching yet
        self.assertEqual(propose()['created'], 3)
        kaiyi = self.proposal('025CHA-1502606')
        self.assertEqual((kaiyi.brand, kaiyi.oem_code, kaiyi.oem_brand, kaiyi.status, kaiyi.description),
                         ('KAIYI', '025CHA-1502606', 'KAIYI', 'pending', 'O-RING DEL CUERPO DE VALVULAS KAIYI GS11'))
        self.assertEqual(set(kaiyi.items.values_list('item_id', flat=True)), {genuine.pk, same.pk})
        self.assertEqual(kaiyi.references, [])   # the other printed form becomes an alterno on approval, from the items themselves
        self.assertEqual(kaiyi.field_sources['oem'], {'source': 'supplier'})
        self.assertEqual((self.proposal('10045678-00').oem_brand, self.proposal('0500-900-013').oem_code), ('DFSK', ''))
        self.assertEqual(SkuProposal.objects.count(), 3)
        self.assertEqual(propose().get('created', 0), 0)                   # nothing new: nothing written
        self.assertEqual(kaiyi.version, self.proposal('025CHA-1502606').version)
        self.assertIn('KAIYI', vehicle_makes())
        self.assertNotIn('WURTH', vehicle_makes())

    def test_the_oem_library_gives_an_aftermarket_code_its_oem_number(self):
        reference = OEMReference.objects.create(manufacturer='TOYOTA', code='1610039315', printed_forms=['16100-39315'])
        OEMCrossReference.objects.create(reference=reference, brand='GMB', code='GWT-41A', number='GWT41A')
        self.item(self.dealer, '20', 'GWT-41A', 'GMB', 'BOMBA DE AGUA')
        propose()
        proposal = self.proposal('GWT-41A')
        self.assertEqual((proposal.oem_code, proposal.oem_brand, proposal.field_sources['oem']), ('16100-39315', 'TOYOTA', {'source': 'oem_library'}))

    def test_approving_creates_the_sku_with_its_oem_and_links_every_item(self):
        first = self.item(self.dealer, '10', '025CHA-1502606', 'KAIYI', 'O-RING KAIYI GS11', reported_quantity=4)
        second = self.item(self.other, 'A7', '025CHA-1502606', 'KAIYI', 'O-RING KAIYI GS11')
        propose()
        proposal = self.proposal('025CHA-1502606')
        approve(proposal.pk, self.root, proposal.version)
        proposal.refresh_from_db()
        part = proposal.part
        self.assertEqual((proposal.status, proposal.decided_by, part.sku, part.is_OEM, part.description), ('approved', self.root, '025CHA-1502606', True, 'O-RING KAIYI GS11'))
        code = PartCode.objects.get(part=part, ref_type='oem')
        self.assertEqual((code.brand, code.code), ('KAIYI', '025CHA-1502606'))
        self.assertIn('Motion Motors', code.reference_source)
        for item in (first, second):
            item.refresh_from_db()
            self.assertEqual((item.part, item.matching_status), (part, 'matched'))
            self.assertEqual(MatchingCase.objects.get(key=f'supplier:{item.pk}').status, 'applied')
        self.assertEqual(MatchingDecision.objects.filter(action='proposal_approved').count(), 2)
        self.assertEqual(SupplierCodeMapping.objects.filter(part=part, actor=self.root).count(), 2)

    def test_an_aftermarket_item_with_a_library_oem_gets_the_oem_as_its_main(self):
        reference = OEMReference.objects.create(manufacturer='TOYOTA', code='1610039315', printed_forms=['16100-39315'])
        OEMCrossReference.objects.create(reference=reference, brand='GMB', code='GWT-41A', number='GWT41A')
        self.item(self.dealer, '20', 'GWT-41A', 'GMB', 'BOMBA DE AGUA TOYOTA HILUX')
        propose()
        proposal = self.proposal('GWT-41A')
        approve(proposal.pk, self.root, proposal.version)
        part = SkuProposal.objects.get(pk=proposal.pk).part
        self.assertEqual((part.sku, part.is_OEM), ('16100-39315', True))   # OEM-preferred MAIN, the supplier's code stays an alterno
        self.assertTrue(PartCode.objects.filter(part=part, code='GWT-41A').exists())

    def test_conflicts_and_stale_items_save_nothing(self):
        item = self.item(self.dealer, '10', 'A-1', 'KAIYI', 'PIEZA A')
        propose()
        proposal = self.proposal('A-1')
        response = self.client.post(f'/api/v1/management/catalog/proposals/{proposal.pk}/', {'action': 'approve', 'expected_version': proposal.version + 1}, format='json')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['proposal']['version'], proposal.version)
        SupplierItem.objects.filter(pk=item.pk).update(description='OTRA PIEZA')     # changed after it was proposed
        response = self.client.post(f'/api/v1/management/catalog/proposals/{proposal.pk}/', {'action': 'approve', 'expected_version': proposal.version}, format='json')
        self.assertEqual(response.status_code, 409)
        self.assertFalse(Part.objects.filter(sku='A-1').exists())
        self.assertEqual(self.proposal('A-1').status, 'pending')
        Part.objects.create(sku='B-1', name='B-1')
        self.item(self.dealer, '11', 'B-1', 'KAIYI', 'PIEZA B')  # a SKU that exists already (matching would have linked it)
        propose()
        clash = self.proposal('B-1')
        response = self.client.post(f'/api/v1/management/catalog/proposals/{clash.pk}/', {'action': 'approve', 'expected_version': clash.version}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.proposal('B-1').status, 'pending')

    def test_link_to_an_existing_sku_or_reject(self):
        item = self.item(self.dealer, '10', 'A-1', 'ACME', 'PIEZA A')
        self.item(self.dealer, '11', 'B-1', 'ACME', 'PIEZA B')
        propose()
        target = Part.objects.create(sku='MASTER-1', name='MASTER-1')
        link_existing(self.proposal('A-1').pk, self.root, self.proposal('A-1').version, target.pk)
        item.refresh_from_db()
        self.assertEqual((item.part, self.proposal('A-1').status), (target, 'linked'))
        rejected = self.proposal('B-1')
        reject(rejected.pk, self.root, rejected.version, 'not_a_part', 'Es un servicio')
        self.assertEqual((self.proposal('B-1').status, self.proposal('B-1').reject_note), ('rejected', 'Es un servicio'))
        propose()
        self.assertEqual(self.proposal('B-1').status, 'rejected')            # unchanged items: the rejection stands
        SupplierItem.objects.filter(supplier_invent_id='11').update(description='PIEZA B 12V', updated_at=timezone.now() + timedelta(seconds=5))
        propose()
        self.assertEqual(self.proposal('B-1').status, 'pending')             # its item changed: a fresh look

    def test_an_administrators_edits_stick_and_matched_items_leave(self):
        item = self.item(self.dealer, '10', 'A-1', 'ACME', 'pieza a')
        propose()
        proposal = self.proposal('A-1')
        part_type = PartType.objects.create(category='FRENOS', name='PASTILLAS')
        edit(proposal.pk, self.root, proposal.version, {'description': 'Pastilla delantera', 'part_type': str(part_type.pk), 'oem_code': '04465-0K240', 'oem_brand': 'toyota'})
        proposal.refresh_from_db()
        self.assertEqual((proposal.description, proposal.part_type, proposal.oem_code, proposal.oem_brand), ('PASTILLA DELANTERA', part_type, '04465-0K240', 'TOYOTA'))
        self.assertEqual(proposal.field_sources['description']['source'], 'admin')
        SupplierItem.objects.filter(pk=item.pk).update(description='PIEZA A NUEVA', updated_at=timezone.now() + timedelta(seconds=5))
        propose()
        self.assertEqual(self.proposal('A-1').description, 'PASTILLA DELANTERA')   # the supplier's text no longer overwrites it
        with self.assertRaises(Exception):
            edit(proposal.pk, self.root, proposal.version, {'sku': 'OTRO'})   # stale version
        SupplierItem.objects.filter(pk=item.pk).update(part=Part.objects.create(sku='ELSEWHERE', name='ELSEWHERE'), matching_status='matched')
        self.assertEqual(propose()['resolved'], 1)
        self.assertEqual(self.proposal('A-1').status, 'resolved')

    def test_the_summary_dates_the_newest_pending_change_for_the_storefront_reminder(self):
        url = '/api/v1/management/catalog/proposals/summary/'
        self.assertIsNone(self.client.get(url).json()['pending_changed_at'])
        self.item(self.dealer, '10', 'A-1', 'KAIYI', 'PIEZA A')
        self.item(self.dealer, '11', 'B-1', 'KAIYI', 'PIEZA B')
        proposals_stage()
        first, second = self.proposal('A-1'), self.proposal('B-1')
        SkuProposal.objects.filter(pk=first.pk).update(updated_at=timezone.now() - timedelta(hours=2))
        SkuProposal.objects.filter(pk=second.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        second.refresh_from_db()
        self.assertEqual(self.client.get(url).json()['pending_changed_at'].replace('Z', '+00:00'), second.updated_at.isoformat())
        # Reviewing the newest leaves the older one's date: no new arrival, so a closed reminder stays closed.
        reject(second.pk, self.root, second.version, 'duplicate', '')
        first.refresh_from_db()
        self.assertEqual(self.client.get(url).json()['pending_changed_at'].replace('Z', '+00:00'), first.updated_at.isoformat())
        reject(first.pk, self.root, first.version, 'duplicate', '')
        self.assertIsNone(self.client.get(url).json()['pending_changed_at'])
        member = User.objects.create_user('summary-member', 'summary-member@example.com', 'SecureMember938!')
        self.client.force_authenticate(member)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_the_admin_api_lists_counts_and_approves_in_bulk(self):
        self.item(self.dealer, '10', 'A-1', 'KAIYI', 'PIEZA A', reported_quantity=3)
        self.item(self.dealer, '11', 'B-1', 'KAIYI', 'PIEZA B')
        self.item(self.dealer, '12', 'C-1', 'WURTH', 'PIEZA C')
        proposals_stage()
        listing = self.client.get('/api/v1/management/catalog/proposals/').json()
        self.assertEqual(listing['count'], 3)
        first = listing['results'][0]
        self.assertEqual((first['sku'], first['stock'], first['oem'], first['items'][0]['supplier_name'], first['item_count']),
                         ('A-1', 3, {'code': 'A-1', 'brand': 'KAIYI'}, 'Motion Motors', 1))
        self.assertEqual(self.client.get('/api/v1/management/catalog/proposals/?has_oem=false').json()['count'], 1)
        self.assertEqual(self.client.get('/api/v1/management/catalog/proposals/?search=motion').json()['count'], 3)
        rows = [{'id': row['id'], 'version': row['version']} for row in listing['results'] if row['sku'] in ('A-1', 'B-1')]
        result = self.client.post('/api/v1/management/catalog/proposals/bulk/', {'action': 'approve', 'proposals': rows + [{'id': rows[0]['id'], 'version': 1}]}, format='json').json()
        self.assertEqual((result['approved'], [entry['ok'] for entry in result['results']]), (2, [True, True, False]))
        summary = self.client.get('/api/v1/management/catalog/proposals/summary/').json()
        changed = summary.pop('pending_changed_at')
        self.assertEqual(summary, {'pending': 1, 'approved': 2, 'linked': 0, 'rejected': 0, 'resolved': 0})
        self.assertEqual(changed.replace('Z', '+00:00'), self.proposal('C-1').updated_at.isoformat())
        detail = self.client.get(f'/api/v1/management/catalog/proposals/{rows[0]["id"]}/').json()
        self.assertEqual((detail['status'], detail['part']['sku']), ('approved', 'A-1'))
        member = User.objects.create_user('staff', 'staff@example.com', 'SecureStaff938!', is_staff=True)
        client = APIClient()
        client.force_authenticate(member)
        self.assertEqual(client.get('/api/v1/management/catalog/proposals/').status_code, 403)

    def later(self, item, **fields):
        """A save after the last pass (updated_at is auto_now: move it past what the proposal saw)."""
        SupplierItem.objects.filter(pk=item.pk).update(updated_at=timezone.now() + timedelta(seconds=5), **fields)

    def test_a_code_the_catalog_has_under_another_spelling_is_refused(self):
        self.item(self.dealer, '10', '04465-02220', 'TOYOTA', 'PASTILLA')
        self.item(self.other, 'A7', '0446502220', '', 'PASTILLA DELANTERA')     # blank brand: its own proposal
        propose()
        rows = [{'id': str(proposal.pk), 'version': proposal.version} for proposal in SkuProposal.objects.order_by('sku')]
        result = self.client.post('/api/v1/management/catalog/proposals/bulk/', {'action': 'approve', 'proposals': rows}, format='json').json()
        self.assertEqual([entry['ok'] for entry in result['results']], [True, False])
        self.assertEqual(result['results'][1]['message'], '0446502220 ya está en el catálogo como SKU 04465-02220: usa «Vincular a SKU existente».')
        self.assertEqual(Part.objects.filter(sku__in=['04465-02220', '0446502220']).count(), 1)
        # An OEM number another SKU carries as an alterno: the aftermarket item belongs there.
        reference = OEMReference.objects.create(manufacturer='TOYOTA', code='1610039315', printed_forms=['16100-39315'])
        holder = Part.objects.create(sku='BOMBA-1', name='BOMBA-1')
        PartCode.objects.create(part=holder, code='16100-39315', brand='TOYOTA', ref_type='oem')
        OEMCrossReference.objects.create(reference=reference, brand='GMB', code='GWT-41A', number='GWT41A')
        self.item(self.dealer, '20', 'GWT-41A', 'GMB', 'BOMBA DE AGUA')
        propose()
        proposal = self.proposal('GWT-41A')
        response = self.client.post(f'/api/v1/management/catalog/proposals/{proposal.pk}/', {'action': 'approve', 'expected_version': proposal.version}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('BOMBA-1', response.json()['detail'])
        self.assertEqual(response.json()['owners'], [{'id': str(holder.pk), 'sku': 'BOMBA-1', 'active': True}])   # the SKU its items belong to
        detail = self.client.get(f'/api/v1/management/catalog/proposals/{proposal.pk}/').json()
        self.assertEqual(detail['catalog_match'], [{'id': str(holder.pk), 'sku': 'BOMBA-1', 'code': '16100-39315', 'active': True}])
        holder.active = False
        holder.save(update_fields=['active'])
        retired = self.client.post(f'/api/v1/management/catalog/proposals/{proposal.pk}/', {'action': 'approve', 'expected_version': proposal.version}, format='json')
        self.assertEqual(retired.json()['detail'], '16100-39315 ya está en el catálogo como SKU BOMBA-1 (retirado): reactívalo para vincular ahí sus artículos.')

    def test_an_item_that_gains_candidate_skus_leaves_its_proposal(self):
        first = self.item(self.dealer, '10', '12345678', 'DENSO', 'BUJIA')
        second = self.item(self.other, 'A7', '12-345-678', 'DENSO', 'BUJIA')
        propose()
        proposal = SkuProposal.objects.get(key='12345678|DENSO')
        self.assertEqual((proposal.sku, proposal.items.count()), ('12-345-678', 2))   # the printed form with its separators
        MatchingCase.objects.filter(key=f'supplier:{second.pk}').update(status='review')   # matching found a candidate since
        with self.assertRaises(ProposalConflict):                                          # before the next pass: approving refuses
            approve(proposal.pk, self.root, proposal.version)
        propose()
        self.assertEqual(list(proposal.items.values_list('item_id', flat=True)), [first.pk])
        MatchingCase.objects.filter(key=f'supplier:{first.pk}').update(status='review')
        propose()
        self.assertEqual(SkuProposal.objects.get(pk=proposal.pk).status, 'resolved')

    def test_a_stock_change_only_marks_the_item_seen(self):
        item = self.item(self.dealer, '10', 'A-1', 'ACME', 'PIEZA A')
        other = self.item(self.dealer, '11', 'B-1', 'ACME', 'PIEZA B')
        propose()
        reject(self.proposal('B-1').pk, self.root, self.proposal('B-1').version, 'not_a_part')
        before = {sku: self.proposal(sku).version for sku in ('A-1', 'B-1')}
        self.later(item, reported_quantity=9)
        self.later(other, reported_quantity=2)
        stats = propose()
        self.assertEqual((stats['seen'], stats.get('refreshed', 0)), (2, 0))
        self.assertEqual({sku: self.proposal(sku).version for sku in ('A-1', 'B-1')}, before)
        item.refresh_from_db()
        self.assertEqual(SkuProposalItem.objects.get(item=item).seen_at, item.updated_at)   # the item as read, not the time of the write
        self.assertEqual(propose().get('seen', 0), 0)                                        # and it is not read again

    def test_a_product_linked_before_comes_back_with_the_sku_it_went_to(self):
        self.item(self.dealer, '10', 'GWT-41A', 'GMB', 'BOMBA DE AGUA')
        propose()
        target = Part.objects.create(sku='16100-39315', name='BOMBA')
        link_existing(self.proposal('GWT-41A').pk, self.root, self.proposal('GWT-41A').version, target.pk)
        self.item(self.other, 'A7', 'GWT41A', 'GMB', 'BOMBA DE AGUA GMB')   # another supplier starts carrying it
        self.assertEqual(propose()['created'], 1)
        fresh = SkuProposal.objects.get(status='pending')
        self.assertEqual(fresh.key, SkuProposal.objects.get(status='linked').key)
        detail = self.client.get(f'/api/v1/management/catalog/proposals/{fresh.pk}/').json()
        self.assertEqual(detail['earlier'], {'id': str(target.pk), 'sku': '16100-39315'})

    def test_make_abbreviations_agree_and_placeholders_stay_per_supplier(self):
        makes = vehicle_makes()
        self.assertEqual((makes['TOY'], makes['VW'], makes['AUDI'], makes['GMC'], makes['KAIYI']), ('TOYOTA', 'VOLKSWAGEN', 'AUDI', 'GMC', 'KAIYI'))
        self.assertNotIn('TOYO', makes)                                       # the tire brand
        self.item(self.dealer, '10', '90915-YZZE1', 'TOY', 'FILTRO')
        self.item(self.other, 'A7', '90915YZZE1', 'TOYOTA', 'FILTRO DE ACEITE')
        self.item(self.dealer, '11', '12345678', 'GENERICO', 'TORNILLO')
        self.item(self.other, 'A8', '12345678', 'GENERICO', 'ARANDELA')
        self.item(self.dealer, '12', '205/55R16', 'TOYO', 'LLANTA')
        propose()
        toyota = SkuProposal.objects.get(key='90915YZZE1|TOYOTA')
        self.assertEqual((toyota.items.count(), toyota.oem_brand), (2, 'TOYOTA'))
        self.assertEqual(SkuProposal.objects.filter(sku='12345678').count(), 2)
        self.assertEqual(self.proposal('205/55R16').oem_code, '')

    def test_the_suppliers_code_stays_an_alterno_whatever_the_sku_became(self):
        self.item(self.dealer, '10', 'GWT-41A', 'GMB', 'BOMBA DE AGUA', references=[{'brand': 'TOYOTA', 'code': '16100-39315', 'ref_type': 'oem'}, {'code': 'BAD' * 50}])
        self.item(self.other, 'A7', 'GWT41A', 'GMB', 'BOMBA', references=[{'brand': 'TOYOTA', 'code': '16100-39315'}])
        propose()
        proposal = self.proposal('GWT-41A')
        self.assertEqual(proposal.references, [{'brand': 'TOYOTA', 'code': '16100-39315', 'ref_type': 'oem'}])   # typed wins, malformed skipped
        edit(proposal.pk, self.root, proposal.version, {'sku': 'BOMBA-GWT', 'description': 'bomba de agua'})       # description unchanged
        proposal.refresh_from_db()
        self.assertEqual((proposal.field_sources['sku']['source'], proposal.field_sources['description']['source']), ('admin', 'supplier'))
        version = proposal.version
        edit(proposal.pk, self.root, version, {'sku': 'BOMBA-GWT', 'references': proposal.references})            # nothing changed
        self.assertEqual(self.proposal('BOMBA-GWT').version, version)
        approve(proposal.pk, self.root, version)
        part = SkuProposal.objects.get(pk=proposal.pk).part
        self.assertTrue(PartCode.objects.filter(part=part, code='GWT-41A', brand='GMB', ref_type='company').exists())
        self.assertFalse(PartCode.objects.filter(code='BOMBA-GWT', brand='GMB').exists())

    def test_a_large_backlog_is_read_in_batches_and_asks_for_another_pass(self):
        for number in range(3):
            self.item(self.dealer, str(number), f'CODE-{number}000', 'ACME', 'PIEZA')
        self.assertEqual(propose(batch=1)['created'], 3)
        for number in range(3, 6):
            self.item(self.dealer, str(number), f'CODE-{number}000', 'ACME', 'PIEZA')
        MatchingQueue.objects.get_or_create(pk=1)
        requested = MatchingQueue.objects.get(pk=1).requested
        stats = propose(batch=1, seconds=0)
        self.assertEqual((stats['created'], stats['continues']), (1, 1))
        self.assertEqual(MatchingQueue.objects.get(pk=1).requested, requested + 1)

    def test_an_item_whose_code_changed_moves_and_its_old_proposal_follows(self):
        first = self.item(self.dealer, '10', '12345678', 'DENSO', 'BUJIA')
        self.item(self.other, 'A7', '12-345-678', 'DENSO', 'BUJIA IRIDIUM LARGA')
        propose()
        proposal = SkuProposal.objects.get(key='12345678|DENSO')
        self.assertEqual(proposal.items.count(), 2)
        self.later(first, codigo='87654321')
        propose()
        moved = self.proposal('87654321')
        self.assertEqual(list(moved.items.values_list('item_id', flat=True)), [first.pk])
        proposal.refresh_from_db()
        self.assertEqual((proposal.items.count(), proposal.sku, proposal.status), (1, '12-345-678', 'pending'))
