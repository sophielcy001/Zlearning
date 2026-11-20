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
