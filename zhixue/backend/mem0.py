"""HTTP operations for the Mem0 Platform connector."""

from collections.abc import Callable
from http.client import HTTPException as HTTPClientError, HTTPSConnection
import json
from uuid import UUID


RECIPIENT = "https://api.mem0.ai"
ADD_PATH = "/v3/memories/add/"
LIST_PATH = "/v3/memories/?page=1&page_size=100"
Transport = Callable[[str, str, str, dict | None], tuple[int, object]]


class ExternalRequestError(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def request_mem0(method: str, path: str, key: str, body: dict | None = None) -> tuple[int, object]:
    # Fixed TLS destination. HTTPConnection does not follow redirects.
    connection = HTTPSConnection("api.mem0.ai", timeout=8)
    try:
        try:
            connection.connect()
        except (OSError, HTTPClientError):
            raise ExternalRequestError("not_sent") from None
        headers = {"Authorization": "Token " + key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, json.dumps(body).encode("utf-8") if body is not None else None, headers)
        response = connection.getresponse()
        payload = response.read(256 * 1024 + 1)
        if len(payload) > 256 * 1024:
            return response.status, None
        try:
            return response.status, json.loads(payload) if payload else None
        except (ValueError, UnicodeError):
            return response.status, None
    finally:
        connection.close()


def call(transport, method, path, key, body=None):
    try:
        return transport(method, path, key, body)
    except (OSError, HTTPClientError):
        raise ExternalRequestError("network") from None


def user_identity(identity):
    return "zhixue-" + identity


def copy_ids(results):
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise ExternalRequestError("invalid_response")
    try:
        identities = [str(UUID(item["id"])) for item in results]
    except (TypeError, KeyError, ValueError, AttributeError):
        raise ExternalRequestError("invalid_response") from None
    if len(set(identities)) != len(identities):
        raise ExternalRequestError("invalid_response")
    return identities


def add_copy(transport, key, identity, content):
    status, result = call(transport, "POST", ADD_PATH, key, {
        "messages": [{"role": "user", "content": content}],
        "infer": False, "immutable": True, "user_id": user_identity(identity),
    })
    if status in (400, 401, 403, 422):
        raise ExternalRequestError("rejected")
    if status != 200 or not isinstance(result, dict) or result.get("status") != "SUCCEEDED":
        raise ExternalRequestError("unknown_upload")
    ids = copy_ids(result.get("results"))
    if not ids:
        raise ExternalRequestError("unknown_upload")
    return ids


def find_copies(transport, key, identity):
    user = user_identity(identity)
    status, result = call(transport, "POST", LIST_PATH, key, {
        "filters": {"user_id": user}, "fields": ["id", "user_id"], "show_expired": True,
    })
    if status in (401, 403):
        raise ExternalRequestError("credentials")
    if status != 200 or not isinstance(result, dict):
        raise ExternalRequestError("network")
    results = result.get("results")
    ids = copy_ids(results)
    if (result.get("count") != len(ids) or "next" not in result or result["next"] is not None
            or any(item.get("user_id") != user for item in results)):
        raise ExternalRequestError("invalid_response")
    return ids


def not_found(status, body):
    return status == 404 and isinstance(body, dict) and body.get("error") == "Memory not found!"


def remove_copy(transport, key, identity):
    path = f"/v1/memories/{identity}/"
    status, result = call(transport, "DELETE", path, key)
    acknowledged = status == 200 and isinstance(result, dict) and result.get("message") == "Memory deleted successfully!"
    if not acknowledged and not not_found(status, result):
        raise ExternalRequestError("delete_unconfirmed")
    if not not_found(*call(transport, "GET", path, key)):
        raise ExternalRequestError("delete_unconfirmed")
