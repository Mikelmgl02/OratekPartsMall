import io
import json
import socket
import ssl
import threading
import time
import http.client
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import ANY, patch

from cryptography.fernet import Fernet
from django.test import override_settings
from rest_framework.test import APIClient, APITestCase

from .apiag_models import ApiagConnection
from . import apiag_client
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
        refused = ['ftp://empresa.apiag.cloud', 'https://', 'https://admin:clave@empresa.apiag.cloud',
                   'https://empresa.apiag.cloud/?token=1', 'https://empresa.apiag.cloud/#api', 'https://empresa.apiag.cloud:99999',
                   'http://empresa.apiag.cloud', 'https://[::1', 'https://empresa.apiag.cloud]', 'https://empresa.apiag.cloud:0',
                   'https://@empresa.apiag.cloud', 'https://empresa apiag.cloud', 'https://empresa.apiag.cloud\\@otro.example',
                   'https://empresa_apiag.cloud', 'https://-empresa.apiag.cloud', 'https://empresa..apiag.cloud', 'HTTPS://[zz::1]',
                   'https//empresa.apiag.cloud', 'https:/empresa.apiag.cloud', 'http:/empresa.apiag.cloud', 'https:', 'empresa.apiag.cloud/año',
                   'empresa.apiag.cloud/' + 'a' * 290]
        for url in refused:
            response = self.save(url)
            self.assertEqual(response.status_code, 400, url)
            self.assertIn('server_url', response.data, url)
        self.assertFalse(ApiagConnection.objects.exists())
        self.assertEqual(self.save('https://empresa.apiag.cloud:8443/').data['server_url'], 'https://empresa.apiag.cloud:8443')
        self.assertEqual(self.save('HTTPS://[0:0::1]:8443/api').data['server_url'], 'https://[::1]:8443/api')
        self.assertEqual(self.save('https://Ñandú.example').data['server_url'], 'https://xn--and-6ma2c.example')
        # Without a scheme, https:// is filled in (production refuses http:// anyway, so localhost gets https:// too).
        for typed, saved in (('empresa.apiag.cloud', 'https://empresa.apiag.cloud'), ('Empresa.apiag.cloud:8443/api/', 'https://empresa.apiag.cloud:8443/api'),
                             ('localhost:8000', 'https://localhost:8000')):
            self.assertEqual(self.save(typed).data['server_url'], saved, typed)

    @override_settings(DEBUG=True)
    def test_local_runs_accept_plain_http_and_fill_it_in_for_local_addresses(self):
        response = self.save('http://host.docker.internal:8000')
        self.assertEqual(response.status_code, 201, response.data)
        for typed, saved in (('localhost:8000', 'http://localhost:8000'), ('127.0.0.1:8000', 'http://127.0.0.1:8000'),
                             ('host.docker.internal:8000', 'http://host.docker.internal:8000'), ('192.168.1.20', 'http://192.168.1.20'),
                             ('empresa.apiag.cloud', 'https://empresa.apiag.cloud')):
            self.assertEqual(self.save(typed).data['server_url'], saved, typed)

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


