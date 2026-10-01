"""Real loopback responses exercise the packaged probe's exit contract."""
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/quadringent_healthcheck.py'


class HealthcheckTests(unittest.TestCase):
    def probe(self, response, delay=0, path=None):
        self.assertTrue(SCRIPT.is_file(), 'lightweight HTTP probe must exist')
        request = []
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(3)
            port = listener.getsockname()[1]
            def serve():
                with listener.accept()[0] as connection:
                    connection.settimeout(2)
                    request.append(connection.recv(4096))
                    time.sleep(delay)
                    try:
                        connection.sendall(response)
                    except OSError:
                        pass
            thread = threading.Thread(target=serve)
            thread.start()
            try:
                result = subprocess.run([sys.executable, '-S', str(SCRIPT), str(port)] + ([path] if path else []),
                                        capture_output=True, timeout=2)
            finally:
                thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        expected = (path or '/healthz').encode()
        self.assertTrue(request[0].startswith(b'GET ' + expected + b' HTTP/1.0\r\n'))
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')
        return result.returncode

    def test_accepts_http_200(self):
        for version in (b'HTTP/1.0', b'HTTP/1.1'):
            with self.subTest(version=version):
                self.assertEqual(self.probe(version+b' 200 OK\r\n\r\nx'), 0)

    def test_rejects_errors_redirects_and_invalid_status_lines(self):
        for line in (b'HTTP/1.0 503 Unavailable\r\n', b'HTTP/1.1 302 Found\r\n',
                     b'HTTP/1.0 2000 Invalid\r\n', b'junk 200 OK\r\n',
                     b'HTTP/1.0 200 OK', b'', b'HTTP/1.0 200 '+b'x'*300+b'\r\n'):
            with self.subTest(line=line[:30]):
                self.assertNotEqual(self.probe(line), 0)

    def test_rejects_timeout_without_waiting_for_kubernetes(self):
        self.assertNotEqual(self.probe(b'HTTP/1.0 200 OK\r\n', delay=1.2), 0)

    def test_rejects_connection_refused(self):
        self.assertTrue(SCRIPT.is_file())
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            port = reserved.getsockname()[1]
            result = subprocess.run([sys.executable, '-S', str(SCRIPT), str(port)],
                                    capture_output=True, timeout=2)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b'')

    def test_rejects_invalid_port_without_traceback(self):
        self.assertTrue(SCRIPT.is_file())
        for port in ('0', '-1', '65536', 'host:8844'):
            result = subprocess.run([sys.executable, '-S', str(SCRIPT), port],
                                    capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, b'')

    def test_probes_the_given_path(self):
        """Le control plane v2 écoute en loopback : sa sonde exec vise /v2/healthz
        (une sonde httpGet du kubelet, sur l'IP du Pod, n'atteint jamais 127.0.0.1)."""
        self.assertEqual(self.probe(b'HTTP/1.1 200 OK\r\n\r\n', path='/v2/healthz'), 0)

    def test_rejects_unsafe_paths_without_connecting(self):
        for path in ('v2/healthz', '/v2/healthz\r\nX: y', '/a b', '/' + 'x' * 100):
            with self.subTest(path=path[:20]):
                result = subprocess.run([sys.executable, '-S', str(SCRIPT), '8845', path],
                                        capture_output=True, timeout=2)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, b'')
