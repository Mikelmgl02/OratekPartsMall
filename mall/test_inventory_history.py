import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace

from django.apps import apps
from django.utils import timezone
from django.test import override_settings
from rest_framework.test import APITestCase

from . import test_apiag_sync as apiag_tests
from . import test_stock_reservations as reservation_tests
from . import test_supplier_import as import_tests
from .apiag_models import ApiagConnection
from .apiag_sync_models import ApiagSync
from .apiag_tokens import encrypt
from .inventory_history import entry_json
from .inventory_history_models import InventoryChange, InventoryRun
from .models import InventoryUpdate, StockEntry, SupplierItem, User
from .services import ingest_inventory

def borrow(source, *names):
    """Methods of another test class (never the class itself at module level: the loader would run its tests here too)."""
    return {name: getattr(source, name) for name in names}


class QuotationLedgerTests(APITestCase):
    """A confirmed quotation moves the supplier's stock through its reservations; the ledger shows each movement with its order."""
    locals().update(borrow(reservation_tests.StockReservationTests, 'setUp', 'account', 'item', 'url', 'payload', 'submit', 'stock_snapshot', 'order',
                           'review', 'action', 'drafted', 'quote', 'headers', 'file', 'handshake', 'counts', 'upload', 'job_url', 'commit', 'sent'))

    def ledger(self, query='', management=False):
        base = f'/api/v1/management/accounts/{self.supplier_a.pk}/ledger/' if management else f'/api/v1/accounts/{self.supplier_a.pk}/ledger/'
        return self.client.get(f'{base}{query}')

    def test_confirmed_quotations_and_the_excel_load_that_discounts_them_fill_the_ledger(self):
        order = self.handshake()
        job = self.upload(a1=7, a2=10)
        decisions = {order['id']: 'net' for entry in job['reservations'] for order in entry['orders']}
        self.assertEqual(self.commit(job, decisions).status_code, 200)
        self.client.force_authenticate(self.seller_a)
        rows = self.ledger().json()['results']
        reserve = next(row for row in rows if row['kind'] == 'reserve' and row['item']['supplier_invent_id'] == 'A-001')
        self.assertEqual((reserve['origin'], reserve['order']['reference'], reserve['order']['revision'], reserve['reserved_delta'], reserve['available_delta']),
                         ('quotation', order.reference, 1, 3, -3))
        self.assertEqual(reserve['order']['client_name'], order.client_name)
        settle = next(row for row in rows if row['kind'] == 'settle' and row['item']['supplier_invent_id'] == 'A-001')
        self.assertEqual((settle['origin'], settle['reserved_delta'], settle['stock_delta']), ('quotation', -3, 0))
        load = next(row for row in rows if row['kind'] == 'sync' and row['item']['supplier_invent_id'] == 'A-001')
        self.assertEqual((load['origin'], load['run']['label']), ('excel', f'excel-{job["job_id"]}'))
        quotation_rows = self.ledger('?origin=quotation').json()['results']
        self.assertTrue(quotation_rows and all(row['origin'] == 'quotation' for row in quotation_rows))
        self.assertEqual({row['kind'] for row in self.ledger('?kind=reserve').json()['results']}, {'reserve'})
        self.assertEqual(self.ledger(f'?search={order.reference}').json()['count'], len(quotation_rows))
        runs = self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/ledger/runs/').json()['results']
        self.assertEqual([(run['origin'], run['label']) for run in runs], [('excel', f'excel-{job["job_id"]}')])
        changes = self.client.get(f'/api/v1/accounts/{self.supplier_a.pk}/ledger/runs/{runs[0]["id"]}/changes/').json()['results']
        self.assertTrue(all(change['quantity_after'] is not None for change in changes))
        self.assertEqual(self.ledger('', management=True).status_code, 403)   # a supplier member is not a superuser
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.ledger().status_code, 404)                       # not a member of the supplier
        # Each movement names its order's state as both parties see it: Recibido once the client confirms.
        quotation = str(order.quotations.get().pk)
        self.client.force_authenticate(self.seller_a)
        for step in ('process', 'deliver'):
            self.assertEqual(self.action(order, step, account=self.supplier_a, quotation_id=quotation).status_code, 200)
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'receive', account=self.client_account, quotation_id=quotation).status_code, 200)
        self.client.force_authenticate(self.seller_a)
        reserve = next(row for row in self.ledger('?kind=reserve').json()['results'] if row['item']['supplier_invent_id'] == 'A-001')
        self.assertEqual((reserve['order']['status'], reserve['order']['status_label']), ('received', 'Recibido'))
        self.client.force_authenticate(User.objects.create_superuser('ledger-root', 'ledger-root@example.invalid', 'Example938!'))
        self.assertEqual(self.ledger('', management=True).json()['count'], len(rows))


    def test_movements_name_the_client_not_its_buyer_and_superusers_see_usernames(self):
        order, quotation = self.sent()
        self.client.force_authenticate(self.buyer)
        self.assertEqual(self.action(order, 'reject', quotation_id=quotation['id'], reason_code='not_needed', reason='').status_code, 200)
        self.client.force_authenticate(self.seller_a)
        reserve, release = self.ledger('?kind=reserve').json()['results'][0], self.ledger('?kind=release').json()['results'][0]
        self.assertEqual(reserve['actor'], self.seller_a.username)  # the member who sent the quotation, which holds its units
        self.assertEqual(release['actor'], release['order']['client_name'])  # the buyer who rejected it, as their client account
        self.client.force_authenticate(User.objects.create_superuser('ledger-root2', 'ledger-root2@example.invalid', 'Example938!'))
        self.assertEqual(self.ledger('?kind=release', management=True).json()['results'][0]['actor'], self.buyer.username)

    def test_date_and_item_filters_and_their_errors(self):
        self.handshake()
        entries = list(StockEntry.objects.filter(supplier=self.supplier_a).order_by('pk'))
        old = timezone.make_aware(datetime(2026, 9, 1, 23, 30))
        StockEntry.objects.filter(pk=entries[0].pk).update(created_at=old)
        self.client.force_authenticate(self.seller_a)
        self.assertEqual(self.ledger('?before=2026-09-01').json()['count'], 1)                 # the whole local day, inclusive
        self.assertEqual(self.ledger('?after=2026-09-01&before=2026-09-01').json()['count'], 1)
        self.assertEqual(self.ledger('?after=2026-09-02').json()['count'], len(entries) - 1)
        self.assertEqual(self.ledger('?after=2026-09-02&before=2026-09-01').json()['count'], 0)
        self.assertEqual(self.ledger(f'?item={entries[0].item_id}').json()['count'], StockEntry.objects.filter(item_id=entries[0].item_id).count())
        for query, key in (('?item=zzz', 'item'), (f'?item={"-" * 36}', 'item'), ('?after=2026-02-30', 'after'), ('?before=nope', 'before'),
                           ('?origin=x', 'origin'), ('?kind=x', 'kind')):
            response = self.ledger(query)
            self.assertEqual(response.status_code, 400, query)
            self.assertIn(key, response.json())

    def test_runs_and_management_stay_within_the_supplier(self):
        other = InventoryRun.objects.create(supplier=self.supplier_b, origin='manual', label='manual-b')
        InventoryChange.objects.create(run=other, item=self.item_b, action='stock', quantity_before=20, quantity_after=19)
        base = f'/api/v1/accounts/{self.supplier_a.pk}/ledger/'
        self.client.force_authenticate(self.seller_a)
        for path in (f'{base}runs/{other.pk}/', f'{base}runs/{other.pk}/changes/'):
            self.assertEqual(self.client.get(path).status_code, 404)  # another supplier's run through this supplier's ledger
        self.assertEqual(self.client.get(f'{base}runs/').json()['count'], 0)
        self.client.force_authenticate(User.objects.create_superuser('ledger-root3', 'ledger-root3@example.invalid', 'Example938!'))
        self.assertEqual(self.client.get(f'/api/v1/management/accounts/{self.client_account.pk}/ledger/').status_code, 403)  # not a supplier
        self.assertEqual(self.client.get(f'/api/v1/management/accounts/{self.supplier_a.pk}/ledger/runs/{other.pk}/').status_code, 404)


