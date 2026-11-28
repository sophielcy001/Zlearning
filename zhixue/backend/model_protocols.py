from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from http.client import HTTPConnection, HTTPException as HTTPClientError, HTTPSConnection
from ipaddress import ip_address
import json
import re
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from .model_costs import ModelUsage, provider_amount


CHAT = "chat-completions"
MESSAGES = "anthropic-messages"
GEMINI = "gemini-generate-content"
DEFAULT_PROVIDER = "minimax"
PROVIDERS = {
    "minimax": ("MiniMax", [CHAT, MESSAGES]),
    "openai": ("OpenAI", [CHAT]),
    "anthropic": ("Anthropic（Claude）", [MESSAGES, CHAT]),
    "gemini": ("Gemini", [GEMINI, CHAT]),
    "deepseek": ("DeepSeek", [CHAT]),
    "qwen": ("通义千问", [CHAT]),
    "zhipu": ("智谱", [CHAT]),
    "kimi": ("Kimi", [CHAT]),
    "doubao": ("豆包", [CHAT]),
    "custom": ("自定义", [CHAT, MESSAGES, GEMINI]),
}
PROMPT = "Reply with OK."
OUTPUT_LIMIT = 512
MODEL_HTTP_TIMEOUT = 20
RESPONSE_LIMIT = 256 * 1024
COMPLETION_LIMIT_PROVIDERS = {"minimax", "openai", "kimi", "doubao"}
Transport = Callable[[str, dict[str, str], dict], tuple[int, bytes]]
OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"


class TransportNotSent(Exception):
    """The transport declined the request before attempting network I/O."""


def normalize_endpoint(value: str) -> str:
    if not value.isascii() or any(not char.isprintable() or char.isspace() for char in value):
        raise ValueError("地址必须是有效的 URL。")
    if any(char in value for char in "?#\\"):
        raise ValueError("地址不能包含查询参数、片段或反斜杠。")
    parts = urlsplit(value)
    host, port = parts.hostname, parts.port
    if parts.username is not None or parts.password is not None or not host or port == 0:
        raise ValueError("地址必须包含有效的主机且不能内嵌认证信息。")
    try:
        ip = ip_address(host)
    except ValueError:
        ip = None
        if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.?", host):
            raise ValueError("地址的主机名无效。") from None
    is_loopback = host == "localhost" or (ip is not None and ip.is_loopback)
    if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback):
        raise ValueError("服务地址需要使用 HTTPS，本机回环地址可以使用 HTTP。")
    authority = f"[{host}]" if ":" in host else host
    default_port = 443 if parts.scheme == "https" else 80
    if port is not None and port != default_port:
        authority += f":{port}"
    return urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))


def test_address(endpoint: str, protocol: str, model: str) -> str:
    path = urlsplit(endpoint).path
    if protocol == CHAT:
        return endpoint if path.endswith("/chat/completions") else f"{endpoint}/chat/completions"
    if protocol == MESSAGES:
        if path.endswith("/messages"):
            return endpoint
        return f"{endpoint}/messages" if path.endswith("/v1") else f"{endpoint}/v1/messages"

    identity = model.removeprefix("models/")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", identity):
        raise ValueError("Gemini 模型名称应填写模型 ID。")
    suffix = f"/models/{identity}:generateContent"
    if path.endswith(":generateContent"):
        if path.endswith(suffix):
            return endpoint
        raise ValueError("Gemini 地址中的模型名称与所填模型不一致。")
    return f"{endpoint}{'' if path else '/v1beta'}{suffix}"


def post_json(address: str, headers: dict[str, str], payload: dict) -> tuple[int, bytes]:
    return post_bytes(address, {"Content-Type": "application/json", **headers}, json.dumps(payload).encode("utf-8"))


def post_bytes(address: str, headers: dict[str, str], body: bytes, *, timeout: int = MODEL_HTTP_TIMEOUT,
               upload_guard: Callable[[], AbstractContextManager[None]] | None = None) -> tuple[int, bytes]:
    parts = urlsplit(address)
    connection_type = HTTPSConnection if parts.scheme == "https" else HTTPConnection
    connection = connection_type(parts.hostname, parts.port, timeout=timeout)
    try:
        if upload_guard is not None:
            connection.connect()
        with upload_guard() if upload_guard is not None else nullcontext():
            connection.request("POST", parts.path, body, {"Accept": "application/json", **headers})
        with connection.getresponse() as reply:
            # http.client sends one request without using proxy variables or following redirects.
            if 300 <= reply.status < 400:
                return reply.status, b""
            return reply.status, reply.read(RESPONSE_LIMIT + 1)
    finally:
        connection.close()


@dataclass(frozen=True)
class ProbeResult:
    status: str
    usage: ModelUsage | None = None


@dataclass(frozen=True)
class TextReply:
    status: str
    usage: ModelUsage | None = None
    text: str = ""
    truncated: bool = False


def probe_model(
    provider: str, protocol: str, address: str, model: str, key: str | None, transport: Transport,
) -> ProbeResult:
    reply = call_text(provider, protocol, address, model, key, PROMPT, OUTPUT_LIMIT, transport)
    return ProbeResult(reply.status, reply.usage)


