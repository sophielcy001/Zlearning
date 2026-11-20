"""Build the standalone two-origin mobile deployment archive."""
import argparse
from base64 import b64encode
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[2]
SITE_REQUIRED = {
    "/mobile/", "/mobile/manifest.webmanifest", "/mobile/icon-192.png", "/mobile/icon-512.png", "/zhixue.png",
}
PART = re.compile(r"[A-Za-z0-9._-]+")
ARTIFACT_INPUTS = {
    "backend/__init__.py": "backend/__init__.py",
    "backend/mobile_cipher.py": "backend/mobile_cipher.py",
    "backend/mobile_relay.py": "backend/mobile_relay.py",
    "Caddyfile": "deploy/mobile/Caddyfile",
    "README.md": "deploy/mobile/README.md",
    "verify.py": "deploy/mobile/verify.py",
}


def read_input(root: Path, relative: str) -> bytes:
    file = root / relative
    if file.resolve() != file:
        raise ValueError(f"Deployment file must be a regular file at its build path: {relative}")
    if not file.is_file():
        raise ValueError(f"Missing deployment file: {relative}")
    return file.read_bytes()


def worker_value(lines: list[str], name: str):
    prefix = f"const {name} = "
    declaration = next((line for line in lines if line.startswith(prefix)), "")
    if not declaration.endswith(";"):
        raise ValueError("Run npm run build to create the mobile Service Worker and its integrity manifest.")
    return json.loads(declaration.removeprefix(prefix).removesuffix(";"))


def mobile_integrity(worker: bytes) -> dict[str, str]:
    lines = worker.decode("utf-8").splitlines()
    paths = worker_value(lines, "PRECACHE")
    if not isinstance(paths, list):
        raise ValueError("Invalid mobile Service Worker asset list.")
    for route in paths:
        if not isinstance(route, str):
            raise ValueError("Invalid mobile asset path.")
        if route in SITE_REQUIRED:
            continue
        if not route.startswith(("/assets/", "/mobile/")) or any(
            part in (".", "..") or not PART.fullmatch(part) for part in route[1:].split("/")
        ):
            raise ValueError(f"Unsupported mobile asset path: {route}")
    missing = SITE_REQUIRED.difference(paths)
    if missing:
        raise ValueError("Mobile Service Worker is missing entry resources: " + ", ".join(sorted(missing)))
    integrity = worker_value(lines, "INTEGRITY")
    if not isinstance(integrity, dict) or integrity.keys() != set(paths) or any(
        not isinstance(value, str) for value in integrity.values()
    ):
        raise ValueError("Invalid mobile Service Worker integrity manifest.")
    return integrity


def make_archive(root: Path, output: Path, requirements: str) -> Path:
    root = root.resolve()
    worker = read_input(root, "dist/mobile/sw.js")
    contents = {}
    for route, expected in mobile_integrity(worker).items():
        path = "mobile/index.html" if route == "/mobile/" else route[1:]
        data = read_input(root, f"dist/{path}")
        digest = "sha256-" + b64encode(sha256(data).digest()).decode("ascii")
        if digest != expected:
            raise ValueError(f"Version mismatch between mobile Service Worker and dist/{path}; rebuild the complete site.")
        contents[f"site/{path}"] = data
    contents["site/mobile/sw.js"] = worker
    contents.update({name: read_input(root, path) for name, path in ARTIFACT_INPUTS.items()})
    contents["requirements.txt"] = requirements.encode("utf-8")
    manifest = "".join(f"{sha256(data).hexdigest()}  {name}\n" for name, data in sorted(contents.items()))

    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".mobile-archive-", dir=output.parent) as work:
        temporary = Path(work) / output.name
        with ZipFile(temporary, "w") as archive:
            entries = [*sorted(contents.items()), ("SHA256SUMS.txt", manifest.encode("utf-8"))]
            for name, data in entries:
                # Keep member metadata independent of the build clock and host platform.
                entry = ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, data, compress_type=ZIP_DEFLATED)
        temporary.replace(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="生成智学手机 HTTPS 部署包")
    parser.add_argument("--output", type=Path, default=ROOT / "release/mobile/zhixue-mobile-https.zip")
    args = parser.parse_args()
    requirements = subprocess.run(
        ["uv", "--color", "never", "export", "--locked", "--only-group", "mobile-relay", "--no-emit-project",
         "--no-header", "--format", "requirements.txt"],
        cwd=ROOT, check=True, stdout=subprocess.PIPE, text=True, encoding="utf-8",
    ).stdout
    print(make_archive(ROOT, args.output, requirements))


if __name__ == "__main__":
    main()