class PingServer(BaseHTTPRequestHandler):
    """A stand-in apiag-cloud: the first path segment picks the answer, and every request is recorded."""
    seen, hosts = [], []

    def do_GET(self):
        PingServer.seen.append((self.path, self.headers.get('Authorization')))
        PingServer.hosts.append(self.headers.get('Host'))
        case = self.path.split('/')[1]
        answers = {'ok': (200, {'ok': True}), 'off': (401, {'detail': 'El acceso de MotionPartes está desactivado en este servidor.'}),
                   'bad': (401, {'detail': 'Token de MotionPartes inválido.'}), 'otherauth': (401, {'detail': 'Credenciales requeridas.'}),
                   'expired': (403, {'error': 'License expired'}), 'nolicence': (403, {'error': 'License not found'}),
                   'boom': (500, {'detail': 'error'}), 'html': (200, None)}
        if case == 'slow':  # a status line, then a byte now and then: no single read ever times out
            self.wfile.write(b'HTTP/1.1 200 OK\r\n')
            try:
                for _ in range(50):
                    self.wfile.write(b'X')
                    time.sleep(0.1)
            except OSError:
                pass
            return
        if case == 'move':
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/ok{apiag_client.PING_PATH}')
            self.end_headers()
            return
        code, body = answers.get(case, (404, {'detail': 'No encontrado.'}))
        payload = b'<html>Hola</html>' if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/html' if body is None else 'application/json')
        if case in ('off', 'bad'):
            self.send_header('WWW-Authenticate', 'Bearer realm="motionpartes"')
        elif case == 'otherauth':
            self.send_header('WWW-Authenticate', 'Basic realm="intranet"')
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@override_settings(APIAG_TOKEN_KEYS=[KEY], DEBUG=True, APIAG_LOCALHOST='')
class ApiagConnectionCheckTests(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), PingServer)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        super().tearDownClass()

    def setUp(self):
        PingServer.seen, PingServer.hosts = [], []
        self.root = User.objects.create_superuser('root', 'root@example.com', 'SecureRoot938!')
        self.supplier = Account.objects.create(name='Repuestos con apiag')
        self.supplier.roles.add(Role.objects.get(code='supplier_wholesale'))
        self.client.force_authenticate(self.root)
        self.path = f'/api/v1/management/accounts/{self.supplier.pk}/apiag/test/'

    def check(self, case, secret=TOKEN):
        return self.client.post(self.path, {'server_url': f'{self.base}/{case}', 'access_token': secret}, format='json')

    def test_a_server_that_accepts_the_token(self):
        response = self.check('ok')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual({key: response.data[key] for key in ('ok', 'status', 'http_status')}, {'ok': True, 'status': 'ok', 'http_status': 200})
        self.assertRegex(response.data['message'], r'^Conexión correcta: ese apiag-cloud aceptó el token \(\d+ ms\)\.$')
        self.assertEqual(PingServer.seen, [(f'/ok{apiag_client.PING_PATH}', f'Bearer {TOKEN}')])
        self.assertFalse(ApiagConnection.objects.exists())

    def test_each_answer_has_its_own_message(self):
        expected = {'off': ('disabled', 401, 'falta motion_partes_access_token'), 'bad': ('token_rejected', 401, 'rechazó el token'),
                    'otherauth': ('unexpected', 401, 'no parece un apiag-cloud'), 'nada': ('not_found', 404, 'no tiene la ruta de MotionPartes'),
                    'expired': ('license', 403, 'está vencida'), 'nolicence': ('license', 403, 'todavía no tiene una licencia'),
                    'boom': ('unexpected', 500, 'HTTP 500'), 'html': ('unexpected', 200, 'no como un apiag-cloud')}
        for case, (status, code, text) in expected.items():
            data = self.check(case).data
            self.assertEqual((data['ok'], data['status'], data['http_status']), (False, status, code), case)
            self.assertIn(text, data['message'], case)

    def test_the_token_never_follows_a_redirect(self):
        data = self.check('move').data
        self.assertEqual((data['status'], data['http_status']), ('redirect', 302))
        self.assertIn(f'{self.base}/ok{apiag_client.PING_PATH}', data['message'])
        self.assertEqual([path for path, _ in PingServer.seen], [f'/move{apiag_client.PING_PATH}'])

    def test_a_blank_token_tests_the_saved_one_on_the_same_server_only(self):
        self.assertEqual(self.client.post(self.path, {'server_url': f'{self.base}/ok', 'access_token': ''}, format='json').status_code, 400)
        self.client.post(f'/api/v1/management/accounts/{self.supplier.pk}/apiag/', {'server_url': f'{self.base}/ok', 'access_token': TOKEN}, format='json')
        self.assertTrue(self.check('ok', '').data['ok'])
        self.assertEqual(PingServer.seen[-1][1], f'Bearer {TOKEN}')
        other = self.client.post(self.path, {'server_url': 'http://localhost:9/ok', 'access_token': ''}, format='json')
        self.assertEqual(other.status_code, 400)
        self.assertIn('access_token', other.data)
        self.assertEqual(len(PingServer.seen), 1)

    def test_unreachable_servers(self):
        cases = [(socket.gaierror(-2, 'Name or service not known'), 'unreachable', 'No se encontró el servidor'),
                 (ConnectionRefusedError(111, 'refused'), 'unreachable', 'rechazó la conexión'),
                 (ssl.SSLCertVerificationError(1, 'certificate verify failed'), 'tls', 'certificado HTTPS'),
                 (ssl.SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number'), 'tls', 'no respondió con HTTPS'),
                 (TimeoutError('timed out'), 'unreachable', 'no respondió en'), (ConnectionResetError(104, 'reset'), 'unreachable', 'cortó la conexión'),
                 (http.client.RemoteDisconnected('closed'), 'unreachable', 'cortó la conexión'), (ValueError('bad'), 'unreachable', 'no es válida')]
        for error, status, text in cases:
            with patch.object(apiag_client.GuardedHTTP, 'request', side_effect=error):
                data = self.check('ok').data
            self.assertEqual((data['ok'], data['status']), (False, status), error)
            self.assertIn(text, data['message'], error)

    def test_one_deadline_for_the_whole_call(self):
        started = time.monotonic()
        with patch.object(apiag_client, 'TIMEOUT', 1):
            data = self.check('slow').data
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((data['status'], data['message']), ('unreachable', f'127.0.0.1 no respondió en 1 segundos.'))

    def test_outside_debug_internal_addresses_are_refused(self):
        post = lambda url: self.client.post(self.path, {'server_url': url, 'access_token': TOKEN}, format='json').data
        with self.settings(DEBUG=False), patch.object(apiag_client.GuardedHTTPS, 'request') as sent:
            for address in ('127.0.0.1', '10.0.0.5', '169.254.169.254', '::1', 'fd00::1', '::ffff:10.0.0.5', '64:ff9b::a00:5'):
                with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]):
                    self.assertEqual(post('https://empresa.apiag.cloud')['status'], 'internal', address)
            with patch('socket.getaddrinfo', side_effect=socket.gaierror(-2, 'unknown')):
                self.assertEqual(post('https://nadie.example')['status'], 'unreachable')
            sent.assert_not_called()

    def test_outside_debug_the_address_actually_reached_is_checked_too(self):
        # A name that resolves to a public address for the check and to an internal one for the connection (DNS rebinding).
        peer = type('Socket', (), {'getpeername': lambda self: ('10.0.0.5', 443), 'close': lambda self: None})()
        with self.settings(DEBUG=False), patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]), \
                patch('socket.create_connection', return_value=peer) as connected:
            data = self.client.post(self.path, {'server_url': 'https://empresa.apiag.cloud', 'access_token': TOKEN}, format='json').data
        self.assertEqual(data['status'], 'internal')
        connected.assert_called_once()
        self.assertTrue(apiag_client.public(ipaddress.ip_address('2001:4860:4860::8888')))
        self.assertFalse(apiag_client.public(ipaddress.ip_address('::7f00:1')))

    def test_locally_localhost_is_the_computer_running_docker(self):
        # The stand-in server, on 127.0.0.1 only, plays that computer: [::1] reaches it only through the mapping, and the server
        # still sees the address as saved.
        with self.settings(APIAG_LOCALHOST='127.0.0.1'):
            data = self.client.post(self.path, {'server_url': f'[::1]:{self.server.server_port}/ok', 'access_token': TOKEN}, format='json').data
        self.assertTrue(data['ok'], data)
        self.assertEqual(PingServer.hosts, [f'[::1]:{self.server.server_port}'])
        with self.settings(APIAG_LOCALHOST='host.docker.internal'):
            self.assertEqual(apiag_client.target('http://localhost:8000/api'), ('host.docker.internal', 'localhost:8000'))
            self.assertEqual(apiag_client.target('https://empresa.apiag.cloud'), ('empresa.apiag.cloud', 'empresa.apiag.cloud'))
            with self.settings(DEBUG=False):
                self.assertEqual(apiag_client.target('https://localhost:8000'), ('localhost', 'localhost:8000'))

    def test_validation_and_access(self):
        self.assertEqual(self.client.post(self.path, {'server_url': 'ftp://empresa', 'access_token': TOKEN}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/v1/management/accounts/00000000-0000-0000-0000-000000000000/apiag/test/',
                                          {'server_url': f'{self.base}/ok', 'access_token': TOKEN}, format='json').status_code, 404)
        member = User.objects.create_user('member', 'member@example.com', 'SecureMember938!')
        client = APIClient()
        client.force_authenticate(member)
        self.assertEqual(client.post(self.path, {'server_url': f'{self.base}/ok', 'access_token': TOKEN}, format='json').status_code, 403)
        self.assertEqual(PingServer.seen, [])
