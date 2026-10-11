import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from . import apiag_client
from .apiag_models import ApiagConnection
from .apiag_sync import SERVICE_USER, process_due, request_run
from .apiag_sync_models import ApiagSucursal, ApiagSync, SupplierItemBranchStock
from .apiag_tokens import encrypt
from .check_stock_ledger import check
from .models import Account, Membership, Role, StockEntry, SupplierItem, User

KEY = Fernet.generate_key().decode()


class FakeApiag:
    """A stand-in for apiag-cloud's GET /api/motionpartes/stock/ (its feed.py): full pages by id, or the ids marked changed."""

    def __init__(self):
        self.sucursales = [{'id': 1, 'nombre': 'MATRIZ'}, {'id': 2, 'nombre': 'DAVID'}]
        self.items, self.retired, self.changed, self.calls = {}, set(), set(), []
        self.clock = timezone.now()
        self.answer = None  # (status, body) or an exception, instead of a page

    def publish(self, item_id, codigo, stock, **fields):
        self.items[item_id] = {'id': item_id, 'status': 'published', 'codigo': codigo, 'codigo2': fields.get('codigo2'),
                               'descripcion': fields.get('descripcion', f'Artículo {codigo}'), 'marca': fields.get('marca', 'TOYOTA'),
                               'alternos': fields.get('alternos', []),
                               'stock': [{'sucursal': suc, 'disponible': units} for suc, units in stock.items()]}
        self.retired.discard(item_id)
        self.changed.add(item_id)

    def retire(self, item_id):
        self.items.pop(item_id, None)
        self.retired.add(item_id)
        self.changed.add(item_id)

    def __call__(self, server_url, path, token, max_bytes=65536):
        query = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        self.calls.append(query)
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer:
            return self.answer[0], json.dumps(self.answer[1]).encode(), self.answer[2] if len(self.answer) > 2 else {}
        after, limit = int(query.get('after', 0)), int(query['limit'])
        ids = sorted(self.changed) if 'since' in query else sorted(self.items)
        rest = [item_id for item_id in ids if item_id > after]
        listed = {sucursal['id'] for sucursal in self.sucursales}
        entries = []
        for item_id in rest[:limit]:
            if item_id in self.items:
                entry = dict(self.items[item_id])
                entry['stock'] = [row for row in entry['stock'] if row['sucursal'] in listed]
                entries.append(entry)
            elif 'since' in query:
                entries.append({'id': item_id, 'status': 'retired'})
        return 200, json.dumps({
            'server_time': self.clock.isoformat(), 'mode': 'changes' if 'since' in query else 'full', 'overlap_seconds': 600,
            'sucursales': self.sucursales, 'sucursales_version': ','.join(str(sucursal['id']) for sucursal in self.sucursales),
            'items': entries, 'next': rest[limit - 1] if len(rest) > limit else None}).encode(), {}


