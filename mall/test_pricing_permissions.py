import io
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless

from django.db import IntegrityError, close_old_connections, connection, transaction
from django.http import Http404
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient, APITestCase, APITransactionTestCase

from .models import Account, Membership, Role, User
from .pricing_models import PERMISSION_CHOICES, PricingAuditEvent, SupplierPricingSettings, pricing_settings
from .pricing_preflight import main as preflight_main
from .views import PERMISSION_RANK, membership_for


class PricingPermissionTests(APITestCase):
    def setUp(self):
        self.owner, self.manager, self.staff, self.buyer, self.outsider = [
            User.objects.create_user(name, f'{name}@example.invalid', 'Pricing938!') for name in ['p-owner', 'p-manager', 'p-staff', 'p-buyer', 'p-outsider']]
        self.root = User.objects.create_superuser('p-root', 'p-root@example.invalid', 'Pricing938!')
        self.supplier = self.account('PROVEEDOR PRECIOS', 'supplier_retail', {self.owner: 'owner', self.manager: 'manager', self.staff: 'staff'})
        self.client_account = self.account('CLIENTE PRECIOS', 'client_business', {self.buyer: 'owner'})
        self.other = self.account('OTRO PROVEEDOR', 'supplier_wholesale', {self.outsider: 'owner'})

    def account(self, name, role, members):
        account = Account.objects.create(name=name)
        account.roles.add(Role.objects.get(code=role))
        for user, permission in members.items():
            Membership.objects.create(user=user, account=account, permission=permission)
        return account

    def url(self, account=None):
        return f'/api/v1/accounts/{(account or self.supplier).pk}/pricing/settings/'

    def as_user(self, user):
        self.client.force_authenticate(user)
        return self.client

    def save(self, user, version=1, account=None, **changes):
        return self.as_user(user).post(self.url(account), {'expected_version': version, **changes}, format='json')

    def test_every_supplier_member_reads_defaults_without_creating_rows(self):
        for user, permission in [(self.staff, 'staff'), (self.manager, 'manager'), (self.owner, 'owner')]:
            with self.subTest(permission=permission):
                response = self.as_user(user).get(self.url())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, {
                    'default_currency': 'USD', 'usd_pab_parity': True, 'config_min_permission': 'staff', 'publish_min_permission': 'staff',
                    'over_request_policy': 'confirm', 'over_stock_policy': 'confirm', 'accept_shortfall_policy': 'block',
                    'prefill_quantity': 'requested', 'assistant_enabled': False, 'version': 1, 'updated_at': None, 'updated_by': None,
                    'permission': permission, 'can_configure': True, 'can_manage_permissions': permission == 'owner'})
        self.assertFalse(SupplierPricingSettings.objects.exists())
        self.assertFalse(PricingAuditEvent.objects.exists())

    def test_outsiders_get_404_and_accounts_without_supplier_role_get_403(self):
        inactive = self.account('PROVEEDOR INACTIVO', 'supplier_retail', {self.staff: 'owner'})
        inactive.active = False
        inactive.save()
        cases = [('no miembro', self.outsider, self.supplier, 404), ('superusuario', self.root, self.supplier, 404),
                 ('cuenta inactiva', self.staff, inactive, 404), ('sin rol de proveedor', self.buyer, self.client_account, 403)]
        for label, user, account, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.as_user(user).get(self.url(account)).status_code, expected)
                self.assertEqual(self.save(user, account=account, over_stock_policy='block').status_code, expected)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url()).status_code, 401)
        self.assertEqual(self.client.post(self.url(), {'expected_version': 1}, format='json').status_code, 401)
        self.assertFalse(SupplierPricingSettings.objects.exists())

    def test_config_minimum_gates_writes_and_403_comes_before_409(self):
        raised = self.save(self.owner, config_min_permission='manager')
        self.assertEqual((raised.status_code, raised.data['version'], raised.data['config_min_permission']), (200, 2, 'manager'))
        self.assertFalse(self.as_user(self.staff).get(self.url()).data['can_configure'])
        cases = [('empleado bajo el mínimo', self.staff, 2, 'block', 403), ('empleado y versión vieja', self.staff, 1, 'block', 403),
                 ('administrador', self.manager, 2, 'block', 200), ('administrador y versión vieja', self.manager, 2, 'confirm', 409),
                 ('propietario', self.owner, 3, 'confirm', 200)]
        for label, user, version, policy, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.save(user, version, over_stock_policy=policy).status_code, expected)
        stale = self.save(self.manager, 1, prefill_quantity='available')
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.data['detail'], 'Otro miembro de tu equipo cambió esta configuración. Revisa los valores actuales.')
        self.assertEqual((stale.data['settings']['version'], stale.data['settings']['prefill_quantity']), (4, 'requested'))
        self.assertEqual(SupplierPricingSettings.objects.get().version, 4)
        self.assertEqual(PricingAuditEvent.objects.count(), 3)

    def test_only_owners_change_permissions_and_the_assistant(self):
        for field, value in [('config_min_permission', 'manager'), ('publish_min_permission', 'owner'), ('assistant_enabled', True)]:
            for user in [self.staff, self.manager]:
                with self.subTest(field=field, user=user.username):
                    response = self.save(user, **{field: value})
                    self.assertEqual(response.status_code, 403)
                    self.assertIn('propietario', response.data['detail'])
        unchanged = self.save(self.manager, config_min_permission='staff', publish_min_permission='staff', assistant_enabled=False,
                              over_request_policy='block')
        self.assertEqual((unchanged.status_code, unchanged.data['version']), (200, 2))
        owner = self.save(self.owner, 2, publish_min_permission='manager', assistant_enabled=True)
        self.assertEqual((owner.status_code, owner.data['publish_min_permission'], owner.data['assistant_enabled']), (200, 'manager', True))

    def test_save_is_partial_audited_privately_and_safe_to_retry(self):
        payload = {'default_currency': 'PAB', 'usd_pab_parity': False, 'accept_shortfall_policy': 'allow'}
        first = self.save(self.staff, **payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual({key: first.data[key] for key in [*payload, 'version', 'over_stock_policy']},
                         {**payload, 'version': 2, 'over_stock_policy': 'confirm'})
        self.assertEqual(first.data['updated_by'], {'name': 'p-staff'})
        retry = self.save(self.staff, **payload)
        self.assertEqual((retry.status_code, retry.data['version']), (200, 2))
        event = PricingAuditEvent.objects.get()
        self.assertEqual((event.supplier, event.actor, event.kind, event.client, event.order), (self.supplier, self.staff, 'settings_changed', None, None))
        self.assertEqual(event.payload, {'version': 2, 'old': {'default_currency': 'USD', 'usd_pab_parity': True, 'accept_shortfall_policy': 'block'},
                                         'new': payload})
        self.assertEqual(self.as_user(self.manager).get(self.url()).data['updated_by'], {'name': 'p-staff'})

    def test_invalid_payloads_are_rejected_without_writes(self):
        for payload in [{}, {'expected_version': 0}, {'expected_version': 1, 'default_currency': 'EUR'},
                        {'expected_version': 1, 'over_stock_policy': 'allow'}, {'expected_version': 1, 'accept_shortfall_policy': 'confirm'},
                        {'expected_version': 1, 'prefill_quantity': 'todo'}, {'expected_version': 1, 'config_min_permission': 'root'}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.as_user(self.owner).post(self.url(), payload, format='json').status_code, 400)
        self.assertFalse(SupplierPricingSettings.objects.exists())
        self.assertFalse(PricingAuditEvent.objects.exists())

    def test_database_rejects_unknown_choices(self):
        for field, value in [('default_currency', 'EUR'), ('config_min_permission', 'root'), ('publish_min_permission', ''),
                             ('over_request_policy', 'allow'), ('over_stock_policy', 'x'), ('accept_shortfall_policy', 'confirm'),
                             ('prefill_quantity', 'all')]:
            with self.subTest(field=field), self.assertRaises(IntegrityError), transaction.atomic():
                SupplierPricingSettings.objects.create(supplier=self.supplier, **{field: value})
        self.assertEqual(PERMISSION_CHOICES, Membership._meta.get_field('permission').choices)

    def test_membership_for_ranks_permissions_and_hides_other_accounts(self):
        self.assertEqual(sorted(PERMISSION_RANK, key=PERMISSION_RANK.get), ['staff', 'manager', 'owner'])
        account, membership = membership_for(self.manager, self.supplier.pk, 'supplier', 'manager')
        self.assertEqual((account, membership.permission), (self.supplier, 'manager'))
        self.assertEqual(membership_for(self.owner, self.supplier.pk, 'supplier', 'owner')[1].user, self.owner)
        for user, minimum in [(self.staff, 'manager'), (self.manager, 'owner')]:
            with self.subTest(user=user.username), self.assertRaises(PermissionDenied):
                membership_for(user, self.supplier.pk, 'supplier', minimum)
        with self.assertRaises(PermissionDenied):
            membership_for(self.buyer, self.client_account.pk, 'supplier')
        for user in [self.outsider, self.root]:
            with self.subTest(user=user.username), self.assertRaises(Http404):
                membership_for(user, self.supplier.pk, 'supplier')

    def test_lazy_settings_are_locked_and_created_once_on_write(self):
        self.assertTrue(pricing_settings(self.supplier)._state.adding)
        self.assertFalse(SupplierPricingSettings.objects.exists())
        with transaction.atomic():
            row = pricing_settings(self.supplier, lock=True)
            again = pricing_settings(self.supplier, lock=True)
        self.assertEqual((row.pk, again.pk, row.version), (self.supplier.pk, self.supplier.pk, 1))
        self.assertEqual(SupplierPricingSettings.objects.count(), 1)

    def test_account_list_exposes_only_the_callers_own_permission_with_constant_queries(self):
        def accounts():
            with CaptureQueriesContext(connection) as queries:
                response = self.as_user(self.staff).get('/api/v1/accounts/')
            self.assertEqual(response.status_code, 200)
            return response.data['results'], len(queries)
        rows, small = accounts()
        self.assertEqual([(row['name'], row['permission']) for row in rows], [('PROVEEDOR PRECIOS', 'staff')])
        self.assertEqual(set(rows[0]), {'id', 'name', 'roles', 'capabilities', 'permission'})
        for index in range(3):
            self.account(f'CUENTA EXTRA {index}', 'client_business', {self.staff: ['owner', 'manager', 'staff'][index], self.owner: 'owner'})
        rows, large = accounts()
        self.assertEqual(large, small)
        self.assertEqual({row['name']: row['permission'] for row in rows},
                         {'PROVEEDOR PRECIOS': 'staff', 'CUENTA EXTRA 0': 'owner', 'CUENTA EXTRA 1': 'manager', 'CUENTA EXTRA 2': 'staff'})


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
class ConcurrentPricingSettingsTests(APITransactionTestCase):
    def test_parallel_first_saves_create_one_row_and_never_lose_an_update(self):
        supplier = Account.objects.create(name='PROVEEDOR CONCURRENTE')
        supplier.roles.add(Role.objects.get_or_create(code='supplier_retail', defaults={'name': 'PROVEEDOR', 'capability': 'supplier'})[0])
        users = [User.objects.create_user(f'p-concurrent-{index}', f'p-concurrent-{index}@example.invalid', 'Pricing938!') for index in range(2)]
        for user in users:
            Membership.objects.create(user=user, account=supplier, permission='staff')
        url = f'/api/v1/accounts/{supplier.pk}/pricing/settings/'
        def save(args):
            user, changes = args
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(User.objects.get(pk=user.pk))
                return client.post(url, {'expected_version': 1, **changes}, format='json').status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(save, [(users[0], {'over_stock_policy': 'block'}), (users[1], {'prefill_quantity': 'available'})]))
        self.assertEqual(sorted(statuses), [200, 409])
        row = SupplierPricingSettings.objects.get()
        self.assertEqual(row.version, 2)
        self.assertEqual(sorted([row.over_stock_policy == 'block', row.prefill_quantity == 'available']), [False, True])
        self.assertEqual(PricingAuditEvent.objects.count(), 1)