class AvailableDeltaTests(APITestCase):
    """Available never goes below 0, so a movement's Disponibles is the change of the clamped balance."""

    def row(self, **fields):
        item = SimpleNamespace(pk='i', supplier_invent_id='A', codigo='A', brand='', description='', source='upload')
        entry = SimpleNamespace(pk=1, created_at=None, origin='manual', item=item, actor_id=None, reservation_id=None, reference='r', **fields)
        return entry_json(entry, {})

    def test_shortfalls_and_counts_below_the_reserved_units_move_what_really_became_available(self):
        partial = self.row(kind='shortfall', quantity=1, direction='debit', balance_type='stock', reserved_delta=3, reserved_direction='debit', reported_after=0, reserved_after=0)
        self.assertEqual((partial['stock_delta'], partial['reserved_delta'], partial['available_delta']), (-1, -3, 0))
        reserved_only = self.row(kind='shortfall', quantity=2, direction='debit', balance_type='reservation', reserved_delta=2, reserved_direction='debit', reported_after=0, reserved_after=1)
        self.assertEqual(reserved_only['available_delta'], 0)
        below = self.row(kind='sync', quantity=3, direction='debit', balance_type='stock', reserved_delta=0, reserved_direction='', reported_after=2, reserved_after=3)
        self.assertEqual((below['available_delta'], below['available_after']), (-2, 0))  # 5 - 3 = 2 available became 0
        settle = self.row(kind='settle', quantity=3, direction='debit', balance_type='reservation', reserved_delta=3, reserved_direction='debit', reported_after=2, reserved_after=0)
        self.assertEqual(settle['available_delta'], 2)


