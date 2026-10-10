import io
from unittest.mock import ANY

from cryptography.fernet import Fernet
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from .apiag_models import ApiagConnection
from .apiag_keys import main as apiag_keys
from .apiag_tokens import token
from .models import Account, Role, User

KEY, NEW_KEY = Fernet.generate_key().decode(), Fernet.generate_key().decode()
TOKEN, OTHER_TOKEN = 'apiagTok3n-For-MotionPartes-0001', 'apiagTok3n-For-MotionPartes-0002'


@override_settings(APIAG_TOKEN_KEYS=[KEY])
class ApiagConnectionTests(APITestCase):
    def setUp(self):
        self.root = User.objects.create_superuser('root', 'root@example.com', 'SecureRoot938!')
        self.supplier = Account.objects.create(name='Repuestos con apiag')
        self.supplier.roles.add(Role.objects.get(code='supplier_wholesale'))
        self.client_account = Account.objects.create(name='Taller cliente')
        self.client_account.roles.add(Role.objects.get(code='client_business'))
        self.client.force_authenticate(self.root)
        self.path = f'/api/v1/management/accounts/{self.supplier.pk}/apiag/'

    def save(self, server_url='https://empresa.apiag.cloud', secret=TOKEN, account=None):
        path = f'/api/v1/management/accounts/{account.pk}/apiag/' if account else self.path
        return self.client.post(path, {'server_url': server_url, 'access_token': secret}, format='json')

    def test_the_token_is_stored_encrypted_and_no_response_carries_it(self):
        response = self.save('  https://Empresa.APIAG.cloud/api/  ')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data, {'server_url': 'https://empresa.apiag.cloud/api', 'token_hint': '0001', 'token_readable': True,
                                         'updated_at': ANY, 'updated_by': 'root'})
        connection = ApiagConnection.objects.get(account=self.supplier)
        self.assertNotIn(TOKEN, connection.token_ciphertext)
        self.assertEqual(token(connection), TOKEN)
        listing = self.client.get('/api/v1/management/accounts/')
        detail = self.client.get(f'/api/v1/management/accounts/{self.supplier.pk}/')
        for response in (listing, detail):
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(TOKEN, response.content.decode())
            self.assertNotIn(connection.token_ciphertext, response.content.decode())
        self.assertEqual(detail.data['apiag']['token_hint'], '0001')
        rows = {row['name']: row['apiag'] for row in listing.data['results']}
        self.assertEqual(rows['Taller cliente'], None)
        self.assertEqual(rows['Repuestos con apiag']['server_url'], 'https://empresa.apiag.cloud/api')

    def test_a_blank_token_keeps_the_stored_one_and_a_new_one_replaces_it(self):
        self.save()
        kept = self.save('https://empresa.apiag.cloud/api', '')
        self.assertEqual(kept.status_code, 200, kept.data)
        connection = ApiagConnection.objects.get(account=self.supplier)
        self.assertEqual((connection.server_url, token(connection), connection.token_hint), ('https://empresa.apiag.cloud/api', TOKEN, '0001'))
        replaced = self.save('https://otra.apiag.cloud', OTHER_TOKEN)
        self.assertEqual(replaced.data['token_hint'], '0002')
        self.assertEqual(token(ApiagConnection.objects.get(account=self.supplier)), OTHER_TOKEN)
        self.assertEqual(ApiagConnection.objects.count(), 1)

    def test_the_first_save_needs_a_token_and_tokens_are_pasted_bare(self):
        missing = self.save(secret='')
        self.assertEqual(missing.status_code, 400)
        self.assertIn('access_token', missing.data)
        for secret in (f'Token {TOKEN}', 'short-token', f'{TOKEN}\n{TOKEN}', 'x' * 513):
            response = self.save(secret=secret)
            self.assertEqual(response.status_code, 400, secret)
            self.assertIn('access_token', response.data)
        self.assertFalse(ApiagConnection.objects.exists())

    def test_server_url_rules(self):
        refused = ['empresa.apiag.cloud', 'ftp://empresa.apiag.cloud', 'https://', 'https://admin:clave@empresa.apiag.cloud',
                   'https://empresa.apiag.cloud/?token=1', 'https://empresa.apiag.cloud/#api', 'https://empresa.apiag.cloud:99999',
                   'http://empresa.apiag.cloud', 'https://[::1', 'https://empresa.apiag.cloud]', 'https://empresa.apiag.cloud:0',
                   'https://@empresa.apiag.cloud', 'https://empresa apiag.cloud', 'https://empresa.apiag.cloud\\@otro.example',
                   'https://empresa_apiag.cloud', 'https://-empresa.apiag.cloud', 'https://empresa..apiag.cloud', 'HTTPS://[zz::1]']
        for url in refused:
            response = self.save(url)
            self.assertEqual(response.status_code, 400, url)
            self.assertIn('server_url', response.data, url)
        self.assertFalse(ApiagConnection.objects.exists())
        self.assertEqual(self.save('https://empresa.apiag.cloud:8443/').data['server_url'], 'https://empresa.apiag.cloud:8443')
        self.assertEqual(self.save('HTTPS://[0:0::1]:8443/api').data['server_url'], 'https://[::1]:8443/api')
        self.assertEqual(self.save('https://Ñandú.example').data['server_url'], 'https://xn--and-6ma2c.example')

    @override_settings(DEBUG=True)
    def test_local_runs_accept_plain_http(self):
        response = self.save('http://host.docker.internal:8000')
        self.assertEqual(response.status_code, 201, response.data)

    def test_only_supplier_accounts_connect(self):
        response = self.save(account=self.client_account)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data['detail']), 'Solo las cuentas de proveedor pueden conectarse con apiag-cloud.')
        self.assertFalse(ApiagConnection.objects.exists())

    def test_delete_removes_the_connection(self):
        self.save()
        self.assertEqual(self.client.delete(self.path).status_code, 204)
        self.assertFalse(ApiagConnection.objects.exists())
        self.assertEqual(self.client.delete(self.path).status_code, 204)
        self.assertEqual(self.client.delete('/api/v1/management/accounts/00000000-0000-0000-0000-000000000000/apiag/').status_code, 404)
        self.assertEqual(self.client.get(f'/api/v1/management/accounts/{self.supplier.pk}/').data['apiag'], None)

    def test_only_superusers_reach_the_connection(self):
        self.save()
        staff = User.objects.create_user('staff', 'staff@example.com', 'SecureStaff938!', is_staff=True)
        member = User.objects.create_user('member', 'member@example.com', 'SecureMember938!')
        self.supplier.memberships.create(user=member, permission='owner')
        for user in (staff, member, None):
            client = APIClient()
            if user:
                client.force_authenticate(user)
            for response in (client.post(self.path, {'server_url': 'https://robo.example', 'access_token': OTHER_TOKEN}, format='json'), client.delete(self.path)):
                self.assertIn(response.status_code, (401, 403))
        connection = ApiagConnection.objects.get(account=self.supplier)
        self.assertEqual((connection.server_url, token(connection)), ('https://empresa.apiag.cloud', TOKEN))

    def test_rotated_keys_still_read_old_tokens_and_a_lost_key_asks_for_the_token_again(self):
        self.save()
        with self.settings(APIAG_TOKEN_KEYS=[NEW_KEY, KEY]):
            self.assertTrue(self.client.get(f'/api/v1/management/accounts/{self.supplier.pk}/').data['apiag']['token_readable'])
        with self.settings(APIAG_TOKEN_KEYS=[NEW_KEY]):
            self.assertFalse(self.client.get(f'/api/v1/management/accounts/{self.supplier.pk}/').data['apiag']['token_readable'])
            blank = self.save(secret='')
            self.assertEqual(blank.status_code, 400)
            self.assertIn('access_token', blank.data)
            self.assertTrue(self.save(secret=OTHER_TOKEN).data['token_readable'])
            self.assertEqual(token(ApiagConnection.objects.get(account=self.supplier)), OTHER_TOKEN)

    def test_a_save_moves_the_kept_token_to_the_first_key_and_rotate_moves_them_all(self):
        self.save()
        other = Account.objects.create(name='Otro proveedor')
        other.roles.add(Role.objects.get(code='supplier_retail'))
        self.save(account=other, secret=OTHER_TOKEN)
        with self.settings(APIAG_TOKEN_KEYS=[NEW_KEY, KEY]):
            self.assertEqual(self.save(secret='').status_code, 200)
        with self.settings(APIAG_TOKEN_KEYS=[NEW_KEY]):
            self.assertEqual(token(ApiagConnection.objects.get(account=self.supplier)), TOKEN)
            output = io.StringIO()
            self.assertEqual(apiag_keys(['rotate'], output), 1)  # the other account's token is still under the old key only
            self.assertIn('Ninguna clave lee el token de: Otro proveedor.', output.getvalue())
        with self.settings(APIAG_TOKEN_KEYS=[NEW_KEY, KEY]):
            output = io.StringIO()
            self.assertEqual(apiag_keys(['rotate'], output), 0)
            self.assertEqual(output.getvalue(), 'Tokens cifrados de nuevo con la primera clave: 2.\n')
        output = io.StringIO()
        apiag_keys([], output)
        self.assertRegex(output.getvalue(), r'^APIAG_TOKEN_KEYS=[A-Za-z0-9_-]{43}=\n$')
        with self.settings(APIAG_TOKEN_KEYS=[NEW_KEY]):
            self.assertEqual([token(connection) for connection in ApiagConnection.objects.order_by('account__name')], [OTHER_TOKEN, TOKEN])

    def test_a_new_server_needs_its_own_token(self):
        self.save()
        for url in ('https://otra.apiag.cloud', 'https://empresa.apiag.cloud:8443', 'https://empresa.apiag.cloud.evil.example'):
            response = self.save(url, '')
            self.assertEqual(response.status_code, 400, url)
            self.assertIn('access_token', response.data)
        self.assertEqual(self.save('https://empresa.apiag.cloud/api/v2', '').status_code, 200)
        self.assertEqual(self.save('https://otra.apiag.cloud', OTHER_TOKEN).data['token_hint'], '0002')

    def test_without_a_key_production_refuses_to_save_and_local_runs_use_the_secret_key(self):
        for keys in ([], ['not-a-fernet-key']):
            with self.settings(APIAG_TOKEN_KEYS=keys):
                response = self.save()
                self.assertEqual(response.status_code, 503)
                self.assertIn('APIAG_TOKEN_KEYS', str(response.data['detail']))
        self.assertFalse(ApiagConnection.objects.exists())
        with self.settings(APIAG_TOKEN_KEYS=[], DEBUG=True):
            self.assertEqual(self.save('http://localhost:8000').status_code, 201)
            self.assertEqual(token(ApiagConnection.objects.get(account=self.supplier)), TOKEN)
