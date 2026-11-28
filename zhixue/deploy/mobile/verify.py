"""Verify a published deployment against this unpacked release."""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from hashlib import file_digest, sha256
from http.client import HTTPException, HTTPSConnection
from ipaddress import ip_address
import json
from pathlib import Path
import re
from secrets import token_bytes, token_urlsafe
import socket
import ssl
from time import monotonic, sleep
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


def resolved_endpoints(origin, public):
    parsed = urlsplit(origin)
    try:
        records = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM,
                                     proto=socket.IPPROTO_TCP)
    except OSError as error:
        raise ValueError(f"{origin}: address resolution failed: {error}") from error
    endpoints = {}
    for family, _, protocol, _, target in records:
        peer = ip_address(target[0])
        require(not public or peer.is_global, f"{origin} resolved to a non-public address: {peer}")
        endpoints[(family, target)] = (family, protocol, target)
    require(endpoints, f"{origin} did not resolve to an HTTPS address")
    return endpoints.values()


class ResolvedHTTPSConnection(HTTPSConnection):
    """Dial one address while retaining the origin's HTTP Host and TLS name."""

    def __init__(self, origin, endpoint, context):
        parsed = urlsplit(origin)
        super().__init__(parsed.hostname, port=parsed.port, context=context, timeout=8)
        self.endpoint = endpoint
        self.tls_context = context

    def connect(self):
        family, protocol, target = self.endpoint
        with socket.socket(family, socket.SOCK_STREAM, protocol) as raw:
            raw.settimeout(self.timeout)
            raw.connect(target)
            require(ip_address(raw.getpeername()[0]) == ip_address(target[0]), "TCP peer differs from the verified address")
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)


