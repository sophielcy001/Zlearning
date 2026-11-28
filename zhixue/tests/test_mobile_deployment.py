from base64 import b64encode
from hashlib import sha256
import json
import sys
from types import SimpleNamespace
import zipfile
from zipfile import ZipFile

import pytest

from deploy.mobile import package
from deploy.mobile.package import make_archive
from deploy.mobile.verify import site_files, verify_deployment


REQUIRED = "fastapi==0.1.0 --hash=sha256:fixture\n"
STATIC = {
    "dist/mobile/index.html": "<h1>智学离线</h1>",
    "dist/mobile/manifest.webmanifest": "{}",
    "dist/mobile/icon-192.png": "small icon fixture",
    "dist/mobile/icon-512.png": "large icon fixture",
    "dist/assets/phone-a123.js": "export const phone = true;",
    "dist/zhixue.png": "brand fixture",
}


@pytest.fixture
def project(tmp_path):
    fixture = dict(STATIC)
    integrity = {
        "/mobile/" if name == "dist/mobile/index.html" else name.removeprefix("dist"):
        "sha256-" + b64encode(sha256(content.encode()).digest()).decode("ascii")
        for name, content in STATIC.items()
    }
    fixture.update({
        "dist/mobile/sw.js": (
            'const CACHE = "zhixue-fixture";\nconst PRECACHE = ' + json.dumps(list(integrity)) + ";\n"
            + "const INTEGRITY = " + json.dumps(integrity) + ";\n"
        ),
        "dist/index.html": "desktop HTML",
        "dist/assets/unused.js": "unused asset",
        "backend/__init__.py": "",
        "backend/mobile_cipher.py": "# cipher fixture",
        "backend/mobile_relay.py": "# relay fixture",
        "backend/app.py": "# desktop service",
        "data/private.sqlite3": "private fixture",
        "deploy/mobile/Caddyfile": "# Caddy template",
        "deploy/mobile/README.md": "# Deployment instructions",
        "deploy/mobile/verify.py": "# Post-deployment verifier",
    })
    for name, content in fixture.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def test_mobile_archive_contains_requested_offline_assets_and_minimal_relay(project):
    result = make_archive(project, project / "output" / "mobile.zip", REQUIRED)
    with ZipFile(result) as archive:
        assert archive.testzip() is None
        names = {name for name in archive.namelist() if name != "SHA256SUMS.txt"}
        assert names == {
            *(name.replace("dist/", "site/", 1) for name in STATIC),
            "site/mobile/sw.js", "backend/__init__.py", "backend/mobile_cipher.py",
            "backend/mobile_relay.py", "Caddyfile", "README.md", "requirements.txt", "verify.py",
        }
        assert archive.read("requirements.txt") == REQUIRED.encode()
        assert archive.read("site/mobile/sw.js") == (project / "dist/mobile/sw.js").read_bytes()
        manifest = dict(line.rsplit("  ", 1)[::-1] for line in archive.read("SHA256SUMS.txt").decode().splitlines())
        assert manifest == {name: sha256(archive.read(name)).hexdigest() for name in names}


def test_missing_offline_asset_preserves_existing_deployment_package(project):
    output = project / "mobile.zip"
    output.write_bytes(b"previous release")
    (project / "dist/assets/phone-a123.js").unlink()
    with pytest.raises(ValueError, match="phone-a123"):
        make_archive(project, output, REQUIRED)
    assert output.read_bytes() == b"previous release"


def test_packager_rejects_worker_paths_outside_the_phone_site(project):
    source = project / "dist/mobile/sw.js"
    source.write_text(source.read_text().replace('"/assets/phone-a123.js"', '"/assets/../outside.txt"'))
    with pytest.raises(ValueError, match="outside|path"):
        make_archive(project, project / "mobile.zip", REQUIRED)
    assert not (project / "mobile.zip").exists()


def test_mixed_version_offline_asset_preserves_existing_deployment_package(project):
    output = project / "mobile.zip"
    output.write_bytes(b"previous release")
    (project / "dist/mobile/index.html").write_text("<h1>旧版入口</h1>", encoding="utf-8")
    with pytest.raises(ValueError, match=r"Version mismatch.*mobile/index.html"):
        make_archive(project, output, REQUIRED)
    assert output.read_bytes() == b"previous release"


def test_mobile_archive_has_identical_bytes_when_rebuilt_at_a_different_time(project, monkeypatch):
    clock = (2026, 11, 20, 10, 20, 30)
    monkeypatch.setattr(zipfile, "time", SimpleNamespace(time=lambda: 0, localtime=lambda _: clock))
    first = make_archive(project, project / "first.zip", REQUIRED)
    clock = (2027, 1, 10, 12, 0, 0)
    second = make_archive(project, project / "second.zip", REQUIRED)
    assert first.read_bytes() == second.read_bytes()


