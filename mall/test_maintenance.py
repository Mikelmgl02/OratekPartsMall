"""Ops safety of the refactor's deploy windows (S0.11): the catalog write freeze, the workers' clean stop, the CLIs' live-database guard
and the migrations' lock timeout. The runner loop itself is tested in test_matching_runner."""
import io
import json
import signal
import tempfile
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.db import connection
from django.db.migrations.writer import OperationWriter
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.test import APITestCase

from . import link_oem_numbers, mark_exact_oem, oem_finder_revert, supplier_catalog_replace
from .apiag_models import ApiagConnection
from .apiag_sync import process_due
from .apiag_sync_models import ApiagSync
from .apiag_tokens import encrypt
from .maintenance import (ALL_DETAIL, BIBLIOTECA_DETAIL, CATALOG_DETAIL, CATALOG_ROUTES, MaintenanceFreezeMiddleware, biblioteca_frozen,
                          freeze_level)
from .matching_models import MatchingQueue
from .matching_runner import STOP, WorkerStopping
from .matching_worker import process_queue
from .models import Account, Membership, Part, Role, SupplierItem, User, WishlistItem
from .refactor.guard import live_database, refusal
from .refactor.migration_ops import LockTimeout
from .test_apiag_sync import FakeApiag

ID = '00000000-0000-4000-8000-000000000001'
ACCOUNT = f'/api/v1/accounts/{ID}'


