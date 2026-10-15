"""Guards the inventory refactor keeps on (plan §1.4): the models equal the migrations, migrations import no app code, the test
settings keep the outside services off, guard the network and never drop a real database, (S1) every Part.objects filter on active,
merged_into or the SKU goes through mall.lines, and (S2a) nothing deletes a Reference outside mall.references, no new code names
PartCode or reaches codes through a codes__ join outside the reviewed allowlist, no bulk write of lines or References happens
outside their choke points (mall.lines, mall.references), (S5) a subgrupo's grupo is read by its FK, not by its text, and (S3) a
variant's line and Reference are written by mall.variants."""
import ast
import io
import re
import socket
from collections import Counter
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
    # mall.media_storage alone), the lock timeout every expand, tighten and contract migration of the refactor starts with (plan
    # §1.2) and the refactor's output folder, where 0088 and 0089 (S10) find the export. They may import only Django, which the
    # second test holds them to.
    ALLOWED = {'mall.media_storage', 'mall.refactor.migration_ops', 'mall.refactor.output'}

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


# --- S2a: References are written by mall.references, never deleted, and PartCode is a name of the past ------------------------------

REFERENCE_NAMES = {'PartCode', 'Reference'}
REFERENCE_RELATIONS = {'codes', 'alternos', 'reemplaza', 'internal_references', 'all_objects'}
# The app modules that reach a line's codes through codes__ (a join reaches retired rows, so each one names the live estados:
# mall.references.live_q; a prefetch_related('codes__sources') is no join), as reviewed in S10, which removed the PartCode alias: at
# most this many uses each. New code goes through Reference.objects or a line's codes, which leave retired rows out. catalog_search.py's
# is no join: codes__contains on the Search Document's own codes column (S7); catalog_classification's two and management's one are
# prefetch_related('codes__sources') (the JSON's reference_source, S10).
LEGACY_ALLOWED = {'catalog_assistant.py': 2, 'catalog_classification.py': 4, 'catalog_search.py': 1, 'management.py': 1, 'services.py': 6,
                  'wishlist.py': 4}
BULK_METHODS = {'bulk_create', 'bulk_update'}
BULK_CHOKE_POINTS = {Path('lines.py'), Path('references.py')}
# Bulk writes of lines elsewhere, reviewed: none since S5 moved the catalog assistant's taxonomy writes to mall.lines.bulk_update_lines.
BULK_ALLOWED = {}