def verify_deployment(root, phone, relay, *, ca_file=None):
    phone, relay = https_origin(phone), https_origin(relay)
    require(urlsplit(phone).hostname != urlsplit(relay).hostname, "Phone and relay must use distinct hosts")
    files, manifest_digest = site_files(root.resolve())
    endpoints = {origin: resolved_endpoints(origin, ca_file is None) for origin in (phone, relay)}
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    peers = {origin: {} for origin in endpoints}

    def fetch(connection, origin, route, *, method="GET", headers=None, body=None, limit=4096):
        try:
            if connection.sock is None:
                connection.connect()
                channel = connection.sock
                peer = str(ip_address(channel.getpeername()[0]))
                peers[origin].setdefault(peer, {
                    "tls": channel.version(), "certificateExpiry": channel.getpeercert().get("notAfter"),
                })
            connection.request(method, route, body=body, headers={"Accept-Encoding": "identity", **(headers or {})})
            with closing(connection.getresponse()) as response:
                body = response.read(limit + 1)
                require(len(body) <= limit, f"{route} response exceeds expected size")
                selected = {name: response.getheader(name, "") for name in (
                    "Cache-Control", "Location", "Content-Type", "Access-Control-Allow-Origin",
                    "Access-Control-Allow-Headers", "Access-Control-Allow-Methods",
                )}
                return response.status, selected, body
        except (OSError, HTTPException) as error:
            raise ValueError(f"{route}: {error}") from error

    def cache(headers, expected, route):
        directives = {part.strip().lower() for part in headers["Cache-Control"].split(",")}
        require(set(expected).issubset(directives), f"{route} has unexpected Cache-Control: {headers['Cache-Control']}")

    def check_phone(connection):
        status, headers, _ = fetch(connection, phone, "/")
        require(status == 302 and headers["Location"] in ("/mobile/", phone + "/mobile/"),
                "Phone root did not redirect to /mobile/")
        require(fetch(connection, phone, "/healthz")[0] == 404, "Phone host must not serve the relay health route")
        for path, (expected, size) in sorted(files.items()):
            route = "/mobile/" if path == "mobile/index.html" else "/" + path
            status, headers, body = fetch(connection, phone, route, limit=size)
            require(status == 200 and sha256(body).hexdigest() == expected,
                    f"{route} does not match the release (HTTP {status})")
            if path.startswith("assets/"):
                cache(headers, ("public", "max-age=31536000", "immutable"), route)
            elif path.startswith("mobile/"):
                cache(headers, ("no-cache",), route)

    def check_relay_round_trip(connection, room, allowed):
        ticket, reply_token = token_urlsafe(32), token_urlsafe(32)
        entry = f"/v1/{room}"
        status, headers, _ = fetch(connection, relay, f"{entry}/exchange/{ticket}", method="OPTIONS", headers={
            **allowed, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "Content-Type",
        })
        methods = {part.strip() for part in headers["Access-Control-Allow-Methods"].split(",")}
        allowed_headers = {part.strip().lower() for part in headers["Access-Control-Allow-Headers"].split(",")}
        require(status in (200, 204) and headers["Access-Control-Allow-Origin"] == phone
                and "POST" in methods and "content-type" in allowed_headers,
                f"Relay exchange preflight did not allow the phone POST with Content-Type (HTTP {status})")

        octets = {"Content-Type": "application/octet-stream"}
        desktop = {"X-Reply-Token": reply_token}
        request_packet, answer_packet = token_bytes(48), token_bytes(64)

        def transfer(channel, route, expected_status, sent_headers, sent_body=None, expected_body=b""):
            label = f"Relay {route}"
            try:
                status, headers, body = fetch(channel, relay, f"{entry}/{route}/{ticket}", method="POST",
                                              headers=sent_headers, body=sent_body)
            except ValueError as error:
                raise ValueError(f"{label}: {error}") from error
            require(status == expected_status, f"{label} returned HTTP {status}, expected {expected_status}")
            cache(headers, ("no-store",), label)
            require(body == expected_body, f"{label} returned a different payload")
            if expected_body:
                content_type = headers["Content-Type"].split(";", 1)[0].strip().lower()
                require(content_type == octets["Content-Type"], f"{label} returned an unexpected Content-Type")
            if "Origin" in sent_headers:
                require(headers["Access-Control-Allow-Origin"] == phone,
                        f"{label} did not allow the phone origin")

        def transfer_on_peer(*args):
            with closing(ResolvedHTTPSConnection(relay, connection.endpoint, context)) as channel:
                transfer(channel, *args)

        with ThreadPoolExecutor(max_workers=2) as workers:
            desktop_wait = workers.submit(transfer_on_peer, "wait", 200, desktop, None, request_packet)
            deadline = monotonic() + connection.timeout
            while monotonic() < deadline:
                status, headers, body = fetch(connection, relay, f"{entry}/offer", headers=allowed)
                if status == 200:
                    require(headers["Access-Control-Allow-Origin"] == phone, "Relay offer did not allow the phone origin")
                    cache(headers, ("no-store",), "Relay offer")
                    require(json.loads(body) == {"ticket": ticket}, "Relay offer did not return the active ticket")
                    break
                if desktop_wait.done():
                    desktop_wait.result()
                require(status == 404, f"Relay offer returned HTTP {status}, expected 200 or 404")
                sleep(0.05)
            else:
                raise ValueError("Relay wait did not make the ticket available")

            phone_exchange = workers.submit(transfer_on_peer, "exchange", 200,
                                            {**allowed, **octets}, request_packet, answer_packet)
            completed, _ = wait((desktop_wait, phone_exchange), return_when=FIRST_COMPLETED)
            if phone_exchange in completed:
                phone_exchange.result()
            desktop_wait.result()
            transfer(connection, "answer", 204, {**desktop, **octets}, answer_packet)
            phone_exchange.result()

        status, headers, _ = fetch(connection, relay, f"{entry}/offer", headers=allowed)
        require(status == 404 and headers["Access-Control-Allow-Origin"] == phone,
                f"Relay offer did not close the round trip (HTTP {status})")
        cache(headers, ("no-store",), "Relay closed offer")

    def check_relay(connection):
        allowed = {"Origin": phone}
        unrelated = {"Origin": "https://unrelated-origin.invalid"}
        require(fetch(connection, relay, "/mobile/")[0] == 404, "Relay host must not serve the phone entry")
        status, headers, body = fetch(connection, relay, "/healthz", headers=allowed)
        require(status == 200 and json.loads(body) == {"status": "ready"}, "Relay is not ready")
        require(headers["Access-Control-Allow-Origin"] == phone, "Relay did not allow the phone origin")
        cache(headers, ("no-store",), "Relay health")
        status, headers, _ = fetch(connection, relay, "/healthz", method="OPTIONS", headers={
            **allowed, "Access-Control-Request-Method": "GET",
        })
        require(status in (200, 204) and headers["Access-Control-Allow-Origin"] == phone,
                "Relay preflight rejected the phone origin")
        status, headers, _ = fetch(connection, relay, "/healthz", headers=unrelated)
        require(status == 403 and not headers["Access-Control-Allow-Origin"], "Relay did not reject an unrelated origin")
        cache(headers, ("no-store",), "Relay origin rejection")

        # A fresh room has no offer. The relay's CORS response proves the request reached it.
        room = token_urlsafe(32)
        route = f"/v1/{room}/offer"
        status, headers, _ = fetch(connection, relay, route, headers=allowed)
        require(status == 404 and headers["Access-Control-Allow-Origin"] == phone,
                "Relay pairing entry did not reach the relay")
        cache(headers, ("no-store",), "Relay pairing entry")
        status, headers, _ = fetch(connection, relay, route, headers=unrelated)
        require(status == 403 and not headers["Access-Control-Allow-Origin"],
                "Relay pairing entry did not reject an unrelated origin")
        cache(headers, ("no-store",), "Relay pairing origin rejection")
        check_relay_round_trip(connection, room, allowed)

    for origin, check in ((phone, check_phone), (relay, check_relay)):
        for endpoint in endpoints[origin]:
            try:
                with closing(ResolvedHTTPSConnection(origin, endpoint, context)) as connection:
                    check(connection)
            except ValueError as error:
                raise ValueError(f"{origin} [{endpoint[2][0]}] {error}") from error
    return {
        "status": "passed", "phone": phone, "relay": relay, "siteFiles": len(files),
        "manifestSHA256": manifest_digest, "tlsTrust": "provided-ca" if ca_file else "system",
        "peers": peers, "relayPairingEntryVerified": True, "relayRoundTripVerified": True,
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