class FreezeFolderMixin:
    """A MAINTENANCE_DIR of the test's own; freeze(level) writes the freeze file, freeze(None) removes it."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        settings_override = override_settings(MAINTENANCE_DIR=self.folder)
        settings_override.enable()
        self.addCleanup(settings_override.disable)

    def freeze(self, level='catalog'):
        path = self.folder / 'freeze'
        if level is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(f'{level}\n', encoding='utf-8')


class FreezeLevelTests(FreezeFolderMixin, SimpleTestCase):
    def test_the_file_and_its_level(self):
        self.assertIsNone(freeze_level())
        for text, level in [('catalog', 'catalog'), ('', 'catalog'), ('ALL', 'all'), (' all \n', 'all'), ('catalogo', 'catalog')]:
            (self.folder / 'freeze').write_text(text, encoding='utf-8')
            self.assertEqual(freeze_level(), level, text)
        self.freeze(None)
        self.assertIsNone(freeze_level())

    def test_a_missing_folder_is_no_freeze_and_an_unreadable_file_is_one(self):
        with override_settings(MAINTENANCE_DIR=self.folder / 'missing'):
            self.assertIsNone(freeze_level())
        (self.folder / 'freeze').mkdir()  # exists, cannot be read: fail closed
        self.assertEqual(freeze_level(), 'catalog')

    def test_the_biblioteca_file_is_its_own_switch(self):
        self.assertFalse(biblioteca_frozen())
        (self.folder / 'biblioteca').touch()
        self.assertTrue(biblioteca_frozen())
        self.assertIsNone(freeze_level())


class FreezeMiddlewareTests(FreezeFolderMixin, SimpleTestCase):
    # What the freeze holds back at each level, and what always goes through.
    CATALOG_WRITES = [
        ('patch', f'/api/v1/management/catalog/{ID}/'), ('post', '/api/v1/management/catalog/'),
        ('post', '/api/v1/management/catalog/import/'), ('post', f'/api/v1/management/catalog/import/jobs/{ID}/'),
        ('post', '/api/v1/management/catalog/proposals/bulk/'), ('patch', f'/api/v1/management/catalog/proposals/review/{ID}/'),
        ('patch', f'/api/v1/management/catalog/assistant/{ID}/'), ('patch', '/api/v1/management/catalog/suffixes/A/'),
        ('post', '/api/v1/management/catalog/grouping/merge/'), ('post', f'/api/v1/management/catalog/{ID}/split/'),
        ('post', f'/api/v1/management/catalog/{ID}/images/'), ('put', f'/api/v1/management/catalog/{ID}/technical/'),
        ('patch', f'/api/v1/management/inventory/{ID}/'), ('post', '/api/v1/management/alternates/'),
        ('delete', '/api/v1/management/alternates/7/'), ('post', f'/api/v1/management/alternate-review/{ID}/'),
        ('patch', f'/api/v1/management/oem-references/{ID}/'), ('post', '/api/v1/management/oem-review/bulk/'),
        ('post', '/api/v1/management/oem-finder/pending/apply/'), ('post', '/api/v1/management/oem-finder/runs/3/revert/'),
        ('post', '/api/v1/management/matching/'), ('post', f'/api/v1/management/matching/{ID}/'),
        ('post', '/api/v1/management/part-types/'), ('patch', f'/api/v1/management/part-types/{ID}/template/'),
        ('delete', f'/api/v1/management/applications/{ID}/'),
        ('post', f'{ACCOUNT}/inventory/ingest/'), ('post', f'{ACCOUNT}/inventory/import/'),
        ('post', f'{ACCOUNT}/inventory/import/jobs/{ID}/'),
    ]
    ALL_WRITES = [
        ('post', f'{ACCOUNT}/requests/'), ('post', f'{ACCOUNT}/requests/{ID}/review/'), ('post', f'{ACCOUNT}/requests/{ID}/draft/'),
        ('post', f'{ACCOUNT}/sent-requests/'), ('post', f'{ACCOUNT}/sent-requests/{ID}/missing/'),
        ('post', f'{ACCOUNT}/deals/{ID}/actions/'), ('post', f'/api/v1/management/deals/{ID}/cancel/'), ('post', f'{ACCOUNT}/basket/'),
    ]
    ALWAYS_OPEN = [
        ('get', '/health/'), ('post', '/api/v1/auth/login/'), ('post', '/api/v1/auth/logout/'), ('patch', '/api/v1/auth/me/'),
        ('post', f'{ACCOUNT}/deals/{ID}/messages/'), ('post', f'{ACCOUNT}/threads/{ID}/messages/'),
        ('post', f'{ACCOUNT}/threads/{ID}/attachments/4/retry/'), ('post', '/api/v1/inbox/notifications/seen/'),
        ('post', f'/api/v1/inbox/conversations/{ID}/read/'), ('post', '/api/v1/inbox/push/'), ('post', '/api/v1/inbox/whatsapp/'),
        ('post', '/api/v1/whatsapp/webhook/'), ('put', f'/api/v1/wishlist/{ID}/'), ('post', '/api/v1/analytics/events/'),
        ('post', f'{ACCOUNT}/apiag/sync/'), ('get', f'{ACCOUNT}/inventory/import/template/'), ('post', f'{ACCOUNT}/prices/import/'),
        ('patch', f'/api/v1/management/accounts/{ID}/'),
        ('get', f'/api/v1/management/catalog/{ID}/'), ('get', '/api/v1/management/oem-references/'), ('head', '/api/v1/management/matching/'),
        ('options', f'{ACCOUNT}/basket/'), ('get', f'{ACCOUNT}/sent-requests/'),
    ]

    def setUp(self):
        super().setUp()
        self.middleware = MaintenanceFreezeMiddleware(lambda request: 'passed')
        self.factory = RequestFactory()

    def call(self, method, path):
        return self.middleware(getattr(self.factory, method)(path))

    def assert_held(self, routes, detail):
        for method, path in routes:
            response = self.call(method, path)
            self.assertNotEqual(response, 'passed', f'{method.upper()} {path} went through')
            self.assertEqual((response.status_code, json.loads(response.content), response['Retry-After']), (503, {'detail': detail}, '120'),
                             f'{method.upper()} {path}')

    def assert_open(self, routes):
        for method, path in routes:
            self.assertEqual(self.call(method, path), 'passed', f'{method.upper()} {path} was held back')

    def test_without_the_freeze_everything_goes_through(self):
        (self.folder / 'biblioteca').unlink(missing_ok=True)
        self.assert_open(self.CATALOG_WRITES + self.ALL_WRITES + self.ALWAYS_OPEN)

    def test_level_catalog_holds_catalog_and_inventory_writes_only(self):
        self.freeze('catalog')
        self.assert_held(self.CATALOG_WRITES, CATALOG_DETAIL)
        self.assert_open(self.ALL_WRITES + self.ALWAYS_OPEN)

    def test_level_all_also_holds_orders_deal_actions_and_the_cesta(self):
        self.freeze('all')
        self.assert_held(self.CATALOG_WRITES, CATALOG_DETAIL)
        self.assert_held(self.ALL_WRITES, ALL_DETAIL)
        self.assert_open(self.ALWAYS_OPEN)

    def test_the_biblioteca_file_holds_only_the_biblioteca(self):
        (self.folder / 'biblioteca').touch()
        biblioteca = [(method, path) for method, path in self.CATALOG_WRITES if '/oem-references/' in path]
        self.assert_held(biblioteca + [('post', '/api/v1/management/oem-references/bulk-edit/'),
                                       ('delete', f'/api/v1/management/oem-references/{ID}/cross-references/9/')], BIBLIOTECA_DETAIL)
        self.assert_open([route for route in self.CATALOG_WRITES if route not in biblioteca] + self.ALL_WRITES + self.ALWAYS_OPEN)
        self.freeze('catalog')  # the catalog freeze says the broader thing
        self.assert_held(biblioteca, CATALOG_DETAIL)

    def test_every_management_route_is_frozen_or_known_to_stay_open(self):
        # A new management screen must decide: does it write the catalog (add it to CATALOG_ROUTES) or not (add it here)?
        open_prefixes = ('analytics', 'deals', 'accounts', 'users', 'roles', 'memberships', 'invitations')
        unclassified = []
        for path in route_paths(get_resolver()):
            if not path.startswith('/api/v1/management/'):
                continue
            frozen = any(route.match(path) for route in CATALOG_ROUTES)
            if not frozen and path.split('/')[4] not in open_prefixes:
                unclassified.append(path)
            if frozen and path.split('/')[4] in open_prefixes:
                unclassified.append(path)
        self.assertEqual(unclassified, [])


def route_paths(resolver, prefix='/'):
    """Every URL of the project with sample values in place of its converters."""
    samples = {'uuid': ID, 'int': '7', 'path': 'A', 'str': 'A'}
    for entry in resolver.url_patterns:
        text = str(entry.pattern)
        for kind, value in samples.items():
            while f'<{kind}:' in text:
                start = text.index(f'<{kind}:')
                text = text[:start] + value + text[text.index('>', start) + 1:]
        if isinstance(entry, URLResolver):
            yield from route_paths(entry, prefix + text)
        elif isinstance(entry, URLPattern):
            yield prefix + text


class FreezeThroughTheApiTests(FreezeFolderMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser('root-freeze', 'root-freeze@example.invalid', 'SecureRoot938!')
        self.part = Part.objects.create(sku='58411-1R000', name='Tambor', description='TAMBOR HYU ACCENT 11-16')
        self.client.force_authenticate(self.root)

    def test_a_frozen_catalog_edit_writes_nothing_and_an_open_write_still_saves(self):
        self.freeze('all')
        response = self.client.patch(f'/api/v1/management/catalog/{self.part.pk}/', {'description': 'NO'}, format='json')
        self.assertEqual((response.status_code, response.json()), (503, {'detail': CATALOG_DETAIL}))
        self.part.refresh_from_db()
        self.assertEqual(self.part.description, 'TAMBOR HYU ACCENT 11-16')
        self.assertEqual(self.client.get(f'/api/v1/management/catalog/{self.part.pk}/').status_code, 200)
        client = User.objects.create_user('client-freeze', 'client-freeze@example.invalid', 'SecureClient938!')
        account = Account.objects.create(name='CLIENTE')
        account.roles.add(Role.objects.get(code='client_business'))
        Membership.objects.create(user=client, account=account)
        self.client.force_authenticate(client)
        self.assertEqual(self.client.put(f'/api/v1/wishlist/{self.part.pk}/').status_code, 200)  # favourites stay open
        self.assertTrue(WishlistItem.objects.filter(user=client, part=self.part).exists())
        self.client.force_authenticate(self.root)
        self.freeze(None)
        response = self.client.patch(f'/api/v1/management/catalog/{self.part.pk}/', {'description': 'TAMBOR NUEVO'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.part.refresh_from_db()
        self.assertEqual(self.part.description, 'TAMBOR NUEVO')


class WorkerStopMixin(FreezeFolderMixin):
    def setUp(self):
        super().setUp()
        STOP.reset()
        self.addCleanup(STOP.reset)


class MatchingWorkerStopTests(WorkerStopMixin, TestCase):
    def setUp(self):
        super().setUp()
        MatchingQueue.objects.create(pk=1, requested=3, completed=2)

    def stopped_pass(self, during):
        with patch('mall.matching_worker.reconcile_catalog', side_effect=during) as reconcile, \
                patch('mall.matching_worker.create_supplier_families') as families, self.assertRaises(WorkerStopping) as stopped:
            process_queue()
        reconcile.assert_called_once()
        families.assert_not_called()  # the next stage never started
        queue = MatchingQueue.objects.get(pk=1)
        self.assertEqual((queue.lease, queue.lease_until, queue.stage, queue.completed, queue.error), (None, None, 'stopped', 2, ''))
        return str(stopped.exception)

    def test_a_stop_signal_ends_the_pass_at_the_next_stage_and_gives_the_lease_back(self):
        def signalled(*args, **kwargs):
            STOP.handle(signal.SIGTERM)
            return 0
        self.assertEqual(self.stopped_pass(signalled), 'signal')

    def test_a_freeze_that_begins_mid_pass_ends_it_the_same_way(self):
        STOP.idle_under = ('catalog', 'all')  # as the runner sets it
        def frozen(*args, **kwargs):
            self.freeze('catalog')
            return 0
        self.assertEqual(self.stopped_pass(frozen), 'freeze')

    def test_outside_the_runner_the_freeze_does_not_stop_a_pass(self):
        self.freeze('all')
        summary = process_queue(use_ai=False)
        queue = MatchingQueue.objects.get(pk=1)
        self.assertEqual((queue.stage, queue.completed, queue.lease), ('completed', 3, None))
        self.assertIn('matched_items', summary)


KEY = Fernet.generate_key().decode()


@override_settings(APIAG_TOKEN_KEYS=[KEY], APIAG_SYNC_MINUTES=5, APIAG_SYNC_DAILY_AT='03:30')
class ApiagStopTests(WorkerStopMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.supplier = Account.objects.create(name='Motion Motors')
        self.supplier.roles.add(Role.objects.get(code='supplier_wholesale'))
        self.connection = ApiagConnection.objects.create(account=self.supplier, server_url='https://mm.oratek.info',
                                                         token_ciphertext=encrypt('apiagTok3n-For-MotionPartes-0001'), token_hint='0001')
        self.apiag = FakeApiag()
        for item_id in (10, 11, 12):
            self.apiag.publish(item_id, f'FILTRO-{item_id}', {1: 2})
        for target, value in [('mall.apiag_sync.apiag_client.get', self.apiag), ('mall.apiag_sync.PAGE_SIZE', 1)]:
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_stop_signal_ends_the_pull_after_its_page_and_the_next_run_starts_it_again(self):
        fetch = self.apiag.__call__
        def signalled(*args, **kwargs):
            STOP.handle(signal.SIGTERM)  # arrives while the first page is on its way
            return fetch(*args, **kwargs)
        with patch('mall.apiag_sync.apiag_client.get', signalled), self.assertRaises(WorkerStopping):
            process_due()
        sync = ApiagSync.objects.get(connection=self.connection)
        self.assertEqual((sync.lease, sync.lease_until, sync.stage, sync.started_at, sync.since, sync.progress, sync.completed),
                         (None, None, 'idle', None, '', {}, 0))
        self.assertEqual(list(SupplierItem.objects.filter(supplier=self.supplier).values_list('supplier_invent_id', flat=True)), ['10'])
        STOP.reset()
        self.assertEqual(process_due()['mode'], 'full')  # the same pull, from the start: values are absolute
        sync.refresh_from_db()
        self.assertEqual((sync.stage, sync.since), ('completed', self.apiag.clock.isoformat()))
        self.assertEqual(SupplierItem.objects.filter(supplier=self.supplier, reported_quantity=2).count(), 3)


class CliGuardTests(FreezeFolderMixin, TestCase):
    def live(self):
        return override_settings(LIVE_DATABASE_NAME=connection.settings_dict['NAME'])

    def test_only_the_live_database_needs_the_freeze(self):
        self.assertFalse(live_database())  # the test database has its own name
        self.assertIsNone(refusal())
        with self.live():
            self.assertEqual(live_database(), connection.vendor == 'postgresql')
            if connection.vendor != 'postgresql':
                return
            self.assertIn('sin el congelamiento del catálogo', refusal())
            self.assertIn(str(self.folder / 'freeze'), refusal())
            self.assertIsNone(refusal(applying=False))  # a dry run needs nothing
            self.freeze('catalog')
            self.assertIsNone(refusal())
            (self.folder / 'freeze').unlink()
            (self.folder / 'biblioteca').touch()  # the Biblioteca switch is not the catalog freeze
            self.assertIsNotNone(refusal())

    def test_the_catalog_clis_refuse_apply_on_the_live_database_until_it_is_frozen(self):
        if connection.vendor != 'postgresql':
            self.skipTest('The live database is PostgreSQL.')
        Part.objects.create(sku='16100-79445', name='Bomba')
        with self.live():
            for main, argv in [(link_oem_numbers.main, ['--apply']), (mark_exact_oem.main, ['--apply']),
                               (supplier_catalog_replace.main, ['--apply', '/nonexistent.json']),
                               (oem_finder_revert.main, ['--run', '1'])]:
                with self.assertRaises(SystemExit) as refused:
                    main(argv, stdout=io.StringIO())
                self.assertIn('sin el congelamiento del catálogo', str(refused.exception.code), argv)
            out = io.StringIO()
            link_oem_numbers.main(['--dry-run'], stdout=out)
            self.assertIn('Simulación', out.getvalue())
            self.freeze('catalog')
            out = io.StringIO()
            link_oem_numbers.main(['--apply'], stdout=out)
            self.assertIn('Aplicado', out.getvalue())


class LockTimeoutTests(TestCase):
    def test_it_sets_the_lock_timeout_for_the_migration_transaction(self):
        operation = LockTimeout('5s')
        self.assertEqual(operation.deconstruct(), ('LockTimeout', ('5s',), {}))
        self.assertEqual((operation.describe(), operation.migration_name_fragment, operation.reversible),
                         ('Wait at most 5s for table locks', 'lock_timeout', True))
        if connection.vendor != 'postgresql':
            return
        with connection.schema_editor(collect_sql=True, atomic=True) as editor:
            operation.database_forwards('mall', editor, None, None)
            operation.database_backwards('mall', editor, None, None)
        self.assertEqual(editor.collected_sql, ["SET LOCAL lock_timeout = '5s';"] * 2)
        with connection.schema_editor(collect_sql=True, atomic=False) as editor:
            LockTimeout('750ms').database_forwards('mall', editor, None, None)
        self.assertEqual(editor.collected_sql, ["SET lock_timeout = '750ms';"])
        with connection.schema_editor(atomic=True) as editor:
            operation.database_forwards('mall', editor, None, None)
            with connection.cursor() as cursor:
                cursor.execute('SHOW lock_timeout')
                self.assertEqual(cursor.fetchone()[0], '5s')

    def test_only_a_postgresql_duration_is_accepted_and_migrations_can_name_it(self):
        for value in ['5s', '500ms', '1min', 2000]:
            LockTimeout(value)
        for value in ["5s'; DROP TABLE mall_part; --", '5 s', '', 'forever']:
            with self.assertRaises(ValueError):
                LockTimeout(value)
        source, imports = OperationWriter(LockTimeout('5s'), indentation=0).serialize()
        self.assertEqual((source, imports), ("mall.refactor.migration_ops.LockTimeout(\n    timeout='5s',\n),", {'import mall.refactor.migration_ops'}))