def test_packaging_cli_writes_plain_requirements_with_forced_terminal_color(project, monkeypatch):
    output = project / "mobile.zip"
    monkeypatch.setattr(package, "ROOT", project)
    monkeypatch.setattr(sys, "argv", ["package.py", "--output", str(output)])
    monkeypatch.setenv("FORCE_COLOR", "1")

    def export(command, **_options):
        plain = "--color" in command and command[command.index("--color") + 1] == "never"
        return SimpleNamespace(stdout=REQUIRED if plain else "\x1b[1m" + REQUIRED + "\x1b[0m")

    monkeypatch.setattr(package.subprocess, "run", export)
    package.main()
    with ZipFile(output) as archive:
        assert archive.read("requirements.txt") == REQUIRED.encode()


def test_verifier_rejects_tampered_reference_before_opening_connections(project):
    package = make_archive(project, project / "mobile.zip", REQUIRED)
    reference = project / "unpacked"
    with ZipFile(package) as archive:
        archive.extractall(reference)
    ready, _ = site_files(reference)
    assert ready["mobile/sw.js"][1] > 0
    (reference / "site/mobile/index.html").write_text("old version", encoding="utf-8")
    with pytest.raises(ValueError, match="Local release digest mismatch: site/mobile/index.html"):
        verify_deployment(reference, "https://phone.example", "https://relay.example")


def test_verifier_rejects_unrelated_paths_or_one_shared_host(project):
    with pytest.raises(ValueError, match="HTTPS origin"):
        verify_deployment(project, "https://phone.example/login", "https://relay.example")
    with pytest.raises(ValueError, match="distinct hosts"):
        verify_deployment(project, "https://example.org:443", "https://example.org:444")


@pytest.fixture
def dual_stack_release(project, monkeypatch):
    from contextlib import ExitStack
    from datetime import UTC, datetime, timedelta
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import socket
    import ssl
    from threading import Thread

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from fastapi.testclient import TestClient

    from backend.mobile_relay import create_relay

    phone_host, relay_host = 'phone.qa.test', 'relay.qa.test'
    reference = project / 'unpacked'
    deployment = make_archive(project, project / 'mobile.zip', REQUIRED)
    with ZipFile(deployment) as archive:
        archive.extractall(reference)
    files, _ = site_files(reference)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    identity = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Zhixue deployment test')])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(identity).issuer_name(identity)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(phone_host), x509.DNSName(relay_host)]), critical=False)
            .sign(key, hashes.SHA256()))
    certificate = project / 'qa-ca.pem'
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private = project / 'qa-key.pem'
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private)
    visited = []
    seen_names = []
    context.set_servername_callback(lambda _socket, name, _context: seen_names.append(name))
    issue = SimpleNamespace(stale_ipv6=False, blocked_ipv6_route=None, strip_ipv6_preflight=False,
                            corrupt_ipv6_answer=False)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *_args):
            pass

        def serve_request(self):
            v6 = self.server.address_family == socket.AF_INET6
            host = self.headers['Host']
            visited.append((v6, host, self.path))
            status, headers, body = 404, {}, b''
            if host == phone[8:]:
                if self.path == '/':
                    status, headers = 302, {'Location': '/mobile/'}
                else:
                    path = 'mobile/index.html' if self.path == '/mobile/' else self.path[1:]
                    if path in files:
                        body = (reference / 'site' / path).read_bytes()
                        if path == 'mobile/index.html' and v6 and issue.stale_ipv6:
                            body = b'earlier release'
                        cache = 'public, max-age=31536000, immutable' if path.startswith('assets/') else 'no-cache'
                        status, headers = 200, {'Cache-Control': cache}
            elif host == relay[8:] and (self.path == '/healthz' or self.path.startswith('/v1/')):
                status, headers, body = self.relay_response(v6)
            self.send_response(status)
            for label, value in headers.items():
                self.send_header(label, value)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def relay_response(self, v6):
            size = int(self.headers.get('Content-Length', 0))
            content = self.rfile.read(size)
            parts = self.path.split('/')
            route = parts[3] if len(parts) > 3 else None
            if v6 and route and route == issue.blocked_ipv6_route and self.command != 'OPTIONS':
                return 404, {}, b''
            response = relay_app.request(self.command, self.path, content=content, headers=dict(self.headers))
            headers = {key: value for key, value in response.headers.items() if key.lower() != 'content-length'}
            headers.setdefault('cache-control', 'no-store')  # Caddy adds this even on relay rejections.
            if v6 and route == 'exchange' and self.command == 'OPTIONS' and issue.strip_ipv6_preflight:
                headers.pop('access-control-allow-headers', None)
            body = response.content
            if v6 and route == 'exchange' and self.command == 'POST' and response.status_code == 200 and issue.corrupt_ipv6_answer:
                body = bytes(len(body))
            return response.status_code, headers, body

        do_GET = do_OPTIONS = do_POST = serve_request

    class RelayTestServer(ThreadingHTTPServer):
        daemon_threads = False

    class IPv6Server(RelayTestServer):
        address_family = socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            super().server_bind()

    with ExitStack() as stack:
        v4 = stack.enter_context(RelayTestServer(('127.0.0.1', 0), Handler))
        port = v4.server_port
        try:
            v6 = stack.enter_context(IPv6Server(('::1', port), Handler))
        except OSError as error:
            pytest.skip(f'Test requires both loopback address families: {error}')
        phone, relay = f'https://{phone_host}:{port}', f'https://{relay_host}:{port}'
        relay_app = stack.enter_context(TestClient(create_relay(phone)))
        for server in (v4, v6):
            # Let active relay requests finish before shutting down its TestClient.
            stack.callback(server.server_close)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            stack.callback(thread.join, 3)
            stack.callback(server.shutdown)

        original = socket.getaddrinfo

        def addresses(host, port, *args, **kwargs):
            if host in (phone_host, relay_host):
                v4 = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))
                v6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('::1', port, 0, 0))
                return [v4, v4, v6]
            return original(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, 'getaddrinfo', addresses)
        yield SimpleNamespace(reference=reference, phone=phone, relay=relay, certificate=certificate,
                              issue=issue, visited=visited, seen_names=seen_names), len(files)


