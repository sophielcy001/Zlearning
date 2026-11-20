"""Verify a published deployment against this unpacked release."""
import argparse
from contextlib import closing
from hashlib import file_digest, sha256
from http.client import HTTPException, HTTPSConnection
from ipaddress import ip_address
import json
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit


def require(condition, message):
    if not condition:
        raise ValueError(message)


def https_origin(value):
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    require(parsed.scheme == "https" and re.fullmatch(r"[a-z0-9.-]+", host)
            and parsed.username is None and parsed.path in ("", "/")
            and not parsed.query and not parsed.fragment and parsed.port != 0,
            "Use an HTTPS origin with a host and optional port")
    return f"https://{host}" + (f":{parsed.port}" if parsed.port not in (None, 443) else "")


def site_files(root):
    manifest = (root / "SHA256SUMS.txt").read_bytes()
    files = {}
    for line in manifest.decode("utf-8").splitlines():
        expected, separator, name = line.partition("  ")
        if not name.startswith("site/"):
            continue
        path = name.removeprefix("site/")
        parts = path.split("/")
        allowed = path == "zhixue.png" or (len(parts) > 1 and parts[0] in ("assets", "mobile"))
        require(separator and allowed and re.fullmatch(r"[a-f0-9]{64}", expected)
                and all(part not in (".", "..") and re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts)
                and path not in files, f"Invalid site entry in SHA256SUMS.txt: {name}")
        local = root / name
        require(local.resolve() == local and local.is_file(), f"Missing or linked release file: {name}")
        with local.open("rb") as stream:
            require(file_digest(stream, "sha256").hexdigest() == expected, f"Local release digest mismatch: {name}")
        files[path] = (expected, local.stat().st_size)
    require({"mobile/index.html", "mobile/sw.js", "mobile/manifest.webmanifest",
             "mobile/icon-192.png", "mobile/icon-512.png", "zhixue.png"}.issubset(files),
            "Release manifest is missing required mobile files")
    return files, sha256(manifest).hexdigest()


def verify_deployment(root, phone, relay, *, ca_file=None):
    phone, relay = https_origin(phone), https_origin(relay)
    require(urlsplit(phone).hostname != urlsplit(relay).hostname, "Phone and relay must use distinct hosts")
    files, manifest_digest = site_files(root.resolve())
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    peers = {}

    def fetch(origin, route, *, method="GET", headers=None, limit=4096):
        address = urlsplit(origin)
        try:
            with closing(HTTPSConnection(address.hostname, port=address.port, context=context, timeout=8)) as connection:
                connection.connect()
                socket = connection.sock
                peer = ip_address(socket.getpeername()[0])
                require(ca_file is not None or peer.is_global, f"{origin} connected to a non-public address: {peer}")
                certificate = socket.getpeercert()
                peers.setdefault(origin, {})[str(peer)] = {
                    "tls": socket.version(), "certificateExpiry": certificate.get("notAfter"),
                }
                connection.request(method, route, headers={"Accept-Encoding": "identity", **(headers or {})})
                with closing(connection.getresponse()) as response:
                    body = response.read(limit + 1)
                    require(len(body) <= limit, f"{origin}{route} response exceeds expected size")
                    selected = {name: response.getheader(name, "") for name in (
                        "Cache-Control", "Location", "Access-Control-Allow-Origin",
                    )}
                    return response.status, selected, body
        except (OSError, HTTPException) as error:
            raise ValueError(f"{origin}{route}: {error}") from error

    def cache(headers, expected, route):
        directives = {part.strip().lower() for part in headers["Cache-Control"].split(",")}
        require(set(expected).issubset(directives), f"{route} has unexpected Cache-Control: {headers['Cache-Control']}")

    status, headers, _ = fetch(phone, "/")
    require(status == 302 and headers["Location"] in ("/mobile/", phone + "/mobile/"), "Phone root did not redirect to /mobile/")
    require(fetch(phone, "/healthz")[0] == 404, "Phone host must not serve the relay health route")
    require(fetch(relay, "/mobile/")[0] == 404, "Relay host must not serve the phone entry")
    for path, (expected, size) in sorted(files.items()):
        route = "/mobile/" if path == "mobile/index.html" else "/" + path
        status, headers, body = fetch(phone, route, limit=size)
        require(status == 200 and sha256(body).hexdigest() == expected,
                f"{route} does not match the release (HTTP {status})")
        if path.startswith("assets/"):
            cache(headers, ("public", "max-age=31536000", "immutable"), route)
        elif path.startswith("mobile/"):
            cache(headers, ("no-cache",), route)

    status, headers, body = fetch(relay, "/healthz", headers={"Origin": phone})
    require(status == 200 and json.loads(body) == {"status": "ready"}, "Relay is not ready")
    require(headers["Access-Control-Allow-Origin"] == phone, "Relay did not allow the phone origin")
    cache(headers, ("no-store",), "Relay health")
    status, headers, _ = fetch(relay, "/healthz", method="OPTIONS", headers={
        "Origin": phone, "Access-Control-Request-Method": "GET",
    })
    require(status in (200, 204) and headers["Access-Control-Allow-Origin"] == phone, "Relay preflight rejected the phone origin")
    status, headers, _ = fetch(relay, "/healthz", headers={"Origin": "https://unrelated-origin.invalid"})
    require(status == 403 and not headers["Access-Control-Allow-Origin"], "Relay did not reject an unrelated origin")
    cache(headers, ("no-store",), "Relay origin rejection")
    return {
        "status": "passed", "phone": phone, "relay": relay, "siteFiles": len(files),
        "manifestSHA256": manifest_digest, "tlsTrust": "provided-ca" if ca_file else "system",
        "peers": peers,
    }


def main():
    parser = argparse.ArgumentParser(description="校验已部署的智学手机网页与中继")
    parser.add_argument("--phone", required=True, help="手机 HTTPS 来源")
    parser.add_argument("--relay", required=True, help="中继 HTTPS 来源")
    parser.add_argument("--ca-file", type=Path, help="本地联调使用的 CA 证书")
    args = parser.parse_args()
    try:
        result = verify_deployment(Path(__file__).resolve().parent, args.phone, args.relay, ca_file=args.ca_file)
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
