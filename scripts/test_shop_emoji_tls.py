"""Connection-level pinning/TLS tests on a local HTTPS server (no external I/O)."""

import http.server
import ipaddress
import ssl
import subprocess
import threading

import pytest

from utils import shop_emoji as SE


@pytest.mark.parametrize("hostname", ["localhost"])
def test_pinned_connection_keeps_host_and_verifies_tls_hostname(tmp_path, monkeypatch, hostname):
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
    ], check=True, capture_output=True)

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            body = b"bounded-local-image"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return None

    class QuietServer(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            return None

    server = QuietServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certfile=str(cert), keyfile=str(key))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    real_validate = SE._validate_public_ip

    def allow_test_loopback(value):
        ip = value if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ipaddress.ip_address(value)
        if ip.is_loopback:
            return ip
        return real_validate(ip)

    # Only the address policy is relaxed for this local fixture. The production
    # resolver/pinning and TLS verification code remains unchanged.
    monkeypatch.setattr(SE, "_validate_public_ip", allow_test_loopback)
    client_tls = ssl.create_default_context(cafile=str(cert))
    real_connector = SE.aiohttp.TCPConnector

    def trusted_connector(*args, **kwargs):
        kwargs["ssl"] = client_tls
        return real_connector(*args, **kwargs)

    monkeypatch.setattr(SE.aiohttp, "TCPConnector", trusted_connector)
    monkeypatch.setenv("DISCORD_TOKEN", "bot-secret-must-not-leak")
    try:
        body = SE.asyncio.run(SE.fetch_source_image(f"https://{hostname}:{port}/image.png"))
        assert body == b"bounded-local-image"
        assert len(seen) == 1
        assert seen[0]["Host"] == f"localhost:{port}"
        assert "Authorization" not in seen[0]
        assert "Proxy-Authorization" not in seen[0]
        assert "Cookie" not in seen[0]

        # The certificate is valid only for localhost. Connecting to the same
        # pinned socket by a different URL hostname must not disable TLS checks.
        with pytest.raises(SE.SourceImageError) as error:
            SE.asyncio.run(SE.fetch_source_image(f"https://127.0.0.1:{port}/image.png"))
        assert error.value.code == "source_fetch_failed"
        assert len(seen) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
