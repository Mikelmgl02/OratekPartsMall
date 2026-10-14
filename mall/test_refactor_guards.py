"""Guards the inventory refactor keeps on (plan §1.4): the models equal the migrations, migrations import no app code, and the test
settings keep the outside services off, guard the network and never drop a real database. The later stages add theirs (Part filters on estado outside lines.py, Reference deletes outside
references.py, new PartCode uses, bulk writes outside the choke points)."""
import ast
import io
import socket
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from config import network_guard
from config.test_runner import unsafe_test_name

MIGRATIONS = Path(__file__).resolve().parent / 'migrations'


class MigrationGuardTests(TestCase):
    def test_the_models_match_the_migrations(self):
        """Another session's makemigrations must not absorb a stage's half-landed models, nor a stage land models its migrations lack."""
        out = io.StringIO()
        try:
            call_command('makemigrations', 'mall', check=True, dry_run=True, stdout=out, stderr=out)
        except SystemExit:
            self.fail(f'Hay cambios de modelos sin migración (makemigrations --check):\n{out.getvalue()}')


class MigrationImportTests(SimpleTestCase):
    # What a migration may import from the app: the upload storage of 0019's and 0057's file fields (the refactor leaves
    # mall.media_storage alone) and the lock timeout every expand, tighten and contract migration of the refactor starts with (plan
    # §1.2). Both may import only Django, which the second test holds them to.
    ALLOWED = {'mall.media_storage', 'mall.refactor.migration_ops'}

    def imported(self, path, package):
        """The app modules a file imports, relative imports resolved against its package; `from mall.refactor import migration_ops`
        counts as mall.refactor.migration_ops."""
        found = []
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                found.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                parts = package.split('.')
                base = '.'.join([*parts[:len(parts) - node.level + 1], node.module or ''] if node.level else [node.module or '']).strip('.')
                names = [f'{base}.{alias.name}' for alias in node.names]
                found.extend(names if base not in self.ALLOWED and all(name in self.ALLOWED for name in names) else [base])
        return [module for module in found if module.split('.')[0] == 'mall']

    def test_migrations_import_no_app_code(self):
        """The test database replays every migration, so one that imports a module the refactor rewrites stops it from building."""
        found = {path.name: [m for m in self.imported(path, 'mall.migrations') if m not in self.ALLOWED]
                 for path in sorted(MIGRATIONS.glob('[0-9]*.py'))}
        self.assertEqual({name: modules for name, modules in found.items() if modules}, {})

    def test_what_migrations_may_import_imports_only_django(self):
        for module in sorted(self.ALLOWED):
            path = MIGRATIONS.parent.parent / (module.replace('.', '/') + '.py')
            if path.exists():  # migration_ops comes with S0.11
                with self.subTest(module):
                    self.assertEqual(self.imported(path, module.rpartition('.')[0]), [])


class NetworkGuardTests(SimpleTestCase):
    def test_only_private_and_loopback_addresses_count_as_private(self):
        for host in ('127.0.0.1', '10.4.0.7', '172.18.0.5', '192.168.1.20', '::1', 'fd00::5'):
            self.assertTrue(network_guard.private(host), host)
        for host in ('104.20.29.242', '172.66.165.132', '8.8.8.8', '2606:4700::6810:1df2'):
            self.assertFalse(network_guard.private(host), host)

    def test_a_public_connection_is_refused_before_it_opens_and_reported_once(self):
        opened = []
        connect = network_guard._guarded(lambda sock, address: opened.append(address))
        inet, unix, stderr = SimpleNamespace(family=socket.AF_INET), SimpleNamespace(family=socket.AF_UNIX), io.StringIO()
        with patch.object(network_guard, 'blocked', []) as blocked, patch.object(network_guard, '_reported', set()), redirect_stderr(stderr):
            for _ in range(2):
                with self.assertRaises(ConnectionRefusedError):
                    connect(inet, ('104.20.29.242', 443))
            connect(inet, ('10.4.0.7', 5432))
            connect(unix, '/run/postgresql/.s.PGSQL.5432')
        self.assertEqual(opened, [('10.4.0.7', 5432), '/run/postgresql/.s.PGSQL.5432'])
        self.assertEqual(blocked, [('104.20.29.242', 443)] * 2)
        self.assertEqual(stderr.getvalue().count('104.20.29.242:443'), 1)

    def test_the_outside_services_are_blanked_before_the_settings_read_them(self):
        environ = {'RESEND_API_KEY': 're_live', 'DO_SPACES_BUCKET': 'bucket', 'REDIS_URL': 'redis://redis:6379/0', 'POSTGRES_DB': 'partsmall'}
        self.assertEqual(network_guard.blank_outside_services(environ), ['RESEND_API_KEY', 'DO_SPACES_BUCKET', 'REDIS_URL'])
        self.assertEqual({name: environ[name] for name in network_guard.OUTSIDE_SERVICES}, dict.fromkeys(network_guard.OUTSIDE_SERVICES, ''))
        self.assertEqual(environ['POSTGRES_DB'], 'partsmall')


class TestDatabaseNameTests(SimpleTestCase):
    def test_the_runner_refuses_a_test_database_name_that_could_be_a_real_database(self):
        for name in ('test_partsmall_isolated', 'test_partsmall_s0x', None):
            self.assertIsNone(unsafe_test_name({'NAME': 'partsmall', 'TEST': {'NAME': name}}, live='partsmall'), name)
        for name, configured in (('partsmall', 'partsmall'), ('partsmall_rehearsal', 'partsmall'), ('test_copy', 'test_copy'),
                                 ('test_live', 'other')):
            self.assertIn('debe empezar por test_', unsafe_test_name({'NAME': configured, 'TEST': {'NAME': name}}, live='test_live'), name)