@override_settings(APIAG_TOKEN_KEYS=[KEY], APIAG_SYNC_MINUTES=5, APIAG_SYNC_DAILY_AT='03:30')
class ApiagSyncTests(APITestCase):
    def setUp(self):
        self.supplier = Account.objects.create(name='Motion Motors')
        self.supplier.roles.add(Role.objects.get(code='supplier_wholesale'))
        self.member = User.objects.create_user('mm-staff', 'mm-staff@example.com', 'SecureStaff938!')
        Membership.objects.create(user=self.member, account=self.supplier, permission='staff')
        self.connection = ApiagConnection.objects.create(account=self.supplier, server_url='https://mm.oratek.info',
                                                         token_ciphertext=encrypt('apiagTok3n-For-MotionPartes-0001'), token_hint='0001')
        self.apiag = FakeApiag()
        patcher = patch('mall.apiag_sync.apiag_client.get', self.apiag)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync_row(self):
        return ApiagSync.objects.get(connection=self.connection)

    def run_pull(self):
        self.apiag.clock += timedelta(minutes=5)
        ApiagSync.objects.filter(connection=self.connection).update(next_run_at=None)
        result = process_due()
        self.apiag.changed = set()
        return result

    def item(self, apiag_id):
        return SupplierItem.objects.get(supplier=self.supplier, supplier_invent_id=str(apiag_id))

    def branches(self, apiag_id):
        return {row.sucursal.nombre: row.quantity for row in SupplierItemBranchStock.objects.filter(item=self.item(apiag_id)).select_related('sucursal')}

    def assert_ledger_reconciles(self):
        self.assertEqual(check()['problems'], {})

    def test_the_first_pull_brings_every_published_item_with_its_units_per_sucursal(self):
        self.apiag.publish(10, 'pastilla-1', {1: 4, 2: 1}, codigo2='GDB3425', alternos=['D2219'], marca='Toyota')
        self.apiag.publish(11, 'FILTRO-2', {1: 0, 2: 0})
        self.apiag.publish(12, 'BUJIA-3', {1: -3, 2: 2})  # a negative never comes from the feed, but must not count
        result = self.run_pull()
        self.assertEqual((result['mode'], result['created']), ('full', 3))
        item = self.item(10)
        self.assertEqual((item.source, item.codigo, item.brand, item.reported_quantity, item.reserved_quantity), ('apiag', 'PASTILLA-1', 'TOYOTA', 5, 0))
        self.assertEqual(item.references, [{'brand': '', 'code': 'D2219'}, {'brand': '', 'code': 'GDB3425'}])
        self.assertEqual(self.branches(10), {'MATRIZ': 4, 'DAVID': 1})
        self.assertEqual((self.item(11).reported_quantity, self.branches(11)), (0, {}))
        self.assertEqual(self.branches(12), {'DAVID': 2})
        entry = StockEntry.objects.get(item=item)
        self.assertEqual((entry.kind, entry.direction, entry.quantity, entry.actor.username), ('sync', 'credit', 5, SERVICE_USER))
        self.assertTrue(entry.reference.startswith('apiag-cloud '))
        sync = self.sync_row()
        self.assertEqual((sync.stage, sync.error, sync.since, sync.sucursales_version), ('completed', '', self.apiag.clock.isoformat(), '1,2'))
        self.assertAlmostEqual(sync.next_run_at, sync.finished_at + timedelta(minutes=5), delta=timedelta(seconds=1))
        self.assertEqual(list(ApiagSucursal.objects.filter(account=self.supplier, listed=True).values_list('nombre', flat=True).order_by('apiag_id')), ['MATRIZ', 'DAVID'])
        self.assertNotIn('since', self.apiag.calls[0])
        self.assert_ledger_reconciles()

    def test_later_pulls_apply_only_what_changed(self):
        self.apiag.publish(10, 'A-1', {1: 4, 2: 1})
        self.apiag.publish(11, 'B-2', {1: 2})
        self.apiag.publish(12, 'C-3', {2: 6})
        self.run_pull()
        self.apiag.publish(10, 'A-1', {1: 1, 2: 1})     # sold 3 at MATRIZ
        self.apiag.publish(11, 'B-2', {2: 2})           # moved between sucursales: same total
        self.apiag.retire(12)
        self.apiag.publish(13, 'D-4', {1: 7})
        result = self.run_pull()
        self.assertEqual(result['mode'], 'changes')
        self.assertEqual(self.apiag.calls[-1]['since'], (self.apiag.clock - timedelta(minutes=5)).isoformat())  # the last pull's server_time
        self.assertEqual((result['updated'], result['unchanged'], result['withdrawn'], result['created']), (1, 1, 1, 1))
        self.assertEqual((self.item(10).reported_quantity, self.branches(10)), (2, {'MATRIZ': 1, 'DAVID': 1}))
        self.assertEqual((self.item(11).reported_quantity, self.branches(11)), (2, {'DAVID': 2}))
        self.assertEqual(StockEntry.objects.filter(item=self.item(11)).count(), 1)  # a move between sucursales writes no entry
        self.assertEqual((self.item(12).reported_quantity, self.branches(12)), (0, {}))
        self.assertEqual(StockEntry.objects.filter(item=self.item(12)).latest('id').direction, 'debit')
        self.assertEqual(self.item(13).reported_quantity, 7)
        self.run_pull()  # nothing changed: nothing written
        self.assertEqual(StockEntry.objects.filter(item__supplier=self.supplier).count(), 6)
        self.assert_ledger_reconciles()

    def test_a_new_sucursal_choice_pulls_everything_again_and_drops_what_left(self):
        self.apiag.publish(10, 'A-1', {1: 4, 2: 1})
        self.apiag.publish(11, 'B-2', {2: 3})
        self.run_pull()
        self.apiag.sucursales = [{'id': 1, 'nombre': 'MATRIZ'}]   # DAVID no longer published
        del self.apiag.items[11]                                 # and B-2 unpublished while nothing said so
        result = self.run_pull()
        self.assertEqual(result['mode'], 'full')
        self.assertEqual((self.item(10).reported_quantity, self.branches(10)), (4, {'MATRIZ': 4}))
        self.assertEqual((self.item(11).reported_quantity, self.branches(11)), (0, {}))
        self.assertFalse(ApiagSucursal.objects.get(account=self.supplier, apiag_id='2').listed)
        self.assertEqual(self.sync_row().sucursales_version, '1')
        self.assert_ledger_reconciles()

    def test_manual_items_and_bad_rows_do_not_stop_the_pull(self):
        SupplierItem.objects.create(supplier=self.supplier, supplier_invent_id='10', codigo='MANUAL', brand='X', source='upload', reported_quantity=0)
        self.apiag.publish(10, 'A-1', {1: 4})
        self.apiag.publish(11, '', {1: 2})       # no código: refused
        self.apiag.publish(12, 'C-3', {1: 1})
        result = self.run_pull()
        self.assertEqual((result['conflicts'], result['refused'], result['created']), (1, 1, 1))
        self.assertEqual((self.item(10).codigo, self.item(10).source), ('MANUAL', 'upload'))
        self.assertEqual(self.item(12).reported_quantity, 1)
        self.assertEqual(len(self.sync_row().summary['samples']), 2)
        self.assert_ledger_reconciles()

    def test_failures_keep_the_cursor_and_wait_or_pause(self):
        self.apiag.publish(10, 'A-1', {1: 4})
        self.run_pull()
        since = self.sync_row().since
        self.apiag.answer = apiag_client.Unreachable('unreachable', 'mm.oratek.info no respondió en 8 segundos.')
        self.run_pull()
        sync = self.sync_row()
        self.assertEqual((sync.stage, sync.error_code, sync.since, sync.failures), ('failed', 'unreachable', since, 1))
        self.assertGreater(sync.next_run_at, timezone.now())
        self.apiag.answer = (401, {'detail': 'Token de MotionPartes inválido.'}, {'WWW-Authenticate': 'Bearer realm="motionpartes"'})
        self.run_pull()
        sync = self.sync_row()
        self.assertEqual((sync.stage, sync.error_code, sync.next_run_at), ('paused', 'token_rejected', None))
        self.assertIsNone(process_due())                       # paused: not due by time
        ApiagConnection.objects.filter(pk=self.connection.pk).update(updated_at=timezone.now() + timedelta(seconds=1))
        self.apiag.answer = None
        self.assertEqual(process_due()['mode'], 'changes')     # the admin saved the connection again: it resumes
        self.apiag.answer = (400, {'since': 'Han pasado más de 7 días: vuelve a pedir todo, sin since.'})
        self.run_pull()
        self.assertEqual(self.sync_row().since, '')
        self.apiag.answer = None
        self.assertEqual(self.run_pull()['mode'], 'full')

    def test_sincronizar_ahora_queues_one_run_and_goes_first(self):
        other = Account.objects.create(name='Otro proveedor')
        other_connection = ApiagConnection.objects.create(account=other, server_url='https://otro.example', token_ciphertext=encrypt('apiagTok3n-For-MotionPartes-0002'), token_hint='0002')
        self.apiag.publish(10, 'A-1', {1: 1})
        process_due(); process_due()           # both connections pulled once
        request_run(self.connection, self.member)
        request_run(self.connection, self.member)
        sync = self.sync_row()
        self.assertEqual(sync.requested - sync.completed, 1)
        ApiagSync.objects.filter(connection=other_connection).update(next_run_at=timezone.now() - timedelta(minutes=1))
        process_due()
        self.assertEqual(self.sync_row().requested, self.sync_row().completed)     # the asked run went first
        self.assertEqual(process_due()['apiag_sync'], str(other.pk))              # then the one that was due by time
        ApiagSync.objects.filter(connection=self.connection).update(lease='00000000-0000-0000-0000-000000000001',
                                                                     lease_until=timezone.now() + timedelta(minutes=5), next_run_at=None)
        request_run(self.connection, self.member)
        self.assertIsNone(process_due())       # ours is leased by a pull still running, the other one is not due yet
        self.assertEqual(self.sync_row().requested - self.sync_row().completed, 1)

    def test_the_daily_pass_reads_the_24_hours_before_the_last_pull(self):
        self.apiag.publish(10, 'A-1', {1: 1})
        self.run_pull()
        ApiagSync.objects.filter(connection=self.connection).update(last_day_at=timezone.now() - timedelta(days=2))
        with override_settings(APIAG_SYNC_DAILY_AT='00:00'):
            result = self.run_pull()
        self.assertEqual(result['mode'], 'day')
        since = self.apiag.calls[-1]['since']
        self.assertEqual(since, (self.apiag.clock - timedelta(minutes=5) - timedelta(hours=24)).isoformat())
        self.assertEqual(self.run_pull()['mode'], 'changes')

    def test_the_supplier_panel_reads_the_state_and_asks_for_a_run(self):
        self.client.force_authenticate(self.member)
        path = f'/api/v1/accounts/{self.supplier.pk}/apiag/'
        data = self.client.get(path).json()
        self.assertEqual((data['connected'], data['server'], data['state'], data['can_sync'], data['interval_minutes']),
                         (True, 'mm.oratek.info', 'idle', True, 5))
        self.assertNotIn('apiagTok3n', json.dumps(data))
        response = self.client.post(f'{path}sync/')
        self.assertEqual((response.status_code, response.json()['state'], response.json()['can_sync']), (202, 'queued', False))
        self.apiag.publish(10, 'A-1', {1: 3, 2: 2})
        process_due()
        data = self.client.get(path).json()
        self.assertEqual((data['state'], data['counts']['created'], data['last_mode']), ('completed', 1, 'full'))
        self.assertEqual(data['sucursales'], [{'id': '2', 'nombre': 'DAVID'}, {'id': '1', 'nombre': 'MATRIZ'}])
        rows = self.client.get(f'/api/v1/accounts/{self.supplier.pk}/inventory/').json()['results']
        self.assertEqual(rows[0]['branch_stock'], [{'sucursal': '2', 'nombre': 'DAVID', 'disponible': 2}, {'sucursal': '1', 'nombre': 'MATRIZ', 'disponible': 3}])
        stranger = User.objects.create_user('stranger', 'stranger@example.com', 'SecureStranger938!')
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.post(f'{path}sync/').status_code, 404)

    def test_an_account_without_a_connection(self):
        self.connection.delete()
        self.client.force_authenticate(self.member)
        path = f'/api/v1/accounts/{self.supplier.pk}/apiag/'
        self.assertEqual(self.client.get(path).json(), {'connected': False})
        self.assertEqual(self.client.post(f'{path}sync/').status_code, 404)

    def test_the_ledger_check_catches_sucursal_rows_that_do_not_add_up(self):
        self.apiag.publish(10, 'A-1', {1: 4, 2: 1})
        self.run_pull()
        SupplierItemBranchStock.objects.filter(item=self.item(10), sucursal__apiag_id='1').update(quantity=9)
        self.assertEqual(check()['problems']['branches']['count'], 1)

    def test_an_item_refused_after_it_was_synced_stops_selling_and_absurd_totals_are_refused(self):
        self.apiag.publish(10, 'A-1', {1: 4, 2: 1})
        self.apiag.publish(11, 'B-2', {1: 2})
        self.run_pull()
        self.apiag.publish(10, '', {1: 9})                                       # its código was cleared in apiag-cloud
        self.apiag.publish(11, 'B-2', {1: 2_000_000_000, 2: 2_000_000_000})     # a placeholder quantity
        result = self.run_pull()
        self.assertEqual((result['refused'], result['withdrawn']), (2, 2))
        self.assertEqual((self.item(10).reported_quantity, self.branches(10), self.item(10).codigo), (0, {}, 'A-1'))
        self.assertEqual((self.item(11).reported_quantity, self.branches(11)), (0, {}))
        self.assert_ledger_reconciles()

    def test_a_server_that_repeats_a_page_or_takes_too_long_is_stopped(self):
        for index in range(3):
            self.apiag.publish(10 + index, f'P-{index}', {1: 1})
        original = self.apiag.__call__
        def stuck(server_url, path, token, max_bytes=65536):
            status, body, headers = original(server_url, path.replace('limit=500', 'limit=1').replace('after=11', 'after=10'), token, max_bytes)
            return status, body, headers
        with patch('mall.apiag_sync.apiag_client.get', stuck), patch('mall.apiag_sync.PAGE_SIZE', 1):
            self.run_pull()
        self.assertEqual((self.sync_row().stage, self.sync_row().error_code), ('failed', 'unexpected'))
        with patch('mall.apiag_sync.RUN_LIMIT', timedelta(seconds=-1)):
            self.run_pull()
        self.assertEqual((self.sync_row().stage, self.sync_row().error_code), ('failed', 'timeout'))

    def test_a_connection_saved_while_the_failing_run_was_in_flight_resumes_it(self):
        self.apiag.publish(10, 'A-1', {1: 1})
        self.run_pull()
        self.apiag.answer = (401, {'detail': 'Token de MotionPartes inválido.'}, {'WWW-Authenticate': 'Bearer realm="motionpartes"'})
        started = timezone.now()
        def save_mid_run(server_url, path, token, max_bytes=65536):
            ApiagConnection.objects.filter(pk=self.connection.pk).update(updated_at=timezone.now())  # saved again during the run
            return self.apiag(server_url, path, token, max_bytes)
        with patch('mall.apiag_sync.apiag_client.get', save_mid_run):
            self.run_pull()
        self.assertEqual(self.sync_row().stage, 'paused')
        self.apiag.answer = None
        self.assertIsNotNone(process_due())   # the save came after the run started: it is retried
        self.assertGreaterEqual(self.sync_row().started_at, started)

    def test_entries_are_labelled_in_local_time_and_stock_moves_queue_no_matching(self):
        from .matching_models import MatchingQueue
        self.apiag.clock = datetime(2026, 10, 11, 20, 0, tzinfo=dt_timezone.utc) - timedelta(minutes=5)  # 15:00 in Panamá
        self.apiag.publish(10, 'A-1', {1: 3})
        with self.captureOnCommitCallbacks(execute=True):
            self.run_pull()
        self.assertTrue(StockEntry.objects.get(item=self.item(10)).reference.startswith('apiag-cloud 2026-10-11 15:00 #'))  # Panamá
        requested = MatchingQueue.objects.get(pk=1).requested
        self.apiag.publish(10, 'A-1', {1: 1})
        with self.captureOnCommitCallbacks(execute=True):
            self.run_pull()
        self.assertEqual(MatchingQueue.objects.get(pk=1).requested, requested)    # only the quantity moved

    def test_the_supplier_reads_errors_meant_for_them_and_cannot_write_apiag_items_by_hand(self):
        self.client.force_authenticate(self.member)
        path = f'/api/v1/accounts/{self.supplier.pk}/apiag/'
        ApiagSync.objects.update_or_create(connection=self.connection, defaults={'stage': 'paused', 'error_code': 'redirect',
                                                                                 'error': 'La dirección redirige a otra (https://x). Usa la dirección final.'})
        self.assertIn('pide a MotionPartes que la revise', self.client.get(path).json()['error']['message'])
        ApiagSync.objects.filter(connection=self.connection).update(error_code='token_unreadable', error='El token guardado ya no se puede leer.')
        self.assertIsNone(self.client.get(path).json()['error'])
        response = self.client.post(f'/api/v1/accounts/{self.supplier.pk}/inventory/ingest/', {
            'update_id': 'manual-1', 'supplier_invent_id': '99', 'codigo': 'X-1', 'brand': 'X', 'source': 'apiag', 'quantity': 5}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('source', response.json())
        self.assertFalse(SupplierItem.objects.filter(supplier=self.supplier, supplier_invent_id='99').exists())
