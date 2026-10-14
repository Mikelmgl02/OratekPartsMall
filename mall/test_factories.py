"""Shared test factories and helpers for the inventory refactor (plan §1.4).

The factories speak the target's terms (supplier, line, reference, variant, library number) and build today's rows through the paths
the app itself uses. Each stage of the refactor changes these functions, not the hundreds of tests built on them:

- make_line: an OEM line, today a catalog Part.
- make_reference: one of the line's codes, a Reference (PartCode until S2a); tipo oem, aftermarket, interno or otro, ref_type in step.
- make_variant: a supplier's item, today a SupplierItem loaded through ingest_inventory like an upload.
- make_library_number: a number of the OEM library, today an OEMReference with one source.
- make_account, make_supplier, make_client: accounts with one role and their members.

historical_apps() is the app registry as of one migration, for tests that run a data migration's functions: the live registry carries
today's models, which the refactor keeps changing, while a migration only ever sees the models of its own time.
"""
import importlib
from functools import lru_cache
from types import SimpleNamespace

from django.db import connection
from django.db.migrations.loader import MigrationLoader

from . import catalog_suffixes
from .models import Account, Membership, Part, Reference, Role, User
from .oem_reference import upsert_reference
from .services import ingest_inventory

PASSWORD = 'Example938!'
# The target's Reference tipo -> the legacy ref_type Reference.save keeps in step until S10.
REF_TYPES = {'oem': 'oem', 'aftermarket': 'company', 'interno': 'unknown', 'otro': 'unknown'}


def make_user(username, *, password=PASSWORD, superuser=False):
    create = User.objects.create_superuser if superuser else User.objects.create_user
    return create(username, f'{username}@example.invalid', password)


def make_account(name, role, *members):
    """An active account with one role (a Role code: client_business, supplier_retail...) and its members (staff permission)."""
    account = Account.objects.create(name=name)
    account.roles.add(Role.objects.get(code=role))
    for user in members:
        Membership.objects.create(user=user, account=account)
    return account


def make_supplier(name, *members, role='supplier_retail'):
    return make_account(name, role, *members)


def make_client(name, *members, role='client_business'):
    return make_account(name, role, *members)


def make_line(sku, **fields):
    """An OEM line under its codigo (today Part.sku); other Part fields pass through."""
    return Part.objects.create(sku=sku, **fields)


def make_catalog(rows):
    """{sku: line} for (sku, description) rows, each named after its SKU (the OEM finder's catalogs)."""
    return {sku: make_line(sku, name=sku, description=description) for sku, description in rows}


def make_reference(line, code, *, brand='', tipo='otro', source='', **fields):
    """One of the line's codes, written as it is (no key check: a test may need a clash). brand '' means any brand; source is where
    the equivalence came from (reference_source); estado, supplier... pass through."""
    return Reference.objects.create(part=line, code=code, brand=brand, tipo=tipo, ref_type=REF_TYPES[tipo], reference_source=source, **fields)


def make_variant(supplier, invent_id, code, quantity=0, *, actor=None, brand='', description='', line=None, **data):
    """A supplier's item loaded like an upload (update seed-<invent_id>), so matching, the ledger and the signals run as in the app.
    line: put it on that line as matched afterwards, whatever matching decided."""
    item = ingest_inventory(supplier=supplier, actor=actor, data={
        'update_id': f'seed-{invent_id}', 'supplier_invent_id': invent_id, 'codigo': code, 'brand': brand, 'description': description,
        'source': 'upload', 'quantity': quantity, **data})
    if line is not None:
        item.part, item.matching_status = line, 'matched'
        item.save(update_fields=['part', 'matching_status'])
    return item


def make_library_number(manufacturer, code, *, source_kind='manual', citation='', **fields):
    """A number of the OEM library with one source (upsert_reference: printed, detail, actor, defaults pass through)."""
    return upsert_reference(manufacturer, code, source_kind=source_kind, citation=citation, **fields)[0]


class Fresh:
    """Drops the cached code suffix table around each test, so one test's suffix edits never reach the next."""
    def setUp(self):
        catalog_suffixes.invalidate()
        super().setUp()

    def tearDown(self):
        catalog_suffixes.invalidate()
        super().tearDown()


@lru_cache(maxsize=None)
def historical_apps(migration, app='mall'):
    """The models as they were right after `migration` (its full name: '0039_oem_reference'), reading the current test database."""
    return MigrationLoader(connection, ignore_no_migrations=True).project_state((app, migration)).apps


def migration_module(migration, app='mall'):
    return importlib.import_module(f'{app}.migrations.{migration}')


def run_migration_function(migration, name, app='mall'):
    """Call a migration's RunPython function (forwards or backwards) the way migrate does: historical models, default database."""
    return getattr(migration_module(migration, app), name)(historical_apps(migration, app), SimpleNamespace(connection=connection))
