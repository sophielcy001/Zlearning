from collections.abc import Callable
from contextlib import suppress
from http.client import HTTPException as HTTPClientError, HTTPSConnection
import json
from time import monotonic
from urllib.parse import urlsplit


PROTOCOL = "2025-06-18"
SUPPORTED = {"2025-03-26", "2025-06-18", "2025-11-25"}
MAX_RESPONSE = 256 * 1024
ENDPOINTS = {
    "github": "https://api.githubcopilot.com/mcp/readonly",
    "gitlab": "https://gitlab.com/api/v4/mcp",
}
TOOLS = {
    "github": {"issue": "issue_read", "merge": "pull_request_read"},
    "gitlab": {"issue": "get_work_item", "merge": "get_merge_request"},
}
Transport = Callable[[str, str, dict, dict | None], tuple[int, dict, object]]


class McpError(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def sse_message(response, request_id):
    remaining = MAX_RESPONSE
    deadline = monotonic() + 12
    lines = []
    while remaining > 0 and monotonic() < deadline:
        line = response.readline(remaining + 1)
        remaining -= len(line)
        if not line or remaining < 0:
            break
        if line.startswith(b"data:"):
            lines.append(line[5:].strip(b"\r\n").lstrip(b" "))
        elif not line.strip() and lines:
            message = json.loads(b"\n".join(lines))
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
            lines.clear()
    raise McpError("response")


def request_mcp(method: str, endpoint: str, headers: dict, message: dict | None = None):
    if endpoint not in ENDPOINTS.values():
        raise McpError("configuration")
    address = urlsplit(endpoint)
    connection = HTTPSConnection(address.hostname, timeout=8)
    try:
        body = json.dumps(message).encode("utf-8") if message is not None else None
        connection.request(method, address.path, body, headers)
        response = connection.getresponse()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        if response.status != 200 or not message or "id" not in message:
            return response.status, response_headers, None
        content = response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content == "text/event-stream":
            return response.status, response_headers, sse_message(response, message["id"])
        if content != "application/json":
            raise McpError("response")
        body = response.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise McpError("response")
        return response.status, response_headers, json.loads(body)
    except (OSError, HTTPClientError):
        raise McpError("network") from None
    except (ValueError, UnicodeError, RecursionError):
        raise McpError("response") from None
    finally:
        connection.close()


class McpSession:
    def __init__(self, provider, token, transport):
        self.provider, self.transport = provider, transport
        self.request_id = 0
        tools = ",".join(TOOLS[provider].values())
        self.headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if provider == "github":
            self.headers.update({"X-MCP-Readonly": "true", "X-MCP-Tools": tools})
        else:
            self.headers["X-Gitlab-Enabled-Mcp-Server-Tools"] = tools
        self.version = None

    def send(self, method, message=None):
        try:
            status, headers, result = self.transport(method, ENDPOINTS[self.provider], dict(self.headers), message)
        except (OSError, HTTPClientError):
            raise McpError("network") from None
        if status in (401, 403):
            raise McpError("credential")
        if status == 429:
            raise McpError("rate")
        if status not in (200, 202, 204) and method != "DELETE":
            raise McpError("network" if status >= 500 else "response")
        return status, headers, result

    def rpc(self, method, params):
        self.request_id += 1
        status, headers, response = self.send("POST", {
            "jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params,
        })
        if (status != 200 or not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
                or response.get("id") != self.request_id or not isinstance(response.get("result"), dict)):
            raise McpError("tool" if method == "tools/call" else "response")
        return response["result"], headers

    def __enter__(self):
        try:
            info, headers = self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                  "clientInfo": {"name": "Zhixue", "version": "0.1.0"}})
            protocol = info.get("protocolVersion")
            session = headers.get("mcp-session-id")
            if session is not None:
                if len(session) > 512 or not session or any(not 0x21 <= ord(char) <= 0x7e for char in session):
                    raise McpError("response")
                self.headers["Mcp-Session-Id"] = session
            if not isinstance(protocol, str) or protocol not in SUPPORTED:
                raise McpError("protocol")
            self.headers["MCP-Protocol-Version"] = protocol
            server = info.get("serverInfo", {})
            version = server.get("version") if isinstance(server, dict) else None
            if isinstance(version, str) and version.isascii() and version.isprintable():
                self.version = version[:80]
            status, _, _ = self.send("POST", {"jsonrpc": "2.0", "method": "notifications/initialized"})
            if status not in (200, 202, 204):
                raise McpError("response")
            return self
        except McpError:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        if "Mcp-Session-Id" in self.headers:
            with suppress(McpError):
                self.send("DELETE")

    def tools(self):
        known = set(TOOLS[self.provider].values())
        names, cursors = set(), set()
        params = {}
        for _ in range(4):
            result, _ = self.rpc("tools/list", params)
            tools = result.get("tools")
            if not isinstance(tools, list) or any(not isinstance(item, dict) for item in tools):
                raise McpError("response")
            names.update(item["name"] for item in tools if isinstance(item.get("name"), str))
            cursor = result.get("nextCursor")
            if known <= names or cursor is None:
                return [name for name in TOOLS[self.provider].values() if name in names]
            if not isinstance(cursor, str) or len(cursor) > 1000 or cursor in cursors:
                raise McpError("response")
            cursors.add(cursor)
            params = {"cursor": cursor}
        raise McpError("response")

    def read(self, repository, kind, number):
        if self.provider == "github":
            owner, repo = repository.split("/")
            identifier = "issue_number" if kind == "issue" else "pullNumber"
            args = {"method": "get", "owner": owner, "repo": repo, identifier: number}
        else:
            identifier = "work_item_iid" if kind == "issue" else "merge_request_iid"
            args = {"project_id": repository, identifier: number}
        result, _ = self.rpc("tools/call", {"name": TOOLS[self.provider][kind], "arguments": args})
        if result.get("isError"):
            raise McpError("tool")
        return result