class PricingPreflightTests(APITestCase):
    def test_lists_suppliers_without_owner_or_manager_and_permission_lockouts(self):
        role = Role.objects.get(code='supplier_retail')
        def account(name, members, active=True, roles=(role,)):
            account = Account.objects.create(name=name, active=active)
            account.roles.add(*roles)
            for index, (permission, user_active) in enumerate(members):
                user = User.objects.create_user(f'{name}-{index}'.replace(' ', '-').lower(), f'{name}-{index}@example.invalid'.replace(' ', '-'), 'Pricing938!', is_active=user_active)
                Membership.objects.create(user=user, account=account, permission=permission)
            return account
        staff_only = account('SOLO EMPLEADOS', [('staff', True), ('staff', True)])
        account('CON ADMINISTRADOR', [('staff', True), ('manager', True)])
        locked = account('PROPIETARIO INACTIVO', [('owner', False), ('manager', True)])
        SupplierPricingSettings.objects.create(supplier=locked, config_min_permission='owner')
        account('PROVEEDOR INACTIVO', [('staff', True)], active=False)
        account('CLIENTE SIN ADMINISTRADOR', [('staff', True)], roles=(Role.objects.get(code='client_business'),))
        empty = account('SIN MIEMBROS', [])
        output = io.StringIO()
        self.assertEqual(preflight_main(['--check'], stdout=output), 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], 'Cuentas proveedoras por revisar: 3')
        self.assertEqual(lines[1:], [
            f'- PROPIETARIO INACTIVO ({locked.pk}) · 1 miembro activo · la configuración de precios exige el permiso propietario y ningún miembro activo lo tiene',
            f'- SIN MIEMBROS ({empty.pk}) · 0 miembros activos · sin propietario ni administrador; la configuración de precios exige el permiso empleado y ningún miembro activo lo tiene',
            f'- SOLO EMPLEADOS ({staff_only.pk}) · 2 miembros activos · sin propietario ni administrador'])
        Membership.objects.filter(account__in=[staff_only, locked, empty]).update(permission='owner')
        User.objects.update(is_active=True)
        output = io.StringIO()
        self.assertEqual(preflight_main(['--check'], stdout=output), 1)
        self.assertIn('SIN MIEMBROS', output.getvalue())
        empty.active = False
        empty.save()
        output = io.StringIO()
        self.assertEqual(preflight_main(['--check'], stdout=output), 0)
        self.assertEqual(output.getvalue().strip(), 'Todas las cuentas proveedoras activas tienen un propietario o administrador.')