class ExcelLedgerTests(APITestCase):
    """An Excel import records what it created, the fields it changed (before and after) and the units it moved. An account synced
    with apiag-cloud cannot import Excel."""
    locals().update(borrow(import_tests.SupplierExcelImportTests, 'setUp', 'seed', 'base_url', 'file', 'preview', 'job_url', 'commit', 'headers'))

    def test_the_run_keeps_created_items_changed_fields_and_moved_units(self):
        staged = self.preview([['STABLE-5', '58411-1R000-G', 'KSM', 'TAMBOR TRASERO', 8], ['NEW-1', 'XYZ-1', 'ACME', 'NUEVO', 2]])
        self.assertEqual(staged.status_code, 200, staged.data)
        self.assertEqual(self.commit(staged.data['job_id']).status_code, 200)
        run = InventoryRun.objects.get(origin='excel')
        self.assertEqual((run.label, run.filename, run.actor, run.finished_at is not None), (f'excel-{staged.data["job_id"]}', 'EXISTENCIAS.xlsx', self.user, True))
        self.assertEqual({key: run.counts[key] for key in ('created', 'updated', 'credit_units', 'debit_units')},
                         {'created': 1, 'updated': 1, 'credit_units': 5, 'debit_units': 0})
        changes = {row['item']['supplier_invent_id']: row for row in
                   self.client.get(f'/api/v1/accounts/{self.account.pk}/ledger/runs/{run.pk}/changes/').json()['results']}
        stable = changes['STABLE-5']
        self.assertEqual((stable['action'], stable['quantity_before'], stable['quantity_after'], stable['delta']), ('updated', 5, 8, 3))
        self.assertEqual(stable['fields'], [{'field': 'description', 'label': 'Descripción', 'before': 'TAMBOR', 'after': 'TAMBOR TRASERO'}])
        self.assertEqual((changes['NEW-1']['action'], changes['NEW-1']['quantity_after']), ('created', 2))
        unchanged = self.preview([['STABLE-5', '58411-1R000-G', 'KSM', 'TAMBOR TRASERO', 8]])
        self.commit(unchanged.data['job_id'])
        self.assertEqual(InventoryRun.objects.count(), 1)  # a load that changes nothing leaves no run

    def test_an_account_synced_with_apiag_cloud_cannot_import_excel_nor_update_by_hand(self):
        staged = self.preview([['STABLE-5', '58411-1R000-G', 'KSM', 'TAMBOR', 9]])
        ApiagConnection.objects.create(account=self.account, server_url='https://mm.oratek.info', token_ciphertext=encrypt('apiagTok3n-For-MotionPartes-0001'),
                                       token_hint='0001')
        for response in (self.preview([['STABLE-5', '58411-1R000-G', 'KSM', 'TAMBOR', 9]]), self.commit(staged.data['job_id']),
                         self.client.get(f'{self.base_url()}template/')):
            self.assertEqual(response.status_code, 400)
            self.assertIn('se sincroniza con apiag-cloud', str(response.data))
        manual = self.client.post(f'/api/v1/accounts/{self.account.pk}/inventory/ingest/', {
            'update_id': 'manual-9', 'supplier_invent_id': 'STABLE-5', 'codigo': '58411-1R000-G', 'brand': 'KSM', 'source': 'upload', 'quantity': 1}, format='json')
        self.assertEqual(manual.status_code, 400)  # nor a manual update: its stock comes only from the pull
        self.assertIn('no se actualizan a mano', str(manual.data))
        self.assertEqual(SupplierItem.objects.get(supplier=self.account, supplier_invent_id='STABLE-5').reported_quantity, 5)
        listing = self.client.get('/api/v1/accounts/').json()
        accounts = {row['id']: row for row in (listing['results'] if isinstance(listing, dict) else listing)}
        self.assertEqual((accounts[str(self.account.pk)]['apiag_connected'], accounts[str(self.second_account.pk)]['apiag_connected']), (True, False))

    def test_a_manual_update_is_its_own_run(self):
        response = self.client.post(f'/api/v1/accounts/{self.account.pk}/inventory/ingest/', {
            'update_id': 'manual-42', 'supplier_invent_id': 'STABLE-5', 'codigo': '58411-1R000-G', 'brand': 'KSM', 'description': 'TAMBOR',
            'source': 'upload', 'quantity': 2}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        run = InventoryRun.objects.get(origin='manual')
        change = run.changes.get()
        self.assertEqual((run.label, change.action, change.quantity_before, change.quantity_after, run.counts['debit_units']), ('manual-42', 'stock', 5, 2, 3))
        self.assertEqual(change.stock_entry.reference, 'manual-42')
        finished = run.finished_at
        replay = self.client.post(f'/api/v1/accounts/{self.account.pk}/inventory/ingest/', {
            'update_id': 'manual-42', 'supplier_invent_id': 'STABLE-5', 'codigo': '58411-1R000-G', 'brand': 'KSM', 'description': 'TAMBOR',
            'source': 'upload', 'quantity': 2}, format='json')
        self.assertEqual(replay.status_code, 200)
        run.refresh_from_db()
        self.assertEqual(run.finished_at, finished)  # a replay leaves the run as it was
        posing = self.client.post(f'/api/v1/accounts/{self.account.pk}/inventory/ingest/', {
            'update_id': 'excel-0f100000-0000-4000-8000-000000000001-2', 'supplier_invent_id': 'STABLE-5', 'codigo': '58411-1R000-G', 'brand': 'KSM',
            'source': 'upload', 'quantity': 3}, format='json')
        self.assertEqual(posing.status_code, 400)
        self.assertIn('update_id', posing.json())


@override_settings(APIAG_TOKEN_KEYS=[apiag_tests.KEY], APIAG_SYNC_MINUTES=5, APIAG_SYNC_DAILY_AT='03:30')
class ApiagLedgerTests(APITestCase):
    """An apiag-cloud pull records what it created, the units it moved per sucursal (also when only the sucursales changed) and what
    it withdrew, with why. A pull that changes nothing leaves no run."""
    locals().update(borrow(apiag_tests.ApiagSyncTests, 'setUp', 'run_pull', 'item', 'sync_row'))

    def test_pulls_record_creations_branch_moves_and_withdrawals(self):
        self.apiag.publish(10, 'A-1', {1: 4, 2: 1})
        self.apiag.publish(11, 'B-2', {1: 2})
        self.run_pull()
        first = InventoryRun.objects.get(origin='apiag')
        created = first.changes.get(item=self.item(10))
        self.assertEqual((created.action, created.quantity_before, created.quantity_after, first.mode), ('created', 0, 5, 'full'))
        self.assertEqual(created.branches, [{'sucursal': str(created.branches[0]['sucursal']), 'nombre': 'DAVID', 'before': 0, 'after': 1},
                                            {'sucursal': str(created.branches[1]['sucursal']), 'nombre': 'MATRIZ', 'before': 0, 'after': 4}])
        self.run_pull()
        self.assertEqual(InventoryRun.objects.count(), 1)  # nothing changed: no run
        self.apiag.publish(11, 'B-2', {2: 2})  # moved between sucursales: same total
        self.apiag.retire(10)
        self.run_pull()
        latest = InventoryRun.objects.exclude(pk=first.pk).get()
        moved, retired = latest.changes.get(item=self.item(11)), latest.changes.get(item=self.item(10))
        self.assertEqual((moved.action, moved.quantity_before, [(row['nombre'], row['before'], row['after']) for row in moved.branches]),
                         ('stock', None, [('DAVID', 0, 2), ('MATRIZ', 2, 0)]))
        self.assertEqual((retired.action, retired.fields, retired.quantity_before, retired.quantity_after), ('withdrawn', {'reason': 'retired'}, 5, 0))
        self.assertEqual((latest.counts['withdrawn'], latest.counts['stock'], latest.counts['debit_units']), (1, 1, 5))
        self.client.force_authenticate(self.member)
        rows = self.client.get(f'/api/v1/accounts/{self.supplier.pk}/ledger/?origin=apiag').json()['results']
        self.assertTrue(rows and all(row['origin'] == 'apiag' and row['run'] for row in rows))
        changes = self.client.get(f'/api/v1/accounts/{self.supplier.pk}/ledger/runs/{latest.pk}/changes/?action=withdrawn').json()['results']
        self.assertEqual([(row['item']['codigo'], row['reason_label']) for row in changes], [('A-1', 'Retirado en apiag-cloud')])


    def test_unlisted_and_refused_items_are_withdrawn_with_why(self):
        self.apiag.publish(10, 'A-1', {1: 4, 2: 1})
        self.apiag.publish(11, 'B-2', {2: 3})
        self.run_pull()
        self.apiag.publish(10, '', {1: 4, 2: 1})   # its code was cleared: invalid data
        self.run_pull()
        refused = InventoryChange.objects.get(item=self.item(10), action='withdrawn')
        self.assertEqual((refused.fields, refused.quantity_before, refused.quantity_after), ({'reason': 'refused'}, 5, 0))
        self.assertEqual(sorted(row['nombre'] for row in refused.branches), ['DAVID', 'MATRIZ'])
        del self.apiag.items[11]                    # a full pull no longer lists it
        ApiagSync.objects.filter(connection=self.connection).update(since='')
        self.run_pull()
        unseen = InventoryChange.objects.get(item=self.item(11), action='withdrawn')
        self.assertEqual((unseen.fields, unseen.quantity_before, unseen.quantity_after, unseen.run.mode), ({'reason': 'unseen'}, 3, 0, 'full'))


class RebuildTests(APITestCase):
    """The ledger before it existed: runs rebuilt from the recorded updates, every entry with its supplier."""
    locals().update(borrow(import_tests.SupplierExcelImportTests, 'setUp', 'seed'))

    def test_recorded_loads_become_rebuilt_runs(self):
        ingest_inventory(supplier=self.account, actor=self.user, data={'update_id': 'excel-0f100000-0000-4000-8000-000000000001-2', 'supplier_invent_id': 'X-1',
                                                                      'codigo': 'X-1', 'brand': 'ACME', 'source': 'upload', 'quantity': 4})
        ingest_inventory(supplier=self.account, actor=self.user, data={'update_id': 'apiag-cloud 2026-10-11 02:30 #e72d · X-1', 'supplier_invent_id': 'X-1',
                                                                      'codigo': 'X-1', 'brand': 'ACME', 'source': 'upload', 'quantity': 1})
        StockEntry.objects.update(supplier=None)
        InventoryRun.objects.all().delete()
        importlib.import_module('mall.migrations.0063_supplier_ledger').rebuild(apps, None)
        self.assertFalse(StockEntry.objects.filter(supplier__isnull=True).exists())
        runs = {run.label: run for run in InventoryRun.objects.all()}
        self.assertEqual(set(runs), {'seed-STABLE-5', 'excel-0f100000-0000-4000-8000-000000000001', 'apiag-cloud 2026-10-11 02:30 #e72d'})
        excel = runs['excel-0f100000-0000-4000-8000-000000000001']
        self.assertEqual((excel.origin, excel.rebuilt, excel.counts['created'], excel.changes.get().quantity_after), ('excel', True, 1, 4))
        moved = runs['apiag-cloud 2026-10-11 02:30 #e72d'].changes.get()
        self.assertEqual((moved.action, moved.quantity_before, moved.quantity_after), ('stock', 4, 1))
        self.assertEqual(InventoryUpdate.objects.count(), 3)
        self.assertEqual(InventoryChange.objects.count(), 3)