def test_verifier_checks_every_distinct_address_with_the_original_host(dual_stack_release):
    release, count = dual_stack_release
    report = verify_deployment(release.reference, release.phone, release.relay, ca_file=release.certificate)
    assert report['siteFiles'] == count
    assert report['relayRoundTripVerified'] is True
    assert all(set(addresses) == {'127.0.0.1', '::1'} for addresses in report['peers'].values())
    assert set(release.seen_names) == {'phone.qa.test', 'relay.qa.test'}
    for family in (False, True):
        assert release.visited.count((family, release.phone[8:], '/mobile/')) == 1
        assert any(ipv6 == family and host == release.relay[8:] and path.endswith('/offer')
                   for ipv6, host, path in release.visited)


def test_verifier_rejects_stale_mobile_files_on_only_the_second_address(dual_stack_release):
    release, _ = dual_stack_release
    release.issue.stale_ipv6 = True
    with pytest.raises(ValueError, match=r'::1.*\/mobile/.*does not match'):
        verify_deployment(release.reference, release.phone, release.relay, ca_file=release.certificate)


def test_verifier_rejects_a_relay_address_that_has_health_but_no_pairing_route(dual_stack_release):
    release, _ = dual_stack_release
    release.issue.blocked_ipv6_route = 'offer'
    with pytest.raises(ValueError, match=r'::1.*pairing'):
        verify_deployment(release.reference, release.phone, release.relay, ca_file=release.certificate)


@pytest.mark.parametrize('route', ['wait', 'exchange', 'answer'])
def test_verifier_rejects_each_missing_relay_transfer_route(dual_stack_release, route):
    release, _ = dual_stack_release
    release.issue.blocked_ipv6_route = route
    with pytest.raises(ValueError, match=rf'::1.*Relay {route}.*HTTP 404'):
        verify_deployment(release.reference, release.phone, release.relay, ca_file=release.certificate)


def test_verifier_rejects_exchange_preflight_missing_content_type(dual_stack_release):
    release, _ = dual_stack_release
    release.issue.strip_ipv6_preflight = True
    with pytest.raises(ValueError, match=r'::1.*Relay exchange preflight'):
        verify_deployment(release.reference, release.phone, release.relay, ca_file=release.certificate)


def test_verifier_rejects_changed_answer_bytes(dual_stack_release):
    release, _ = dual_stack_release
    release.issue.corrupt_ipv6_answer = True
    with pytest.raises(ValueError, match=r'::1.*Relay exchange.*payload'):
        verify_deployment(release.reference, release.phone, release.relay, ca_file=release.certificate)


def test_default_mode_rejects_non_public_dns_before_contacting_any_listener(dual_stack_release):
    release, _ = dual_stack_release
    with pytest.raises(ValueError, match=r'non-public.*127\.0\.0\.1'):
        verify_deployment(release.reference, release.phone, release.relay)
    assert release.seen_names == []
