from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import UTC, datetime
from email.message import Message
from ipaddress import ip_address, ip_network
import json
import socket
import sys
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit


MAX_URL_LENGTH = 2048
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECTS = {301, 302, 303, 307, 308}
PROXY_PLACEHOLDERS = ip_network("198.18.0.0/15")
LOCAL_DOMAINS = (".local", ".localhost", ".internal", ".home.arpa", ".test", ".example", ".invalid", ".onion")
TRANSLATED_IPV6 = tuple(ip_network(prefix) for prefix in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32"))


class WebPageError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Snapshot:
    requested_url: str
    url: str
    fetched_at: str
    encoding: str
    title: str
    body: bytes


def normalize_url(value: str) -> str:
    import idna

    value = value.strip()
    if not value or len(value) > MAX_URL_LENGTH or any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\" for char in value
    ):
        raise WebPageError("请输入完整的公开网页地址。")
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or "@" in parts.netloc
                or "%" in parts.netloc or parts.port not in {None, 443 if parts.scheme == "https" else 80}):
            raise ValueError
        host = parts.hostname.rstrip(".")
        try:
            address = ip_address(host)
        except ValueError:
            host = idna.encode(host, uts46=True, std3_rules=True).decode("ascii")
            if "." not in host:
                raise ValueError
        else:
            host = f"[{address}]" if address.version == 6 else str(address)
    except (ValueError, idna.IDNAError) as failure:
        raise WebPageError("请输入使用标准 HTTP 或 HTTPS 地址的公开网页。") from failure

    target = urlunsplit((parts.scheme, host, quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                         quote(parts.query, safe="/?%:@!$&'()*+,;=-._~[]"), ""))
    if len(target) > MAX_URL_LENGTH:
        raise WebPageError("网页地址过长，请使用较短的链接。")
    return target


def is_public(address) -> bool:
    if address.version == 6:
        if address.ipv4_mapped is not None or address.is_site_local:
            return False
        if any(address in network for network in TRANSLATED_IPV6):
            return False
    return address.is_global and not (address.is_multicast or address.is_reserved)


def https_pool(address: str, hostname: str):
    import certifi
    import urllib3

    return urllib3.HTTPSConnectionPool(
        address, port=443, server_hostname=hostname, assert_hostname=hostname,
        cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), timeout=urllib3.Timeout(connect=3, read=5),
        retries=False,
    )


def public_dns(host: str) -> tuple:
    from urllib3.exceptions import HTTPError

    try:
        with https_pool("1.1.1.1", "cloudflare-dns.com") as pool:
            for kind in (1, 28):
                query = urlencode({"name": host, "type": kind})
                with pool.request(
                    "GET", f"/dns-query?{query}", headers={"Host": "cloudflare-dns.com", "Accept": "application/dns-json"},
                    redirect=False, preload_content=False, assert_same_host=False,
                ) as response:
                    if response.status != 200:
                        break
                    payload = json.loads(response.read(64 * 1024))
                    if not isinstance(payload, dict) or payload.get("Status") != 0:
                        break
                    addresses = tuple(
                        ip_address(item["data"]) for item in payload.get("Answer", ())
                        if isinstance(item, dict) and item.get("type") == kind
                    )
                    if addresses:
                        return addresses
    except (HTTPError, OSError, ValueError, TypeError, KeyError) as failure:
        raise WebPageError("本机 DNS 返回代理占位地址，公开 DNS 暂时不可用。", 502) from failure
    raise WebPageError("公开 DNS 未返回该网站的地址，请核对域名。")


def public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        addresses = tuple(dict.fromkeys(ip_address(result[4][0]) for result in answers))
    except (OSError, ValueError) as failure:
        raise WebPageError("找不到该公开网站，请检查地址和网络。") from failure
    # Some system proxies give ordinary domains synthetic addresses for browser traffic.
    if addresses and all(address in PROXY_PLACEHOLDERS for address in addresses):
        try:
            ip_address(host)
        except ValueError:
            if not host.endswith(LOCAL_DOMAINS):
                addresses = public_dns(host)
    if not addresses or any(not is_public(address) for address in addresses):
        raise WebPageError("该地址指向本机、内网或非公开网络，无法导入。")
    # IPv4 first avoids waiting for unusable IPv6 routes on hosts without IPv6 Internet access.
    return tuple(str(address) for address in sorted(addresses, key=lambda address: address.version))


