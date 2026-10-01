"""Bounded loopback HTTP probe; run with python -S to avoid site imports.

Checks the dedicated /healthz handler — exempt from authentication so the
probe keeps working when the site enables OIDC — not merely whether a TCP
port is open. An optional second argument selects the path (``/v2/healthz``
for the v2 control plane, which also listens on loopback only). No external dependencies, credentials, response bodies or
diagnostic output.
"""
import re
import socket
import sys
import time

SAFE_PATH = re.compile(r'^/[A-Za-z0-9/_.-]{0,63}\Z')


def check(port, path='/healthz'):
    if not 1 <= port <= 65535 or not SAFE_PATH.match(path):
        return False
    deadline = time.monotonic() + 1.0
    with socket.create_connection(('127.0.0.1', port), timeout=1.0) as connection:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.sendall(b'GET ' + path.encode('ascii') + b' HTTP/1.0\r\nHost: localhost\r\n\r\n')
        data = b''
        while b'\r\n' not in data and len(data) < 256:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            connection.settimeout(remaining)
            chunk = connection.recv(256 - len(data))
            if not chunk:
                return False
            data += chunk
        if time.monotonic() > deadline or b'\r\n' not in data:
            return False
        parts = data.split(b'\r\n', 1)[0].split(b' ', 2)
        return len(parts) == 3 and parts[0] in (b'HTTP/1.0', b'HTTP/1.1') and parts[1] == b'200'


def main():
    try:
        if len(sys.argv) > 3:
            return 1
        port = int(sys.argv[1]) if len(sys.argv) >= 2 else 8844
        path = sys.argv[2] if len(sys.argv) == 3 else '/healthz'
        return 0 if check(port, path) else 1
    except (OSError, ValueError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