def docstrings(tree):
    """The ids of the docstring constants of a module, its classes and functions (their prose may name anything)."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def legacy_uses(source):
    """How many times source names PartCode (a name, an attribute, an import) or a codes__ lookup (a keyword or a string)."""
    tree = ast.parse(source)
    skip, count = docstrings(tree), 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == 'PartCode' or isinstance(node, ast.Attribute) and node.attr == 'PartCode':
            count += 1
        elif isinstance(node, ast.ImportFrom):
            count += sum(1 for alias in node.names if alias.name == 'PartCode')
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and 'codes__' in node.value and id(node) not in skip:
            count += 1
        elif isinstance(node, ast.keyword) and node.arg and 'codes__' in node.arg:
            count += 1
    return count


def chain_names(node):
    """The names and attributes along an expression's chain (Reference.all_objects.filter(...).order_by(...) -> Reference, all_objects,
    filter, order_by)."""
    names = []
    while isinstance(node, (ast.Call, ast.Attribute, ast.Subscript, ast.Name)):
        if isinstance(node, ast.Name):
            names.append(node.id)
            break
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
        node = node.func if isinstance(node, ast.Call) else node.value
    return names


def reference_deletes(source):
    """[line] of each .delete() whose chain starts at Reference/PartCode or passes through a relation to References (codes,
    alternos, reemplaza, internal_references, all_objects)."""
    rows = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'delete':
            names = set(chain_names(node.func.value))
            if names & REFERENCE_NAMES or names & REFERENCE_RELATIONS:
                rows.append(node.lineno)
    return rows


def bulk_writes(source):
    """[line] of each bulk_create or bulk_update on Part, Reference or PartCode."""
    rows = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in BULK_METHODS:
            names = chain_names(node.func.value)
            if names and names[-1] in {'Part', 'Reference', 'PartCode'}:
                rows.append(node.lineno)
    return rows


class ReferenceGuardTests(SimpleTestCase):
    def test_nothing_deletes_a_reference_outside_mall_references(self):
        """A Reference is retired (mall.references.retire), never deleted: Reference.delete and its querysets' delete refuse at run
        time, and this keeps the code from trying. mall.references.purge is the one exception, for the S2a backfill's rollback."""
        found = {f'{relative}:{line}' for relative, path in app_modules() if relative != Path('references.py')
                 for line in reference_deletes(path.read_text(encoding='utf-8'))}
        self.assertEqual(found, set(), 'Borrados de referencias fuera de mall.references (usa references.retire).')

    def test_no_new_code_names_partcode_or_joins_codes(self):
        """PartCode stays an alias of Reference until S10 for the code that already uses it; a codes__ join reads retired rows unless it
        names the live estados. Each module may keep at most the uses reviewed in S2a."""
        counts = {str(relative): legacy_uses(path.read_text(encoding='utf-8')) for relative, path in app_modules()}
        over = {module: count for module, count in counts.items() if count > LEGACY_ALLOWED.get(module, 0)}
        self.assertEqual(over, {}, 'Usos nuevos de PartCode o de codes__ (usa Reference, Reference.objects o mall.references.live_q).')

    def test_bulk_writes_of_lines_and_references_go_through_their_choke_points(self):
        found = Counter(str(relative) for relative, path in app_modules() if relative not in BULK_CHOKE_POINTS
                        for _ in bulk_writes(path.read_text(encoding='utf-8')))
        over = {module: count for module, count in found.items() if count > BULK_ALLOWED.get(module, 0)}
        self.assertEqual(over, {}, 'bulk_create/bulk_update de líneas o referencias fuera de mall.lines y mall.references.')

    def test_the_scans_find_each_form(self):
        source = """
code.delete()
Reference.objects.filter(pk=1).delete()
PartCode.objects.get(pk=1).delete()
part.codes.exclude(pk__in=keep).delete()
Reference.all_objects.filter(estado='retirada').delete()
ref.alternos.all().delete()
PartImage.objects.filter(part=part).delete()
models.PartCode.objects.filter(code=x)
Q(codes__code='X') | Q(part__codes__brand='Y')
values('part__codes__code')
Part.objects.bulk_update(lines, ['name'])
Reference.objects.bulk_create(rows)
PartImage.objects.bulk_update(images, ['position'])
"""
        self.assertEqual(reference_deletes(source), [3, 4, 5, 6, 7])
        self.assertEqual(legacy_uses(source), 5)
        self.assertEqual(bulk_writes(source), [12, 13])


# --- S5: a subgrupo's grupo is read by its FK ------------------------------------------------------------------------------------------

# PartType.category is text that follows the grupo until S10 drops it; mall.technical_data (grupo_nombre, pair_grupo) reads it on purpose.
# Other code reads mall.technical_data.grupo_nombre(part_type) or joins part_type__grupo__nombre.
GRUPO_TEXT_ALLOWED = {Path('technical_data.py')}


def grupo_text_reads(source):
    """[line] of each part_type.category attribute (on any chain ending in part_type) or part_type__category lookup (a keyword or a
    string)."""
    tree = ast.parse(source)
    skip, rows = docstrings(tree), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == 'category' and chain_names(node.value)[:1] == ['part_type']:
            rows.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and 'part_type__category' in node.value and id(node) not in skip:
            rows.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg and 'part_type__category' in node.arg:
            rows.append(node.lineno)
    return sorted(rows)


class GrupoGuardTests(SimpleTestCase):
    def test_a_subgrupos_grupo_is_read_by_its_fk(self):
        """PartType.category leaves in S10 and a grupo's rename reaches it only through Grupo.save: a reader takes the grupo by the FK
        (mall.technical_data.grupo_nombre, part_type__grupo__nombre)."""
        found = {f'{relative}:{line}' for relative, path in app_modules() if relative not in GRUPO_TEXT_ALLOWED
                 for line in grupo_text_reads(path.read_text(encoding='utf-8'))}
        self.assertEqual(found, set(), 'Lecturas del grupo de un subgrupo por su texto (usa technical_data.grupo_nombre o part_type__grupo__nombre).')

    def test_the_scan_finds_each_form(self):
        source = """
part.part_type.category
part_type.category
Part.objects.filter(part_type__category='FRENOS')
values_list('part_type__category', flat=True)
part.category
grupo_nombre(part.part_type)
values_list('part_type__grupo__nombre', flat=True)
"""
        self.assertEqual(grupo_text_reads(source), [2, 3, 4, 5])