@contextmanager
def open_response(url: str, address: str):
    import urllib3

    parts = urlsplit(url)
    if parts.scheme == "https":
        pool = https_pool(address, parts.hostname)
    else:
        pool = urllib3.HTTPConnectionPool(
            address, port=80, timeout=urllib3.Timeout(connect=3, read=5), retries=False,
        )
    endpoint = urlunsplit(("", "", parts.path, parts.query, ""))
    with pool:
        with pool.request(
            "GET", endpoint, headers={
                "Host": parts.netloc,
                "Accept": "text/html, application/xhtml+xml",
                "Accept-Encoding": "identity",
                "User-Agent": "Zhixue/0.1 (public webpage import)",
            }, redirect=False, preload_content=False, assert_same_host=False,
        ) as response:
            yield response


def read_response(url: str) -> tuple[str | None, str, bytes]:
    from urllib3.exceptions import HTTPError

    parts = urlsplit(url)
    addresses = public_addresses(parts.hostname, 443 if parts.scheme == "https" else 80)
    last_error = None
    for address in addresses[:4]:
        try:
            with open_response(url, address) as response:
                if response.status in REDIRECTS:
                    location = response.headers.get("Location")
                    if not location:
                        raise WebPageError("网页跳转地址缺失。")
                    return location, "", b""
                if response.status != 200:
                    raise WebPageError(f"网页返回 HTTP {response.status}，请检查链接后重试。")

                content = Message()
                content["Content-Type"] = response.headers.get("Content-Type", "")
                if content.get_content_type() not in {"text/html", "application/xhtml+xml"}:
                    raise WebPageError("网页导入需要 HTML 响应。文件链接可下载后按文件类型导入。", 415)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise WebPageError("网站返回了无法保存的压缩格式，可通过浏览器保存网页后导入。")
                length = response.headers.get("Content-Length", "")
                if length.isascii() and length.isdecimal() and (
                    len(length.lstrip("0")) > len(str(MAX_HTML_BYTES)) or int(length.lstrip("0") or "0") > MAX_HTML_BYTES
                ):
                    raise WebPageError("网页超过 5 MiB，可选择较短的页面。", 413)
                body = response.read(MAX_HTML_BYTES + 1, decode_content=False)
                if len(body) > MAX_HTML_BYTES:
                    raise WebPageError("网页超过 5 MiB，可选择较短的页面。", 413)
                if not body:
                    raise WebPageError("网页响应为空，可以检查网址后重试。")
                return None, content.get_content_charset("")[:100], body
        except (HTTPError, OSError) as failure:
            last_error = failure
    raise WebPageError("无法连接公开网页。请检查网络后重试。", 502) from last_error


def fetch_snapshot(value: str) -> Snapshot:
    from .web_page import page_title

    requested = url = normalize_url(value)
    visited = set()
    for _ in range(MAX_REDIRECTS + 1):
        if url in visited:
            break
        visited.add(url)
        location, declared, body = read_response(url)
        if location is None:
            title, encoding = page_title(body, declared)
            return Snapshot(requested, url, datetime.now(UTC).isoformat(), encoding,
                            title or urlsplit(url).hostname, body)
        url = normalize_url(urljoin(url, location))
    raise WebPageError("网页跳转过多，可以直接导入最终网页地址。")


def write_snapshot(request: bytes) -> None:
    try:
        fields = asdict(fetch_snapshot(request.decode("utf-8")))
    except WebPageError as error:
        fields, body = {"error": str(error), "status": error.status}, b""
    else:
        body = fields.pop("body")
    sys.stdout.buffer.write(json.dumps(fields, ensure_ascii=False).encode("utf-8") + b"\n" + body)


def download_page(url: str) -> Snapshot:
    from .documents import run_parser

    result = run_parser(url.encode("utf-8"), "fetch-web")
    header, _, body = result.partition(b"\n")
    fields = json.loads(header)
    if "error" in fields:
        raise WebPageError(fields["error"], fields["status"])
    return Snapshot(**fields, body=body)
