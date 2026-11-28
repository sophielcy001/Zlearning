"""Compare the mobile assets inside a frozen desktop service to a deployment ZIP."""
from argparse import ArgumentParser
from base64 import b64encode
from hashlib import file_digest, sha256
import json
from pathlib import Path
import re
from zipfile import BadZipFile, ZipFile

from PyInstaller.archive.readers import CArchiveReader

from deploy.mobile.package import ARTIFACT_INPUTS, mobile_integrity


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return file_digest(file, "sha256").hexdigest()


def deployment_assets(bundle: Path) -> dict[str, str]:
    with ZipFile(bundle) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or "SHA256SUMS.txt" not in names:
            raise ValueError("Invalid deployment ZIP file list.")
        manifest = {}
        for line in archive.read("SHA256SUMS.txt").decode("utf-8").splitlines():
            expected, separator, name = line.partition("  ")
            if not separator or not re.fullmatch(r"[a-f0-9]{64}", expected) or name in manifest:
                raise ValueError("Invalid deployment ZIP checksum manifest.")
            manifest[name] = expected
        if set(manifest) != set(names) - {"SHA256SUMS.txt"} or "site/mobile/sw.js" not in manifest:
            raise ValueError("Deployment ZIP and its checksum manifest have different files.")

        worker = archive.read("site/mobile/sw.js")
        precache = {
            "mobile/index.html" if route == "/mobile/" else route[1:]: expected
            for route, expected in mobile_integrity(worker).items()
        }
        site = {f"site/{name}" for name in precache} | {"site/mobile/sw.js"}
        if set(manifest) != site | ARTIFACT_INPUTS.keys() | {"requirements.txt"}:
            raise ValueError("Deployment ZIP does not contain the complete mobile site and relay files.")
        for name, expected in manifest.items():
            with archive.open(name) as file:
                if file_digest(file, "sha256").hexdigest() != expected:
                    raise ValueError(f"Deployment ZIP checksum mismatch: {name}")
        for name, expected in precache.items():
            encoded = b64encode(bytes.fromhex(manifest[f"site/{name}"])).decode("ascii")
            if f"sha256-{encoded}" != expected:
                raise ValueError(f"Deployment worker and site disagree: {name}")
        return {name.removeprefix("site/"): manifest[name] for name in sorted(site)}


def check_resources(service: Path, bundle: Path, *, export_site: Path | None = None) -> dict:
    service_sha, deployment_sha = digest(service), digest(bundle)
    expected = deployment_assets(bundle)
    archive = CArchiveReader(str(service))
    entries = {name.replace("\\", "/"): name for name in archive.toc}
    if len(entries) != len(archive.toc):
        raise ValueError("Packaged service contains ambiguous file paths.")
    missing = sorted(name for name in expected if f"dist/{name}" not in entries)
    if missing:
        raise ValueError("Missing packaged mobile resource: " + ", ".join(missing))
    verified = {}
    for name, checksum in expected.items():
        content = archive.extract(entries[f"dist/{name}"])
        if sha256(content).hexdigest() != checksum:
            raise ValueError(f"Packaged mobile resource differs from deployment: {name}")
        if export_site is not None:
            verified[name] = content
    if digest(service) != service_sha or digest(bundle) != deployment_sha:
        raise ValueError("A verified input changed during packaged mobile resource validation.")
    if export_site is not None:
        export_site.mkdir()
        for name, content in verified.items():
            target = export_site / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as file:
                file.write(content)
    return {"serviceSHA256": service_sha, "deploymentSHA256": deployment_sha, "matchedResources": expected}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("service", type=Path)
    parser.add_argument("deployment", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--export-site", type=Path, help="write checked mobile assets to a fresh directory")
    args = parser.parse_args()
    try:
        result = check_resources(args.service, args.deployment, export_site=args.export_site)
        with args.report.open("x", encoding="utf-8") as report:
            json.dump(result, report, indent=2)
            report.write("\n")
    except (OSError, ValueError, BadZipFile) as error:
        parser.exit(1, str(error) + "\n")
    print(f"Matched {len(result['matchedResources'])} mobile assets in the packaged service.")


if __name__ == "__main__":
    main()

