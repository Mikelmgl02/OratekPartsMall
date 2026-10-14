"""Guards the inventory refactor keeps on (plan §1.4). The test settings' network guard comes first; the later stages add theirs (Part
filters on estado outside lines.py, Reference deletes outside references.py, new PartCode uses, bulk writes outside the choke points)."""
import io
import socket
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from config import network_guard


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
