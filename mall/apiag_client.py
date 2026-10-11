"""Calls from MotionPartes to a supplier's apiag-cloud API, with the server URL and token saved in Cuentas (mall.apiag_connection).

apiag-cloud accepts `Authorization: Bearer <token>` on its /api/motionpartes/ routes when the token equals its
motion_partes_access_token. Calls go straight to the saved server through http.client: no redirects and no proxies, so the token
only ever reaches that server. Outside DJANGO_DEBUG the address actually connected to must be public (checked after the TCP
connect, before TLS or any byte of the request), and every call has one deadline, so a slow server cannot hold a worker. In local
Docker runs, localhost means the computer running Docker (APIAG_LOCALHOST), where the admin's browser and a local apiag-cloud are,
not MotionPartes' own container."""
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
import zlib
from urllib.parse import urlsplit

from django.conf import settings

PING_PATH = '/api/motionpartes/ping/'
TIMEOUT = 8
EMBEDDED_IPV4 = (ipaddress.ip_network('64:ff9b::/96'), ipaddress.ip_network('::/96'))


class Unreachable(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def public(ip):
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif any(ip in network for network in EMBEDDED_IPV4):  # NAT64 and IPv4-compatible forms carry an IPv4 address
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global


def refuse_internal(host):
    """Early, friendlier refusal from a lookup; the connection itself checks the address it reaches."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        raise Unreachable('unreachable', f'No se encontró el servidor {host}.')
    for address in addresses:
        if not public(ipaddress.ip_address(address.split('%')[0])):
            raise Unreachable('internal', f'{host} apunta a una red interna; MotionPartes solo se conecta a servidores públicos.')


class Guarded:
    expired = False
    watched = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = self.open_socket

    def open_socket(self, address, timeout, source_address=None):
        sock = socket.create_connection(address, timeout, source_address)
        self.watched = sock
        if not settings.DEBUG and not public(ipaddress.ip_address(sock.getpeername()[0].split('%')[0])):
            sock.close()
            raise Unreachable('internal', f'{address[0]} apunta a una red interna; MotionPartes solo se conecta a servidores públicos.')
        return sock

    def expire(self):
        self.expired = True
        if self.watched is not None:
            try:
                self.watched.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class GuardedHTTP(Guarded, http.client.HTTPConnection):
    pass


class GuardedHTTPS(Guarded, http.client.HTTPSConnection):
    pass


def target(server_url):
    """(host to connect to, Host header) for the saved server URL."""
    parts = urlsplit(server_url)
    if settings.DEBUG and settings.APIAG_LOCALHOST and parts.hostname in ('localhost', '127.0.0.1', '::1'):
        return settings.APIAG_LOCALHOST, parts.netloc
    return parts.hostname, parts.netloc


def failure(error, host, expired):
    if expired or isinstance(error, TimeoutError):
        return 'unreachable', f'{host} no respondió en {TIMEOUT} segundos.'
    if isinstance(error, ssl.SSLCertVerificationError):
        return 'tls', f'El certificado HTTPS de {host} no es válido.'
    if isinstance(error, ssl.SSLError):
        return 'tls', f'{host} no respondió con HTTPS en esa dirección y puerto.'
    if isinstance(error, socket.gaierror):
        return 'unreachable', f'No se encontró el servidor {host}.'
    if isinstance(error, ConnectionRefusedError):
        return 'unreachable', f'{host} rechazó la conexión.'
    if isinstance(error, ValueError):
        return 'unreachable', 'La dirección del servidor no es válida.'
    return 'unreachable', f'{host} cortó la conexión sin responder.'


def get(server_url, path, token, max_bytes=65536):
    """(HTTP status, body, headers) of a GET to the supplier's apiag-cloud (path may carry a query string). Raises Unreachable, also for
    a body over max_bytes once unpacked: the stock feed's pages are larger than the ping's, so its caller raises the limit."""
    parts = urlsplit(server_url)
    host = parts.hostname
    if not settings.DEBUG:
        refuse_internal(host)
    connect_host, host_header = target(server_url)
    connection = (GuardedHTTPS if parts.scheme == 'https' else GuardedHTTP)(connect_host, parts.port, timeout=TIMEOUT)
    deadline = threading.Timer(TIMEOUT, connection.expire)
    deadline.daemon = True
    deadline.start()
    try:
        try:
            connection.request('GET', parts.path + path, headers={'Host': host_header, 'Authorization': f'Bearer {token}',
                                                                 'Accept': 'application/json', 'Accept-Encoding': 'gzip',
                                                                 'User-Agent': 'MotionPartes'})
            response = connection.getresponse()
        except Unreachable:
            raise
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise Unreachable(*failure(error, host, connection.expired))
        try:
            body = response.read(max_bytes + 1)
        except (OSError, http.client.HTTPException):
            body = b''  # the status still says what happened
        if connection.expired:  # cut off by the deadline: whatever was parsed is incomplete
            raise Unreachable(*failure(None, host, True))
        if 'gzip' in (response.headers.get('Content-Encoding') or '').lower():
            body = unpack(body, max_bytes, host)
        if len(body) > max_bytes:
            raise Unreachable('unexpected', f'La respuesta de {host} es más grande de lo esperado.')
        return response.status, body, response.headers
    finally:
        deadline.cancel()
        connection.close()


def unpack(body, max_bytes, host):
    """A gzip body, unpacked to at most max_bytes + 1 bytes (so a compressed bomb stops there)."""
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        return inflater.decompress(body, max_bytes + 1)
    except zlib.error:
        raise Unreachable('unexpected', f'{host} envió una respuesta comprimida dañada.')


def detail(body):
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def verdict(code, body, headers):
    data = detail(body) or {}
    text = str(data.get('detail') or data.get('error') or '').lower()
    if code == 200 and data.get('ok') is True:
        return 'ok', ''
    if code == 200:
        return 'unexpected', 'El servidor respondió, pero no como un apiag-cloud. Revisa la dirección.'
    if code == 401:
        # apiag-cloud's own refusals carry its realm; any other 401 comes from something else at that address
        if 'realm="motionpartes"' not in (headers.get('WWW-Authenticate', '') if headers else '').lower() and 'motionpartes' not in text:
            return 'unexpected', 'El servidor pidió otra autenticación (HTTP 401): no parece un apiag-cloud. Revisa la dirección.'
        if 'desactivado' in text:
            return 'disabled', 'El acceso de MotionPartes está desactivado en ese apiag-cloud: falta motion_partes_access_token en su .env.'
        return 'token_rejected', 'Ese apiag-cloud rechazó el token: no coincide con motion_partes_access_token de su .env.'
    if code == 404:
        return 'not_found', 'El servidor respondió, pero no tiene la ruta de MotionPartes. Revisa la dirección o actualiza ese apiag-cloud.'
    if code == 403 and ('license' in text or 'licencia' in text):
        if 'not found' in text or 'no se encontr' in text:
            return 'license', 'Ese apiag-cloud todavía no tiene una licencia registrada.'
        return 'license', 'La licencia de ese apiag-cloud está vencida.'
    if 300 <= code < 400:
        location = headers.get('Location', '') if headers else ''
        return 'redirect', f'La dirección redirige a otra{f" ({location})" if location else ""}. Usa la dirección final: el token no sigue redirecciones.'
    return 'unexpected', f'Respuesta inesperada del servidor (HTTP {code}).'


def ping(server_url, token):
    """Whether the supplier's apiag-cloud accepts the token, as {ok, status, message, http_status, elapsed_ms}."""
    started, code = time.monotonic(), None
    try:
        code, body, headers = get(server_url, PING_PATH, token)
    except Unreachable as error:
        status, message = error.status, error.message
    else:
        status, message = verdict(code, body, headers)
    elapsed = round((time.monotonic() - started) * 1000)
    if status == 'ok':
        message = f'Conexión correcta: ese apiag-cloud aceptó el token ({elapsed} ms).'
    return {'ok': status == 'ok', 'status': status, 'message': message, 'http_status': code, 'elapsed_ms': elapsed}
