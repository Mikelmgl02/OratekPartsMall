"""Shared test factories and helpers for the inventory refactor (plan §1.4).

The factories speak the target's terms (supplier, line, reference, variant, library number) and build today's rows through the paths
the app itself uses. Each stage of the refactor changes these functions, not the hundreds of tests built on them:

- make_line: an OEM line, today a catalog Part.
- make_reference: one of the line's codes, a Reference (PartCode until S2a); tipo oem, aftermarket, interno or otro, ref_type in step.
- make_variant: a supplier's item, today a SupplierItem loaded through ingest_inventory like an upload.
- make_library_number: a number of the OEM library: from S4 a Reference (tipo oem, its manufacturer as brand) on the line that carries
  it or on a line of its own in referencia, with one source (mall.references.add_library_number).
- make_legacy_number: a number of the OEM table as it was before S4 (OEMReference), for S4's backfill and the tools before it.
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
from . import references
from .models import Account, Membership, Part, Reference, Role, User
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


def make_library_number(manufacturer, code, *, source_kind='manual', citation='', detail=None, defaults=None, **fields):
    """A number of the OEM library with one source, its Reference (mall.references.add_library_number: printed, actor, description,
    part_type and applications pass through; detail is the source's detail, defaults the texts as the OEM table's upsert took them)."""
    texts = {key: value for key, value in (defaults or {}).items() if key in ('description', 'part_type', 'applications') and value}
    return references.add_library_number(manufacturer, code, source_kind=source_kind, citation=citation, source_detail=detail,
                                         **texts, **fields)[0]


def make_legacy_number(manufacturer, code, *, source_kind='manual', citation='', detail=None, status=None, **fields):
    """A number of the OEM table as it was before S4 (OEMReference with one OEMReferenceSource), which S4's backfill turns into a
    Reference and the tools that ran before it (S0, S2b) still read. fields: printed_forms, description, part_type, applications..."""
    from .oem_reference import derived_status
    from .oem_reference_models import OEMReference, OEMReferenceSource
    ref = OEMReference.objects.create(manufacturer=manufacturer, code=code, printed_forms=fields.pop('printed_forms', [code]),
                                      status=status or derived_status([(source_kind, detail or {})]), **fields)
    OEMReferenceSource.objects.create(reference=ref, kind=source_kind, citation=citation, detail=detail or {})
    return ref


def legacy_mirror(ref):
    """What the OEM table's mirror (the signal handlers before S4) made of an OEM alterno: the OEMReference (manufacturer = its brand,
    the compact code) with the source its reference_source names, the alterno listed in that source's part_codes, and the alterno's
    oem_reference pointing at it. For the tests of the tools that run on the library as it was before S4 (S2a, S2b, S0)."""
    from .oem_reference import classify_number, derived_status, part_defaults, source_of
    from .oem_reference_models import OEMReference, OEMReferenceSource, compact
    kind, citation, detail = source_of(ref.reference_source)
    texts = {key: value for key, value in part_defaults(Part.objects.filter(pk=ref.part_id).values_list('description', flat=True).first()).items() if value}
    system, family = classify_number(ref.brand, compact(ref.code), [ref.code])
    number, _ = OEMReference.objects.get_or_create(manufacturer=ref.brand, code=compact(ref.code), defaults={
        'printed_forms': [ref.code], 'system': system, 'family': family, **texts})
    source, created = OEMReferenceSource.objects.get_or_create(reference=number, kind=kind, citation=citation, defaults={'detail': detail})
    entries = {entry['id']: entry for entry in source.detail.get('part_codes') or []}
    entries[ref.pk] = {'id': ref.pk, 'part': str(ref.part_id), 'code': ref.code}
    source.detail = {**source.detail, 'part_codes': sorted(entries.values(), key=lambda entry: entry['id'])}
    source.save(update_fields=['detail'])
    if number.status != 'disputed':
        number.status = derived_status(number.sources.values_list('kind', 'detail'))
        number.save()
    Reference.all_objects.filter(pk=ref.pk).update(oem_reference=number)
    return number


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


VARIANT_CONSTRAINTS = ('variant_line_follows_reference', 'variant_reference_iff_line', 'variant_unattached_is_pending')  # 0078's


def drop_variant_constraints():
    """0078's constraints, inside a test's transaction (they come back when it rolls back): the state before S3's tighten."""
    with connection.cursor() as cursor:
        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')
        present = connection.introspection.get_constraints(cursor, 'mall_supplieritem')
        for name in VARIANT_CONSTRAINTS:
            if name in present:
                cursor.execute(f'ALTER TABLE mall_supplieritem DROP CONSTRAINT {name}')


def unhook_variants(refs=None):
    """The state before S3 for a test of an earlier stage: no supplier item hangs from a Reference (of refs, or any); 0078's constraints
    are dropped first, in the test's transaction, since an item on a line without its Reference breaks them."""
    from .models import SupplierItem
    drop_variant_constraints()
    rows = SupplierItem.objects.filter(reference__isnull=False)
    if refs is not None:
        rows = rows.filter(reference_id__in=[getattr(ref, 'pk', ref) for ref in refs])
    return rows.update(reference=None)


def move_variant(item, line, **values):
    """Put a supplier item on another line, or on none, by queryset, as a concurrent change would (no save(), updated_at untouched),
    keeping S3's constraints: on a line it hangs from that line's Reference for its code (mall.variants.reference_for, made when
    missing); on none it holds no Reference. values: other columns written in the same UPDATE."""
    from . import variants
    from .models import SupplierItem
    pk = getattr(item, 'pk', item)
    if line is None:
        return SupplierItem.objects.filter(pk=pk).update(part=None, reference=None, **{'matching_status': 'pending', **values})
    row = SupplierItem.objects.get(pk=pk)
    ref, _ = variants.reference_for(line, values.get('codigo', row.codigo), values.get('brand', row.brand), row.supplier_id, create=True, item=row)
    return SupplierItem.objects.filter(pk=pk).update(part=line, reference=ref, **values)
