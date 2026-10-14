"""Guards the inventory refactor keeps on (plan §1.4): the models equal the migrations, migrations import no app code, and the test
settings' network guard holds. The later stages add theirs (Part filters on estado outside lines.py, Reference deletes outside
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
    # The upload storage of 0019's and 0057's file fields; the refactor leaves mall.media_storage alone.
    ALLOWED = {'mall.media_storage'}

    def test_migrations_import_no_app_code(self):
        """The test database replays every migration, so one that imports a module the refactor rewrites stops it from building."""
        found = {}
        for path in sorted(MIGRATIONS.glob('[0-9]*.py')):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ImportFrom):
                    modules = ['.' * node.level + (node.module or '')]
                elif isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                else:
                    continue
                found.setdefault(path.name, []).extend(m for m in modules if (m.startswith('.') or m.split('.')[0] == 'mall') and m not in self.ALLOWED)
        self.assertEqual({name: modules for name, modules in found.items() if modules}, {})


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