# --- S3: a variant's line and Reference are written by mall.variants ---------------------------------------------------------------

VARIANT_FIELDS = {'part', 'part_id', 'reference', 'reference_id'}
VARIANT_WRITERS = {Path('variants.py')}
# Reviewed elsewhere: the S3 backfill's bulk attach and its rollback, and S2b's rollback, which unhooks items before S3's tighten.
VARIANT_ALLOWED = {'refactor/s3_variants.py': 2, 'refactor/s2b_main_refs.py': 2}


def variant_writes(source):
    """[line] of each queryset update or bulk_update of SupplierItem that writes its line or its Reference."""
    rows = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ('update', 'bulk_update')):
            continue
        if 'SupplierItem' not in chain_names(node.func.value):
            continue
        if node.func.attr == 'update':
            hit = any(keyword.arg in VARIANT_FIELDS for keyword in node.keywords)
        else:
            fields = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == 'fields'), None)
            hit = isinstance(fields, (ast.List, ast.Tuple)) and any(isinstance(item, ast.Constant) and item.value in VARIANT_FIELDS
                                                                    for item in fields.elts)
        if hit:
            rows.append(node.lineno)
    return rows


class VariantGuardTests(SimpleTestCase):
    def test_a_variants_line_and_reference_are_written_by_mall_variants(self):
        """SupplierItem.part follows its Reference (I4; from 0078 a composite foreign key): attach, vincular, follow and rehang write
        both together. A queryset update of either elsewhere would leave an item on a line its Reference is not on."""
        found = Counter(str(relative) for relative, path in app_modules() if relative not in VARIANT_WRITERS
                        for _ in variant_writes(path.read_text(encoding='utf-8')))
        over = {module: count for module, count in found.items() if count > VARIANT_ALLOWED.get(module, 0)}
        self.assertEqual(over, {}, 'Escrituras de SupplierItem.part o reference fuera de mall.variants.')

    def test_the_scan_finds_each_form(self):
        source = """
SupplierItem.objects.filter(part_id__in=ids).update(part=target)
m.SupplierItem.objects.filter(pk=pk).update(reference_id=1, part_id=2)
SupplierItem.objects.bulk_update(items, ['reference', 'part'])
SupplierItem.objects.filter(pk=pk).update(reserved_quantity=1)
SupplierItem.objects.bulk_update(items, ['matching_status'])
Part.objects.filter(pk=pk).update(part_type=None)
"""
        self.assertEqual(variant_writes(source), [2, 3, 4])


# --- S10: what the contract retired is no model, field or alias -----------------------------------------------------------------------

class ContractGuardTests(SimpleTestCase):
    def test_the_retired_fields_tables_and_alias_are_gone(self):
        """S10 (0087): Part's name, category, subcategory and legacy_brand, Reference's ref_type, reference_source and oem_reference,
        PartType.category, SkuReviewVerdict.proposal and SearchDocument.reach are no fields (name, category, subcategory and ref_type are
        derived properties), active and is_OEM are generated, the archived tables have no model and PartCode is no name."""
        from django.apps import apps
        from . import models
        from .models import Part, PartType, Reference, SearchDocument, SkuReviewVerdict
        fields = lambda model: {field.name for field in model._meta.get_fields()}
        self.assertFalse(fields(Part) & {'name', 'category', 'subcategory', 'legacy_brand', 'legacy_name', 'legacy_category', 'legacy_subcategory'})
        self.assertFalse(fields(Reference) & {'ref_type', 'reference_source', 'oem_reference', 'legacy_ref_type', 'legacy_reference_source'})
        self.assertFalse(fields(PartType) & {'category', 'legacy_category'})
        self.assertNotIn('proposal', fields(SkuReviewVerdict))
        self.assertNotIn('reach', fields(SearchDocument))
        self.assertTrue(Part._meta.get_field('active').generated and Part._meta.get_field('is_OEM').generated)
        for name in ('SkuProposal', 'SkuProposalItem', 'OEMReference', 'OEMReferenceSource', 'OEMCrossReference', 'OEMCrossReferenceRetired',
                     'PartOEMLink', 'SupplierCodeMapping', 'Alternate'):
            with self.assertRaises(LookupError, msg=name):
                apps.get_model('mall', name)
        self.assertFalse(hasattr(models, 'PartCode'))
