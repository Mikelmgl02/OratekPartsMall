"""Guards the inventory refactor keeps on (plan §1.4): the models equal the migrations, migrations import no app code, the test
settings keep the outside services off, guard the network and never drop a real database, and (S1) every Part.objects filter on
active, merged_into or the SKU goes through mall.lines. The later stages add theirs (Reference deletes outside references.py, new
PartCode uses, bulk writes outside the choke points)."""
import ast
import io
import re
import socket
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from config import network_guard
from config.test_runner import unsafe_test_name

from .refactor import output

APP = Path(__file__).resolve().parent
MIGRATIONS = APP / 'migrations'


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


class OutputTests(SimpleTestCase):
    def test_a_manifest_goes_to_the_main_checkout_or_to_the_rehearsals_folder(self):
        with patch.dict('os.environ', {'REFACTOR_OUTPUT': ''}):
            with override_settings(MAIN_CHECKOUT=None, BASE_DIR=Path('/app/.claude/worktrees/refactor-s1')):
                self.assertEqual(output.manifest_path('s1'), Path('/app/output/refactor/s1/manifest.jsonl'))
            with override_settings(MAIN_CHECKOUT=None, BASE_DIR=Path('/app')):
                self.assertEqual(output.stage_dir('s2a'), Path('/app/output/refactor/s2a'))
        with patch.dict('os.environ', {'REFACTOR_OUTPUT': '/app/output/refactor/s1/rehearsal-20261013-120000/output-refactor/s1'}):
            self.assertEqual(output.manifest_path('s1'), Path('/app/output/refactor/s1/rehearsal-20261013-120000/output-refactor/s1/manifest.jsonl'))


# --- S1: Part readers go through mall.lines ------------------------------------------------------------------------------------------

LIFECYCLE_LOOKUPS = {'active', 'merged_into__isnull', 'sku', 'sku__in', 'sku__iexact', 'sku__iregex'}
QUERY_METHODS = {'filter', 'exclude', 'get', 'get_or_create', 'update_or_create'}
SHORTCUTS = {'get_object_or_404', 'get_list_or_404'}
REASON = re.compile(r'#\s*estado:\s*\S')


def part_manager(node):
    """Whether an expression is a chain that starts at Part.objects (Part.objects.select_for_update().filter(...) included)."""
    while isinstance(node, (ast.Call, ast.Attribute, ast.Subscript)):
        if isinstance(node, ast.Attribute) and node.attr == 'objects':
            base = node.value
            return (isinstance(base, ast.Name) and base.id == 'Part') or (isinstance(base, ast.Attribute) and base.attr == 'Part')
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def lookups(call):
    """The lookups a query call names: its keyword arguments (not defaults) and those of every Q() among its arguments."""
    found = {keyword.arg for keyword in call.keywords if keyword.arg not in (None, 'defaults', 'create_defaults')}
    for argument in call.args:
        for node in ast.walk(argument):
            if isinstance(node, ast.Call) and getattr(node.func, 'id', getattr(node.func, 'attr', None)) == 'Q':
                found |= {keyword.arg for keyword in node.keywords if keyword.arg}
    return found


def part_filters(source):
    """[(line, lookups)] for each Part.objects query (filter, exclude, get...; get_object_or_404 on Part) in source that filters on
    active, merged_into__isnull or the SKU and whose lines carry no '# estado: <reason>'."""
    rows, text = [], source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in QUERY_METHODS and part_manager(func.value):
            names = lookups(node)
        elif getattr(func, 'id', None) in SHORTCUTS and node.args and (
                (isinstance(node.args[0], ast.Name) and node.args[0].id == 'Part') or part_manager(node.args[0])):
            names = lookups(node)  # a Part.objects queryset given to it is a query call of its own
        else:
            continue
        banned = names & LIFECYCLE_LOOKUPS
        if banned and not any(REASON.search(line) for line in text[node.lineno - 1:node.end_lineno]):
            rows.append((node.lineno, sorted(banned)))
    return sorted(rows)


def app_modules():
    """The app's modules a reader can live in: tests, migrations and mall.lines itself left out."""
    for path in sorted(APP.rglob('*.py')):
        relative = path.relative_to(APP)
        if not (relative.name.startswith('test_') or relative.name == 'tests.py' or 'migrations' in relative.parts or relative == Path('lines.py')):
            yield relative, path


class LineReaderGuardTests(SimpleTestCase):
    def test_part_readers_take_their_lines_from_mall_lines(self):
        """A Part.objects filter on active, merged_into__isnull or the SKU decides which estados it reads without saying so: it goes
        through mall.lines (storefront_lines, catalog_lines, all_lines, lines_by_codigo, detail_line), or its line says why not with
        '# estado: <reason>'. Otherwise S4's referencia lines and S6's lines in preparación reach it unasked."""
        modules = list(app_modules())
        self.assertGreater(len(modules), 100)
        found = {f'{relative}:{line}': names for relative, path in modules for line, names in part_filters(path.read_text(encoding='utf-8'))}
        self.assertEqual(found, {}, 'Filtros de Part fuera de mall.lines (usa sus querysets o anota la línea con # estado: <motivo>).')

    def test_the_scan_finds_each_way_of_filtering_parts(self):
        source = """
Part.objects.filter(active=True, merged_into__isnull=True)
Part.objects.select_for_update().filter(pk=pk).exclude(sku__in=codes)
models.Part.objects.get(sku=code)
Part.objects.filter(Q(sku__iexact=code) | Q(pk=pk))
get_object_or_404(Part, pk=pk, merged_into__isnull=True)
get_object_or_404(Part.objects.filter(sku__iregex=rx), pk=pk)
Part.objects.filter(
    active=True,
    pk=pk)
Part.objects.filter(sku=code)  # estado: every estado holds its codigo until S4
Part.objects.filter(pk=pk, description='')
Part.objects.filter(pk=pk).update(active=False, merged_into=target)
Part.objects.get_or_create(pk=pk, defaults={'sku': code})
storefront_lines().filter(sku__in=codes)
SupplierItem.objects.filter(part__active=True, active=True)
get_object_or_404(storefront_lines(), pk=pk)
"""
        self.assertEqual(part_filters(source), [
            (2, ['active', 'merged_into__isnull']), (3, ['sku__in']), (4, ['sku']), (5, ['sku__iexact']), (6, ['merged_into__isnull']),
            (7, ['sku__iregex']), (8, ['active'])])
