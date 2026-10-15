"""Productos nuevos' API (mall.sku_proposal_views) on lines in preparación (inventory refactor S6.2): the routes and JSON the screens
read before S6, now from mall.new_products' lines."""
import re
from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from .brand_models import Brand
from . import new_products
from .matching_engine import item_row
from .matching_models import MatchingCase
from .models import Account, Part, PartType, Reference, Role, SupplierItem, User
from .technical_data import part_type_for
from .reference_matching import backlog
from .sku_proposals import vehicle_makes
from .test_factories import make_legacy_case, make_line, move_variant

OLD_KEYS = {'id', 'status', 'status_label', 'version', 'sku', 'description', 'brand', 'oem', 'part_type', 'references', 'field_sources',
            'internal_code', 'ai_review', 'item_count', 'supplier_count', 'stock', 'items', 'part', 'decided_by', 'decided_at',
            'reject_reason', 'reject_reason_label', 'reject_note', 'created_at', 'updated_at', 'earlier'}


class ProductosNuevosApiTests(APITestCase):
    def setUp(self):
        Brand.objects.get_or_create(nombre='KAIYI', defaults={'tipo': 'vehiculo'})  # a vehicle make (mall.sku_proposals.vehicle_makes)
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
            make_legacy_case(item, case)
        return item

    def line(self, sku):
        return Part.objects.get(sku=sku)

    def url(self, line=None, rest=''):
        return f'/api/v1/management/catalog/proposals/{f"{line.pk}/" if line is not None else ""}{rest}'

    def test_the_admin_api_lists_counts_and_approves_in_bulk(self):
        self.item(self.dealer, '10', 'A-1', 'KAIYI', 'PIEZA A', reported_quantity=3)
        self.item(self.dealer, '11', 'B-1', 'KAIYI', 'PIEZA B')
        self.item(self.dealer, '12', 'C-1', 'WURTH', 'PIEZA C')
        self.assertEqual(backlog()['placed'], 3)   # the matching worker's backlog: rule 4 for the items no line takes
        listing = self.client.get(self.url()).json()
        self.assertEqual(listing['count'], 3)
        first = listing['results'][0]
        self.assertEqual(OLD_KEYS - set(first), set())
        self.assertEqual((first['sku'], first['stock'], first['oem'], first['items'][0]['supplier_name'], first['item_count'], first['status'],
                          first['status_label'], first['estado'], first['internal_code'], first['earlier']),
                         ('A-1', 3, {'code': 'A-1', 'brand': 'KAIYI'}, 'Motion Motors', 1, 'pending', 'Por revisar', 'preparacion', False, None))
        self.assertEqual(first['id'], str(self.line('A-1').pk))
        self.assertEqual(self.client.get(self.url() + '?has_oem=false').json()['count'], 1)
        self.assertEqual(self.client.get(self.url() + '?search=motion').json()['count'], 3)
        self.assertEqual(self.client.get(self.url() + '?search=c-1').json()['count'], 1)
        self.assertEqual(self.client.get(self.url() + '?brand=wurth').json()['count'], 1)
        self.assertEqual(self.client.get(self.url() + f'?supplier={self.other.pk}').json()['count'], 0)
        self.assertEqual([row['sku'] for row in self.client.get(self.url() + '?ordering=sku').json()['results']], ['A-1', 'B-1', 'C-1'])
        self.assertEqual(self.client.get(self.url() + '?status=maybe').status_code, 400)
        rows = [{'id': row['id'], 'version': row['version']} for row in listing['results'] if row['sku'] in ('A-1', 'B-1')]
        result = self.client.post(self.url(rest='bulk/'), {'action': 'approve', 'proposals': rows + [{'id': rows[0]['id'], 'version': 1}]},
                                  format='json').json()
        self.assertEqual((result['approved'], [entry['ok'] for entry in result['results']], result['results'][0]['sku']), (2, [True, True, False], 'A-1'))
        summary = self.client.get(self.url(rest='summary/')).json()
        changed = summary.pop('pending_changed_at')
        self.assertEqual(summary, {'pending': 1, 'approved': 2, 'linked': 0, 'rejected': 0, 'resolved': 0})
        self.assertEqual(changed.replace('Z', '+00:00'), self.line('C-1').changed_at.isoformat())
        detail = self.client.get(self.url(self.line('A-1'))).json()
        self.assertEqual((detail['status'], detail['part']['sku'], detail['decided_by'], detail['ai_review']), ('approved', 'A-1', 'root', None))
        self.assertEqual(self.client.get(self.url() + '?status=approved').json()['count'], 2)
        self.assertEqual(self.client.get(self.url() + '?status=all').json()['count'], 3)
        member = User.objects.create_user('staff', 'staff@example.com', 'SecureStaff938!', is_staff=True)
        client = APIClient()
        client.force_authenticate(member)
        self.assertEqual(client.get(self.url()).status_code, 403)

    def test_conflicts_and_duplicates_save_nothing(self):
        self.item(self.dealer, '10', 'A-1', 'KAIYI', 'PIEZA A')
        backlog()
        line = self.line('A-1')
        response = self.client.post(self.url(line), {'action': 'approve', 'expected_version': line.version + 1}, format='json')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['proposal']['version'], line.version)
        holder = make_line(sku='EC-01', name='EC-01')
        self.item(self.dealer, '11', 'EC01', 'WURTH', 'ARANDELA')   # another spelling of a catalog code: a new line, refused on approval
        backlog()
        clash = self.line('EC01')
        response = self.client.post(self.url(clash), {'action': 'approve', 'expected_version': clash.version}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'EC01 ya está en el catálogo como SKU EC-01: usa «Vincular a SKU existente».',
                                           'owners': [{'id': str(holder.pk), 'sku': 'EC-01', 'active': True}]})
        detail = self.client.get(self.url(clash)).json()
        self.assertEqual((detail['catalog_match'], detail['status']), ([{'id': str(holder.pk), 'sku': 'EC-01', 'code': 'EC01', 'active': True}], 'pending'))
        holder.estado = 'retirada'
        holder.save(update_fields=['estado'])
        retired = self.client.post(self.url(clash), {'action': 'approve', 'expected_version': clash.version}, format='json')
        self.assertEqual(retired.json()['detail'], 'EC01 ya está en el catálogo como SKU EC-01 (retirado): reactívalo para vincular ahí sus artículos.')

    def test_link_to_an_existing_sku_or_reject(self):
        item = self.item(self.dealer, '10', 'A-1', 'ACME', 'PIEZA A')
        self.item(self.dealer, '11', 'B-1', 'ACME', 'PIEZA B')
        backlog()
        target = make_line(sku='MASTER-1', name='MASTER-1')
        line = self.line('A-1')
        response = self.client.post(self.url(line), {'action': 'link', 'expected_version': line.version}, format='json')
        self.assertEqual(response.status_code, 400)                                 # which SKU
        response = self.client.post(self.url(line), {'action': 'link', 'expected_version': line.version, 'part': str(target.pk)}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response.json()['status'], response.json()['part']), ('linked', {'id': str(target.pk), 'sku': 'MASTER-1'}))
        item.refresh_from_db()
        self.assertEqual((item.part, item.matching_status), (target, 'matched'))
        rejected = self.line('B-1')
        response = self.client.post(self.url(rejected), {'action': 'reject', 'expected_version': rejected.version, 'reason': 'not_a_part',
                                                          'note': 'Es un servicio'}, format='json')
        self.assertEqual((response.json()['status'], response.json()['reject_reason'], response.json()['reject_reason_label'],
                          response.json()['reject_note']), ('rejected', 'not_a_part', 'No es un repuesto', 'Es un servicio'))
        self.item(self.other, 'A7', 'B-1', 'ACME', 'PIEZA B 12V')                  # the same code comes back: no notice (OD13)
        backlog()
        self.assertEqual(self.client.get(self.url(rejected)).json()['status'], 'rejected')
        self.assertEqual(SupplierItem.objects.filter(part=rejected).count(), 2)
        summary = self.client.get(self.url(rest='summary/')).json()
        self.assertEqual((summary['linked'], summary['rejected'], summary['pending']), (1, 1, 0))

    def test_an_administrators_edits_through_the_api(self):
        self.item(self.dealer, '10', 'A-1', 'ACME', 'pieza a', references=[{'code': '16100-39315', 'brand': 'TOYOTA'}])
        backlog()
        line = self.line('A-1')
        part_type = part_type_for('FRENOS', 'PASTILLAS')
        self.assertEqual(self.client.patch(self.url(line), {'description': 'X'}, format='json').status_code, 400)   # the version it edits
        self.assertEqual(self.client.patch(self.url(line), {'expected_version': line.version, 'name': 'X'}, format='json').status_code, 400)
        response = self.client.patch(self.url(line), {'expected_version': line.version, 'description': 'Pastilla delantera',
                                                      'part_type': str(part_type.pk), 'oem_code': '04465-0K240', 'oem_brand': 'toyota'}, format='json')
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()
        self.assertEqual((data['description'], data['part_type']['name'], data['oem'], data['field_sources']['description']['source']),
                         ('PASTILLA DELANTERA', 'PASTILLAS', {'code': '04465-0K240', 'brand': 'TOYOTA'}, 'admin'))
        declared = data['declared_references'][0]   # what its item declares waits in a case (rule 5b)
        self.assertEqual((re.sub('[^A-Z0-9]', '', declared['code']), declared['brand']), ('1610039315', 'TOYOTA'))
        stale = self.client.patch(self.url(line), {'expected_version': line.version, 'sku': 'OTRO'}, format='json')
        self.assertEqual((stale.status_code, stale.json()['proposal']['version']), (409, data['version']))
        move_variant(SupplierItem.objects.get(supplier_invent_id='10'), make_line(sku='ELSEWHERE', name='ELSEWHERE'),
                     matching_status='matched')
        self.assertEqual(self.client.get(self.url(line)).json()['status'], 'resolved')   # its items left: no decision waits
        self.assertEqual(self.client.get(self.url(rest='summary/')).json()['resolved'], 1)

    def test_the_summary_dates_the_newest_pending_change_for_the_storefront_reminder(self):
        url = self.url(rest='summary/')
        self.assertIsNone(self.client.get(url).json()['pending_changed_at'])
        self.item(self.dealer, '10', 'A-1', 'KAIYI', 'PIEZA A')
        self.item(self.dealer, '11', 'B-1', 'KAIYI', 'PIEZA B')
        backlog()
        first, second = self.line('A-1'), self.line('B-1')
        Part.objects.filter(pk=first.pk).update(changed_at=timezone.now() - timedelta(hours=2))
        Part.objects.filter(pk=second.pk).update(changed_at=timezone.now() - timedelta(hours=1))
        second.refresh_from_db()
        self.assertEqual(self.client.get(url).json()['pending_changed_at'].replace('Z', '+00:00'), second.changed_at.isoformat())
        # Reviewing the newest leaves the older one's date: no new arrival, so a closed reminder stays closed.
        new_products.reject(second.pk, self.root, second.version, 'duplicate', '')
        first.refresh_from_db()
        self.assertEqual(self.client.get(url).json()['pending_changed_at'].replace('Z', '+00:00'), first.changed_at.isoformat())
        new_products.reject(first.pk, self.root, first.version, 'duplicate', '')
        self.assertIsNone(self.client.get(url).json()['pending_changed_at'])
        member = User.objects.create_user('summary-member', 'summary-member@example.com', 'SecureMember938!')
        self.client.force_authenticate(member)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_a_dealers_code_is_its_makes_oem_number_and_an_internal_code_is_the_suppliers(self):
        self.assertIn('KAIYI', vehicle_makes())
        genuine = self.item(self.dealer, '10', '025CHA-1502606', 'KAIYI', 'O-RING KAIYI GS11')
        self.item(self.other, 'A7', '025CHA 1502606', 'kaiyi', 'O-RING KAIYI')       # another supplier, same product
        self.item(self.dealer, '12', 'URT-22', 'MOTION MOTORS', 'ESCOBILLA')            # the supplier's own name: its own code (OD20)
        backlog()
        genuine.refresh_from_db()
        kaiyi = self.client.get(self.url(genuine.part)).json()   # the first item placed gives the codigo its spelling
        self.assertEqual((kaiyi['oem'], kaiyi['item_count'], kaiyi['supplier_count'], kaiyi['internal_code'], kaiyi['description']),
                         ({'code': genuine.part.sku, 'brand': 'KAIYI'}, 2, 2, False, 'O-RING KAIYI GS11'))
        own = self.client.get(self.url(self.line('URT-22'))).json()
        self.assertEqual((own['internal_code'], own['oem'], own['brand']), (True, None, 'MOTION MOTORS'))
        self.assertEqual(MatchingCase.objects.filter(evidence__s10__kind='supplier', status='dismissed').count(), 3)

    def test_bulk_rejects_and_links_one_by_one(self):
        target = make_line(sku='OLD-1', name='OLD-1')
        self.item(self.dealer, '1', 'R-1', 'ACME', 'NADA')
        self.item(self.dealer, '2', 'L-1', 'ACME', 'PIEZA')
        backlog()
        reject, link = self.line('R-1'), self.line('L-1')
        bulk = self.url(rest='bulk/')
        done = self.client.post(bulk, {'action': 'reject', 'proposals': [{'id': str(reject.pk), 'version': reject.version, 'reason': 'not_a_part'},
                                                                         {'id': str(reject.pk), 'version': 1, 'reason': 'other'}, 'x']}, format='json').json()
        self.assertEqual([row['ok'] for row in done['results']], [True, False, False])
        done = self.client.post(bulk, {'action': 'link', 'proposals': [{'id': str(link.pk), 'version': link.version, 'part': str(target.pk)},
                                                                       {'id': str(link.pk), 'version': link.version, 'part': 'nope'}]}, format='json').json()
        self.assertEqual(([row['ok'] for row in done['results']], done['results'][0]['sku']), ([True, False], 'OLD-1'))
        self.assertEqual(self.client.post(bulk, {'action': 'delete', 'proposals': [{'id': str(link.pk), 'version': 1}]}, format='json').status_code, 400)
        self.assertTrue(Reference.objects.filter(part=target, code='L-1').exists())