def call_text(
    provider: str, protocol: str, address: str, model: str, key: str | None, prompt: str,
    max_output: int, transport: Transport = post_json,
) -> TextReply:
    messages = [{"role": "user", "content": prompt}]
    if protocol == CHAT:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        limit_field = "max_completion_tokens" if provider in COMPLETION_LIMIT_PROVIDERS else "max_tokens"
        payload = {"model": model, "messages": messages, limit_field: max_output, "stream": False}
    elif protocol == MESSAGES:
        headers = {"anthropic-version": "2023-06-01"}
        if key:
            if provider == "minimax":
                headers["Authorization"] = f"Bearer {key}"
            else:
                headers["x-api-key"] = key
        payload = {"model": model, "messages": messages, "max_tokens": max_output}
    else:
        headers = {"x-goog-api-key": key} if key else {}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output},
        }

    try:
        status, body = transport(address, headers, payload)
    except TimeoutError:
        return TextReply("timeout")
    except (OSError, HTTPClientError):
        return TextReply("network")

    data = response_object(body)
    usage = reply_usage(protocol, data, address) if data is not None else None
    error = http_failure(status, data)
    if error:
        return TextReply(error, usage)
    if data is None:
        return TextReply("invalid_response")
    return parse_text_reply(protocol, data, usage)


def response_object(body: bytes) -> dict | None:
    if len(body) > RESPONSE_LIMIT:
        return None
    try:
        result = json.loads(body, parse_float=Decimal)
    except (ValueError, RecursionError, InvalidOperation):
        return None
    return result if isinstance(result, dict) else None


def fields(value) -> dict:
    return value if isinstance(value, dict) else {}


def records(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def http_failure(status: int, data: dict | None) -> str | None:
    if 300 <= status < 400:
        return "redirect"
    if status in (401, 403):
        return "unauthorized"
    if status == 402:
        return "quota"
    if status == 429:
        code = fields(fields(data).get("error")).get("code")
        return "quota" if code == "insufficient_quota" else "rate_limited"
    if status >= 500:
        return "service_unavailable"
    if status >= 400:
        return "request_rejected"
    if not 200 <= status < 300:
        return "invalid_response"

    # MiniMax may report failures in a successful HTTP response.
    provider_code = fields(fields(data).get("base_resp")).get("status_code")
    if type(provider_code) is int and provider_code:
        return {1002: "rate_limited", 1004: "unauthorized", 1008: "quota"}.get(provider_code, "request_rejected")
    if fields(data).get("error"):
        return "request_rejected"
    return None


def visible_text(value) -> str:
    return re.sub(r"<think>.*?(?:</think>|$)", "", value, flags=re.DOTALL).strip() if isinstance(value, str) else ""


def token_usage(value, names: tuple[tuple[str, str], ...]) -> ModelUsage:
    counters = fields(value)
    usage: ModelUsage = {}
    for label, key in names:
        count = counters.get(key)
        if type(count) is int and 0 <= count < 10**12:
            usage[label] = count
    return usage


def reply_usage(protocol: str, data: dict, address: str) -> ModelUsage | None:
    if protocol == CHAT:
        raw = fields(data.get("usage"))
        usage = token_usage(raw, (("inputTokens", "prompt_tokens"), ("outputTokens", "completion_tokens"),
                                  ("totalTokens", "total_tokens")))
        prompt_details = fields(raw.get("prompt_tokens_details"))
        completion_details = fields(raw.get("completion_tokens_details"))
        byok = raw.get("is_byok")
        special = (byok is True or (address == OPENROUTER_CHAT and byok is not False)
                   or any(prompt_details.get(key) for key in ("cached_tokens", "cache_write_tokens", "audio_tokens", "video_tokens"))
                   or any(completion_details.get(key) for key in ("audio_tokens", "image_tokens"))
                   or bool(raw.get("prompt_cache_hit_tokens"))
                   or any(fields(raw.get("server_tool_use_details")).values()))
        if address == OPENROUTER_CHAT and byok is False:
            amount = provider_amount(raw.get("cost"))
            if amount is not None:
                usage["actualCurrency"], usage["actualAmount"] = "USD", amount
    elif protocol == MESSAGES:
        raw = fields(data.get("usage"))
        usage = token_usage(raw, (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                                  ("totalTokens", "total_tokens")))
        special = any(raw.get(key) for key in ("cache_creation_input_tokens", "cache_read_input_tokens", "server_tool_use"))
    else:
        raw = fields(data.get("usageMetadata"))
        usage = token_usage(raw, (("inputTokens", "promptTokenCount"), ("outputTokens", "candidatesTokenCount"),
                                  ("totalTokens", "totalTokenCount"), ("thinkingTokens", "thoughtsTokenCount")))
        special = any(raw.get(key) for key in ("cachedContentTokenCount", "toolUsePromptTokenCount"))
        special = special or ("thoughtsTokenCount" in raw and "thinkingTokens" not in usage)
    if special:
        usage["specialPricing"] = True
    return usage or None


def parse_text_reply(protocol: str, data: dict, usage: ModelUsage | None) -> TextReply:
    if protocol == CHAT:
        if not isinstance(data.get("choices"), list):
            return TextReply("invalid_response", usage)
        for choice in records(data["choices"]):
            if text := visible_text(fields(choice.get("message")).get("content")):
                return TextReply("passed", usage, text, truncated=choice.get("finish_reason") == "length")
        return TextReply("no_text", usage)
    elif protocol == MESSAGES:
        if not isinstance(data.get("content"), list):
            return TextReply("invalid_response", usage)
        options = ["\n".join(filter(None, (
            visible_text(part.get("text")) for part in records(data["content"]) if part.get("type") == "text"
        )))]
    else:
        if not isinstance(data.get("candidates"), list) and not isinstance(data.get("promptFeedback"), dict):
            return TextReply("invalid_response", usage)
        options = [
            "\n".join(filter(None, (
                visible_text(part.get("text"))
                for part in records(fields(candidate.get("content")).get("parts")) if not part.get("thought")
            )))
            for candidate in records(data.get("candidates"))
        ]
    text = next((option for option in options if option), "")
    return TextReply("passed" if text else "no_text", usage, text)
