from datetime import timedelta

from django.contrib.auth.models import Permission
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from .models import Account, Alternate, Invitation, Membership, Part, PartCode, Role, StockEntry, SupplierItem, User
from .services import ingest_inventory


class ManagementTests(APITestCase):
    def setUp(self):
        self.root = User.objects.create_superuser('root', 'root@example.com', 'SecureRoot938!')
        self.user = User.objects.create_user('employee', 'employee@example.com', 'SecureEmployee938!')
        self.account = Account.objects.create(name='Empresa de prueba')
        self.account.roles.add(Role.objects.get(code='supplier_retail'))
        self.member = Membership.objects.create(user=self.user, account=self.account, permission='owner')
        self.invitation = Invitation.objects.create(email='new@example.com', created_by=self.root, expires_at=timezone.now() + timedelta(days=7))
        self.client.force_authenticate(self.root)

    def test_non_superusers_are_denied_every_management_operation(self):
        part = Part.objects.create(sku='ORIGINAL', name='Original')
        alternate = PartCode.objects.create(part=part, code='ALTERNO')
        item = SupplierItem.objects.create(supplier=self.account, supplier_invent_id='locked', codigo='TEST', brand='TEST', source='upload')
        operations = [
            ('get', 'catalog/', None), ('get', 'inventory/', None), ('get', 'alternates/', None),
            ('post', 'catalog/', {'name': 'Denied', 'brand': 'TEST'}),
            ('get', f'catalog/{part.pk}/', None), ('patch', f'catalog/{part.pk}/', {'active': False}),
            ('get', f'inventory/{item.pk}/', None), ('patch', f'inventory/{item.pk}/', {'part': str(part.pk), 'matching_status': 'matched'}),
            ('post', 'alternates/', {'part': str(part.pk), 'code': 'Denied'}),
            ('get', f'alternates/{alternate.pk}/', None), ('patch', f'alternates/{alternate.pk}/', {'code': 'Denied'}),
            ('delete', f'alternates/{alternate.pk}/', None),
            ('get', 'accounts/', None), ('post', 'accounts/', {'name': 'Denied'}),
            ('get', f'accounts/{self.account.pk}/', None), ('patch', f'accounts/{self.account.pk}/', {'active': False}),
            ('get', 'users/', None), ('get', f'users/{self.user.pk}/', None),
            ('patch', f'users/{self.user.pk}/', {'is_active': False}), ('get', 'roles/', None),
            ('post', 'memberships/', {'user': self.root.pk, 'account': str(self.account.pk)}),
            ('patch', f'memberships/{self.member.pk}/', {'permission': 'manager'}),
            ('delete', f'memberships/{self.member.pk}/', None),
            ('get', 'invitations/', None), ('post', 'invitations/', {'email': 'leak@example.com'}),
            ('patch', f'invitations/{self.invitation.pk}/revoke/', {}),
        ]
        for is_staff in [False, True]:
            self.user.is_staff = is_staff
            self.user.save()
            if is_staff:
                self.user.user_permissions.set(Permission.objects.all())
            self.client.force_authenticate(self.user)
            for method, route, data in operations:
                with self.subTest(staff=is_staff, method=method, route=route):
                    kwargs = {'data': data, 'format': 'json'} if data is not None else {}
                    response = getattr(self.client, method)('/api/v1/management/' + route, **kwargs)
                    self.assertEqual(response.status_code, 403)
        self.account.refresh_from_db()
        self.assertTrue(self.account.active)
        self.assertTrue(Membership.objects.filter(pk=self.member.pk).exists())
        part.refresh_from_db()
        self.assertTrue(part.active)
        self.assertTrue(PartCode.objects.filter(pk=alternate.pk).exists())

    def test_catalog_creation_normalizes_codes_and_updates_without_changing_identity(self):
        created = self.client.post('/api/v1/management/catalog/', {'sku': ' filtro-001 ', 'name': 'Filtro de aceite', 'description': 'Aplicación: vehículo diésel', 'codes': [{'brand': ' acme ', 'code': ' abc-123 '}, {'brand': 'ACME', 'code': 'SECOND'}]}, format='json')
        self.assertEqual(created.status_code, 201)
        part_id = created.data['id']
        self.assertEqual(created.data['stock_record_count'], 0)
        self.assertEqual(created.data['codes'][0], {'brand': 'ACME', 'code': 'ABC-123', 'kind': 'alias', 'ref_type': 'unknown', 'reference_source': ''})
        part = Part.objects.get(pk=part_id)
        self.assertEqual(part.name, 'FILTRO DE ACEITE')
        self.assertEqual(part.sku, 'FILTRO-001')
        self.assertNotIn('brand', created.data)
        self.assertEqual(part.description, 'APLICACIÓN: VEHÍCULO DIÉSEL')
        code_id = part.codes.get(code='ABC-123').pk
        item = ingest_inventory(supplier=self.account, actor=self.user, data={'update_id': 'first', 'supplier_invent_id': 'stable', 'codigo': 'ABC-123', 'brand': 'ACME', 'source': 'upload', 'quantity': 8})
        self.assertEqual(item.part_id, part.pk)
        updated = self.client.patch(f'/api/v1/management/catalog/{part_id}/', {'name': 'Filtro actualizado', 'active': False, 'codes': [{'brand': 'ACME', 'code': 'ABC-123'}, {'brand': 'OTHER', 'code': 'NEW'}]}, format='json')
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data['id'], part_id)
        self.assertEqual(updated.data['name'], 'FILTRO ACTUALIZADO')
        self.assertEqual(updated.data['stock_record_count'], 1)
        self.assertEqual(part.codes.get(code='ABC-123').pk, code_id)
        self.assertFalse(part.codes.filter(code='SECOND').exists())
        item.refresh_from_db()
        self.assertEqual(item.part_id, part.pk)
        self.assertEqual(item.reported_quantity, 8)
        self.assertEqual(item.ledger.count(), 1)
        self.client.patch(f'/api/v1/management/catalog/{part_id}/', {'description': 'Solo descripción'}, format='json')
        part.refresh_from_db()
        self.assertEqual(part.description, 'SOLO DESCRIPCIÓN')
        self.assertEqual(part.codes.count(), 2)

    def test_duplicate_catalog_codes_are_rejected_without_partial_writes(self):
        part = Part.objects.create(sku='ORIGINAL', name='Original')
        PartCode.objects.create(part=part, brand='ACME', code='ABC')
        payload = {'sku': 'DUPLICADO', 'name': 'Duplicado', 'codes': [{'brand': 'acme', 'code': 'abc'}]}
        self.assertEqual(self.client.post('/api/v1/management/catalog/', payload, format='json').status_code, 400)
        self.assertEqual(Part.objects.count(), 1)
        payload['codes'] = [{'brand': 'NEW', 'code': 'CODE'}, {'brand': ' new ', 'code': ' code '}]
        self.assertEqual(self.client.post('/api/v1/management/catalog/', payload, format='json').status_code, 400)
        other = Part.objects.create(sku='OTRO', name='Otro')
        response = self.client.patch(f'/api/v1/management/catalog/{other.pk}/', {'name': 'No guardar', 'codes': [{'brand': 'NEW', 'code': 'NEW'}, {'brand': 'ACME', 'code': 'ABC'}]}, format='json')
        self.assertEqual(response.status_code, 400)
        other.refresh_from_db()
        self.assertEqual(other.name, 'Otro')
        self.assertFalse(other.codes.exists())

    def test_brand_neutral_sku_is_unique_and_owns_its_equivalent_codes(self):
        payload = {'sku': '58411-1r000', 'codes': [{'code': '58411-1r000-g'}, {'code': '58-1r0'}, {'code': 'd-hyu-1r'}]}
        created = self.client.post('/api/v1/management/catalog/', payload, format='json')
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data['sku'], '58411-1R000')
        self.assertEqual(created.data['name'], '')
        self.assertNotIn('brand', created.data)
        self.assertEqual([code['brand'] for code in created.data['codes']], ['', '', ''])
        self.assertEqual(self.client.post('/api/v1/management/catalog/', {'sku': '58411-1R000'}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/v1/management/catalog/', {'sku': '58-1R0'}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/v1/management/catalog/', {'sku': 'OTHER', 'codes': [{'code': '58411-1R000'}]}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/v1/management/catalog/', {'sku': 'BRANDED', 'brand': 'ACME'}, format='json').status_code, 400)
        url = f"/api/v1/management/catalog/{created.data['id']}/"
        updated = self.client.patch(url, {'codes': [{'code': '58-1R0'}]}, format='json')
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data['codes'], [{'code': '58-1R0', 'brand': '', 'kind': 'alias', 'ref_type': 'unknown', 'reference_source': ''}])
        self.assertEqual(Part.objects.count(), 1)

    def test_unknown_supplier_code_does_not_create_catalog_or_get_approved_automatically(self):
        item = ingest_inventory(supplier=self.account, actor=self.user, data={'update_id': 'pending', 'supplier_invent_id': 'stable', 'codigo': 'UNKNOWN', 'brand': 'ACME', 'source': 'upload', 'quantity': 4})
        self.assertEqual(Part.objects.count(), 0)
        response = self.client.post('/api/v1/management/catalog/', {'sku': 'NUEVO', 'name': 'Nuevo', 'codes': [{'brand': 'ACME', 'code': 'UNKNOWN'}]}, format='json')
        self.assertEqual(response.status_code, 201)
        item.refresh_from_db()
        self.assertIsNone(item.part)
        self.assertEqual(item.matching_status, 'pending')

    def test_stock_matching_approval_preserves_supplier_identity_balances_and_ledger(self):
        item = ingest_inventory(supplier=self.account, actor=self.user, data={'update_id': 'review', 'supplier_invent_id': 'stable', 'codigo': 'UNKNOWN', 'brand': 'ACME', 'source': 'upload', 'quantity': 9})
        item.reserved_quantity = 2
        item.save()
        ledger_before = list(StockEntry.objects.values())
        part = Part.objects.create(sku='IDENTIFICADO', name='Identificado')
        url = f'/api/v1/management/inventory/{item.pk}/'
        self.assertEqual(self.client.patch(url, {'matching_status': 'matched'}, format='json').status_code, 400)
        for field, value in [('reported_quantity', 100), ('reserved_quantity', 0), ('supplier_invent_id', 'changed'), ('supplier', str(self.account.pk)), ('codigo', 'CHANGED')]:
            self.assertEqual(self.client.patch(url, {field: value}, format='json').status_code, 400)
        response = self.client.patch(url, {'part': str(part.pk), 'matching_status': 'matched'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['part_name'], part.name)
        self.assertEqual(response.data['available_quantity'], 7)
        item.refresh_from_db()
        self.assertEqual(item.supplier_invent_id, 'stable')
        self.assertEqual(item.supplier, self.account)
        self.assertEqual(item.codigo, 'UNKNOWN')
        self.assertEqual(list(StockEntry.objects.values()), ledger_before)
        self.assertEqual(self.client.patch(url, {'part': None}, format='json').status_code, 400)
        self.assertEqual(self.client.patch(url, {'part': None, 'matching_status': 'pending'}, format='json').status_code, 200)

    def test_approved_legacy_codes_remain_in_the_same_sku_library(self):
        part = Part.objects.create(sku='58411-1R000')
        pending = ingest_inventory(supplier=self.account, actor=self.user, data={'update_id': 'pending-alias', 'supplier_invent_id': 'pending', 'codigo': '58411-1R000-G', 'brand': 'GENERIC', 'source': 'upload', 'quantity': 3})
        payload = {'part': str(part.pk), 'code': ' 58411-1r000-g '}
        created = self.client.post('/api/v1/management/alternates/', payload, format='json')
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.data['part_sku'], part.sku)
        self.assertEqual(created.data['code'], '58411-1R000-G')
        self.assertEqual(created.data['brand'], '')
        self.assertNotIn('replacement', created.data)
        pending.refresh_from_db()
        self.assertIsNone(pending.part)
        self.assertEqual(pending.matching_status, 'pending')
        catalog = self.client.get(f'/api/v1/management/catalog/{part.pk}/').data
        self.assertEqual(catalog['codes'], [{'brand': '', 'code': '58411-1R000-G', 'kind': 'alias', 'ref_type': 'unknown', 'reference_source': ''}])
        stock = ingest_inventory(supplier=self.account, actor=self.user, data={'update_id': 'known-alias', 'supplier_invent_id': 'known', 'codigo': '58411-1R000-G', 'brand': 'OTHER', 'source': 'upload', 'quantity': 8})
        self.assertEqual(stock.part, part)
        self.assertEqual(stock.matching_status, 'matched')
        ledger_before = list(StockEntry.objects.values())
        self.assertEqual(self.client.post('/api/v1/management/alternates/', payload, format='json').status_code, 400)
        url = f"/api/v1/management/alternates/{created.data['id']}/"
        updated = self.client.patch(url, {'code': '58-1r0', 'brand': ' oem '}, format='json')
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data['code'], '58-1R0')
        self.assertEqual(updated.data['brand'], 'OEM')
        self.assertEqual(updated.data['id'], created.data['id'])
        self.assertEqual(self.client.patch(url, {'code': 'd-hyu-1r'}, format='json').data['brand'], 'OEM')
        # Both editors operate on the same code row and keep its identity.
        self.assertEqual(self.client.patch(f'/api/v1/management/catalog/{part.pk}/', {'codes': [{'code': 'D-HYU-1R', 'brand': 'OEM'}]}, format='json').status_code, 200)
        self.assertEqual(self.client.get(url).data['code'], 'D-HYU-1R')
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(part.codes.exists())
        self.assertEqual(Part.objects.count(), 1)
        stock.refresh_from_db()
        self.assertEqual(stock.part, part)
        self.assertEqual(stock.supplier_invent_id, 'known')
        self.assertEqual(stock.reported_quantity, 8)
        self.assertEqual(list(StockEntry.objects.values()), ledger_before)

    def test_alterno_conflicts_cannot_assign_the_same_code_to_two_skus(self):
        first = Part.objects.create(sku='FIRST')
        second = Part.objects.create(sku='SECOND')
        PartCode.objects.create(part=first, brand='OEM', code='SHARED')
        url = '/api/v1/management/alternates/'
        for payload in [
            {'part': str(second.pk), 'code': 'shared'},
            {'part': str(second.pk), 'code': 'shared', 'brand': 'oem'},
            {'part': str(second.pk), 'code': first.sku},
            {'part': str(second.pk), 'code': 'IGNORED', 'replacement': str(first.pk)},
        ]:
            self.assertEqual(self.client.post(url, payload, format='json').status_code, 400)
        distinct = self.client.post(url, {'part': str(second.pk), 'code': 'SHARED', 'brand': 'OTHER'}, format='json')
        self.assertEqual(distinct.status_code, 201)
        self.assertEqual(self.client.patch(f"{url}{distinct.data['id']}/", {'brand': ''}, format='json').status_code, 400)
        self.assertEqual(PartCode.objects.count(), 2)

    def test_legacy_compatibility_relations_are_preserved_without_becoming_alternos(self):
        part = Part.objects.create(sku='FIRST')
        replacement = Part.objects.create(sku='SECOND')
        legacy = Alternate.objects.create(part=part, replacement=replacement)
        self.assertEqual(self.client.get('/api/v1/management/alternates/').data['count'], 0)
        self.client.post('/api/v1/management/alternates/', {'part': str(part.pk), 'code': 'SUPPLIER-CODE'}, format='json')
        self.assertTrue(Alternate.objects.filter(pk=legacy.pk).exists())
        self.client.force_authenticate(self.user)
        result = self.client.get('/api/v1/catalog/?search=SUPPLIER-CODE').data['results'][0]
        self.assertEqual(result['sku'], part.sku)
        self.assertEqual(result['codes'], [{'brand': '', 'code': 'SUPPLIER-CODE', 'kind': 'alias', 'ref_type': 'unknown'}])
        self.assertNotIn('alternates', result)

    def test_anonymous_access_is_denied_and_profile_is_authenticated(self):
        self.client.force_authenticate(None)
        for route in ['auth/me/', 'management/catalog/', 'management/inventory/', 'management/alternates/', 'management/accounts/', 'management/users/', 'management/roles/', 'management/invitations/']:
            self.assertEqual(self.client.get('/api/v1/' + route).status_code, 401)
        self.client.force_authenticate(self.user)
        profile = self.client.get('/api/v1/auth/me/').data
        self.assertFalse(profile['is_superuser'])
        self.assertNotIn('password', profile)
        self.assertNotIn('token', profile)

    def test_superuser_can_search_catalog_stock_and_alterno_codes(self):
        part = Part.objects.create(sku='FILTRO ORIGINAL', name='Filtro original', active=False)
        alternate = PartCode.objects.create(part=part, brand='ACME', code='ABC-123')
        SupplierItem.objects.create(supplier=self.account, supplier_invent_id='stable-123', part=part, codigo='ABC-123', brand='ACME', source='upload', reported_quantity=7, reserved_quantity=2, matching_status='matched')
        catalog = self.client.get('/api/v1/management/catalog/?search=ABC-123')
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.data['count'], 1)
        self.assertFalse(catalog.data['results'][0]['active'])
        self.assertEqual(catalog.data['results'][0]['stock_record_count'], 1)
        stock = self.client.get('/api/v1/management/inventory/?search=stable-123')
        self.assertEqual(stock.status_code, 200)
        self.assertEqual(stock.data['results'][0]['supplier_name'], self.account.name)
        self.assertEqual(stock.data['results'][0]['available_quantity'], 5)
        alternates = self.client.get(f'/api/v1/management/alternates/?search={part.sku}')
        self.assertEqual(alternates.status_code, 200)
        self.assertEqual(alternates.data['count'], 1)
        self.assertEqual(alternates.data['results'][0]['id'], alternate.pk)
        self.assertEqual(str(alternates.data['results'][0]['part']), str(part.pk))
        self.assertEqual(alternates.data['results'][0]['code'], 'ABC-123')
        self.assertEqual(self.client.get('/api/v1/management/alternates/?search=ACME').data['count'], 1)

    def test_grid_sorting_is_whitelisted_and_stable(self):
        parts = [Part.objects.create(sku=sku, name=sku) for sku in ['C-SKU', 'A-SKU', 'B-SKU']]
        for index, part in enumerate(parts):
            for n in range(index):
                SupplierItem.objects.create(supplier=self.account, supplier_invent_id=f'{part.sku}-{n}', part=part, codigo=part.sku, brand='X', source='upload',
                                            reported_quantity=n + 1, reserved_quantity=3)
        skus = lambda url: [row['sku'] for row in self.client.get(url).data['results']]
        self.assertEqual(skus('/api/v1/management/catalog/?ordering=sku'), ['A-SKU', 'B-SKU', 'C-SKU'])
        self.assertEqual(skus('/api/v1/management/catalog/?ordering=-stock_record_count'), ['B-SKU', 'A-SKU', 'C-SKU'])
        self.assertEqual(skus('/api/v1/management/catalog/?ordering=-sku'), ['C-SKU', 'B-SKU', 'A-SKU'])
        self.assertEqual(skus('/api/v1/management/catalog/?ordering=password'), ['A-SKU', 'B-SKU', 'C-SKU'])  # not listed: default order
        stock = self.client.get('/api/v1/management/inventory/?ordering=-available').data['results']
        self.assertEqual([row['available_quantity'] for row in stock], [0, 0, 0])  # 1-3, 1-3, 2-3: never below zero
        self.assertEqual(self.client.get('/api/v1/management/alternates/?ordering=-code').status_code, 200)

    def test_account_roles_and_employee_access_can_be_managed(self):
        created = self.client.post('/api/v1/management/accounts/', {'name': 'Nueva empresa', 'roles': ['supplier_wholesale', 'client_business']}, format='json')
        self.assertEqual(created.status_code, 201)
        account_id = created.data['id']
        membership = self.client.post('/api/v1/management/memberships/', {'user': self.user.pk, 'account': account_id, 'permission': 'staff'}, format='json')
        self.assertEqual(membership.status_code, 201)
        member_url = f"/api/v1/management/memberships/{membership.data['id']}/"
        self.assertEqual(self.client.patch(member_url, {'permission': 'manager'}, format='json').status_code, 200)
        detail = self.client.get(f'/api/v1/management/accounts/{account_id}/').data
        self.assertEqual(detail['member_count'], 1)
        self.assertEqual(detail['members'][0]['permission_label'], 'Administrador')
        user = self.client.get(f'/api/v1/management/users/{self.user.pk}/').data
        self.assertEqual(len(user['memberships']), 2)
        self.assertEqual(self.client.delete(member_url).status_code, 204)
        self.assertEqual(self.client.get(f'/api/v1/management/accounts/{account_id}/').data['member_count'], 0)
        self.assertEqual(self.client.patch(f'/api/v1/management/accounts/{account_id}/', {'active': False}, format='json').status_code, 200)
        self.assertFalse(Account.objects.get(pk=account_id).active)

    def test_duplicate_membership_and_invalid_role_are_rejected(self):
        duplicate = self.client.post('/api/v1/management/memberships/', {'user': self.user.pk, 'account': str(self.account.pk)}, format='json')
        self.assertEqual(duplicate.status_code, 400)
        invalid = self.client.patch(f'/api/v1/management/accounts/{self.account.pk}/', {'roles': ['unknown_role']}, format='json')
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(list(self.account.roles.values_list('code', flat=True)), ['supplier_retail'])

    def test_users_can_be_edited_and_deactivation_revokes_existing_access(self):
        token = Token.objects.create(user=self.user)
        updated = self.client.patch(f'/api/v1/management/users/{self.user.pk}/', {'first_name': 'Ana', 'last_name': 'Pérez', 'is_active': False}, format='json')
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data['first_name'], 'Ana')
        self.assertFalse(Token.objects.filter(pk=token.key).exists())
        other_client = APIClient()
        other_client.credentials(HTTP_AUTHORIZATION='Token ' + token.key)
        self.assertEqual(other_client.get('/api/v1/accounts/').status_code, 401)

    def test_privileges_and_superuser_deactivation_cannot_be_changed_in_panel(self):
        for payload in [{'is_superuser': True}, {'is_staff': True}, {'password': 'SomethingElse123!'}]:
            self.assertEqual(self.client.patch(f'/api/v1/management/users/{self.user.pk}/', payload, format='json').status_code, 400)
        self.assertEqual(self.client.patch(f'/api/v1/management/users/{self.root.pk}/', {'is_active': False}, format='json').status_code, 400)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)
        self.assertFalse(self.user.is_staff)
        self.assertTrue(self.user.check_password('SecureEmployee938!'))

    def test_email_uniqueness_is_case_insensitive(self):
        response = self.client.patch(f'/api/v1/management/users/{self.user.pk}/', {'email': 'ROOT@EXAMPLE.COM'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'employee@example.com')

    def test_invitation_can_be_created_and_revoked(self):
        created = self.client.post('/api/v1/management/invitations/', {'email': 'INVITED@example.com', 'days': 3}, format='json')
        self.assertEqual(created.status_code, 201)
        invite = Invitation.objects.get(pk=created.data['id'])
        self.assertEqual(invite.email, 'invited@example.com')
        self.assertEqual(invite.created_by, self.root)
        self.assertEqual(created.data['status'], 'pending')
        revoked = self.client.patch(f'/api/v1/management/invitations/{invite.pk}/revoke/', {}, format='json')
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.data['status'], 'expired')
        invite.refresh_from_db()
        self.assertLessEqual(invite.expires_at, timezone.now())
        self.invitation.accepted_at = timezone.now()
        self.invitation.save()
        self.assertEqual(self.client.patch(f'/api/v1/management/invitations/{self.invitation.pk}/revoke/', {}, format='json').status_code, 400)

    def test_django_admin_also_requires_superuser(self):
        self.user.is_staff = True
        self.user.save()
        self.user.user_permissions.set(Permission.objects.all())
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/admin/').status_code, 302)
        self.assertEqual(self.client.get('/admin/mall/user/').status_code, 302)
        self.client.force_login(self.root)
        self.assertEqual(self.client.get('/admin/').status_code, 200)
