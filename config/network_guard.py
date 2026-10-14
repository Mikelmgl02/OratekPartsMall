"""Outbound network guard for the test suite (config.settings_test installs it).

Any socket connection to a public address raises ConnectionRefusedError, so a test that forgets to mock Resend, Gemini, WhatsApp, web
push or Spaces fails or degrades instead of reaching the live service; private and loopback addresses (PostgreSQL, Redis) stay open.
Each blocked destination is reported once on stderr; with NETWORK_GUARD_LOG set, every attempt is appended to that file with its stack.
The baseline run of 2026-10-13 caught 86 real Resend deliveries this way.

Private addresses stay open, so the services the containers reach on the compose network (Redis, which carries the deal inbox's nudges
to the live users' pages) are switched off by blanking their variables instead: blank_outside_services() before settings.py reads them.
"""
import ipaddress
import os
import socket
import sys
import traceback

# The environment variables that switch an outside service on: Resend, Gemini, WhatsApp, web push, Spaces (settings.py refuses a key
# without its bucket) and the channel layer on Redis.
OUTSIDE_SERVICES = ('RESEND_API_KEY', 'GEMINI_API_KEY', 'GEM_API_KEY', 'WHATSAPP_ACCESS_TOKEN', 'VAPID_PRIVATE_KEY', 'DO_SPACES_KEY',
                    'DO_SPACES_SECRET', 'DO_SPACES_BUCKET', 'REDIS_URL')
blocked = []  # (host, port) of every refused connection, in order
_reported = set()


def private(host):
    """True for loopback and private addresses, and for names that resolve only to them."""
    try:
        addresses = [ipaddress.ip_address(host.split('%')[0])]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(info[4][0].split('%')[0]) for info in socket.getaddrinfo(host, None)]
        except OSError:
            return False
    return bool(addresses) and all(address.is_private or address.is_loopback for address in addresses)


def refuse(address):
    host, port = address[0], address[1]
    blocked.append((host, port))
    log = os.getenv('NETWORK_GUARD_LOG')
    if log:
        with open(log, 'a', encoding='utf-8') as out:
            out.write(f'BLOQUEADA {host}:{port}\n{"".join(traceback.format_stack(limit=30))}\n')
    if (host, port) not in _reported:
        _reported.add((host, port))
        print(f'Guardia de red de las pruebas: conexión saliente bloqueada a {host}:{port}', file=sys.stderr)
    raise ConnectionRefusedError(f'Guardia de red de las pruebas: conexión saliente a {host}:{port} bloqueada')


def _guarded(original):
    def connect(self, address, *args, **kwargs):
        if self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) and not private(str(address[0])):
            refuse(address)
        return original(self, address, *args, **kwargs)
    connect.network_guard = True
    return connect


def blank_outside_services(environ=os.environ):
    """Blank every OUTSIDE_SERVICES variable, so the settings read afterwards configure none of them; returns the names that had a value."""
    had = [name for name in OUTSIDE_SERVICES if environ.get(name)]
    for name in OUTSIDE_SERVICES:
        environ[name] = ''
    return had


def install():
    """Idempotent: wraps socket.connect and socket.connect_ex for the whole process."""
    if getattr(socket.socket.connect, 'network_guard', False):
        return
    socket.socket.connect = _guarded(socket.socket.connect)
    socket.socket.connect_ex = _guarded(socket.socket.connect_ex)
