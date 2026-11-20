"""A single-process rendezvous for opaque, live HTTP transfers."""
import argparse
import asyncio
from dataclasses import dataclass, field
from hmac import compare_digest
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import ClientDisconnect

from .mobile_cipher import MAX_ANSWER, MAX_REQUEST, OVERHEAD, TOKEN, origin


WAIT_SECONDS = 3
EXCHANGE_SECONDS = 9
MAX_ROOMS = 32
OCTETS = "application/octet-stream"


@dataclass
class Rendezvous:
    ticket: str
    reply_token: str = field(repr=False)
    packet: asyncio.Future = field(repr=False)
    answer: asyncio.Future = field(repr=False)
    receiving: bool = False


async def live_value(request: Request, future: asyncio.Future, timeout: float) -> bytes:
    deadline = monotonic() + timeout
    while True:
        if await request.is_disconnected():
            raise HTTPException(499)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise HTTPException(408)
        done, _ = await asyncio.wait((future,), timeout=min(remaining, 0.15))
        if done:
            value = future.result()
            if value is None:
                raise HTTPException(404)
            return value


async def opaque_body(request: Request, maximum: int) -> bytes:
    data = bytearray()
    try:
        async with asyncio.timeout(4):
            async for chunk in request.stream():
                if len(data) + len(chunk) > maximum:
                    raise HTTPException(413)
                data.extend(chunk)
    except TimeoutError:
        raise HTTPException(408) from None
    except ClientDisconnect:
        raise HTTPException(499) from None
    if len(data) < OVERHEAD:
        raise HTTPException(400)
    return bytes(data)


def create_relay(mobile_origin: str) -> FastAPI:
    allowed = origin(mobile_origin)
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    slots: dict[str, Rendezvous] = {}
    app.state.rendezvous = slots

    @app.middleware("http")
    async def ephemeral_response(request: Request, call_next):
        if request.headers.get("origin") not in (None, allowed):
            return Response(status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.add_middleware(
        CORSMiddleware, allow_origins=[allowed], allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"], max_age=300,
    )

    @app.get("/healthz")
    async def health():
        return {"status": "ready"}

    @app.get("/v1/{room}/offer")
    async def offer(room: str):
        slot = slots.get(room)
        if slot is None or slot.packet.done() or slot.receiving:
            raise HTTPException(404)
        return {"ticket": slot.ticket}

    @app.post("/v1/{room}/wait/{ticket}")
    async def wait(room: str, ticket: str, request: Request):
        reply_token = request.headers.get("x-reply-token", "")
        if not all(TOKEN.fullmatch(value) for value in (room, ticket, reply_token)):
            raise HTTPException(404)
        if room in slots:
            raise HTTPException(409)
        if len(slots) >= MAX_ROOMS:
            raise HTTPException(503)
        loop = asyncio.get_running_loop()
        slot = slots[room] = Rendezvous(ticket, reply_token, loop.create_future(), loop.create_future())
        dispatched = False
        try:
            packet = await live_value(request, slot.packet, WAIT_SECONDS)
            dispatched = True
            return Response(packet, media_type=OCTETS)
        finally:
            if not dispatched and slots.get(room) is slot:
                slots.pop(room)
                if not slot.answer.done():
                    slot.answer.set_result(None)

    @app.post("/v1/{room}/exchange/{ticket}")
    async def exchange(room: str, ticket: str, request: Request):
        slot = slots.get(room)
        if slot is None or slot.ticket != ticket or slot.packet.done() or slot.receiving:
            raise HTTPException(404)
        slot.receiving = True
        try:
            packet = await opaque_body(request, MAX_REQUEST)
            if slots.get(room) is not slot:
                raise HTTPException(404)
            slot.packet.set_result(packet)
            answer = await live_value(request, slot.answer, EXCHANGE_SECONDS)
            return Response(answer, media_type=OCTETS)
        finally:
            slot.receiving = False
            if slot.packet.done() and slots.get(room) is slot:
                slots.pop(room)

    @app.post("/v1/{room}/answer/{ticket}")
    async def answer(room: str, ticket: str, request: Request):
        slot = slots.get(room)
        supplied = request.headers.get("x-reply-token", "")
        if (
            slot is None or slot.ticket != ticket or not slot.packet.done() or slot.answer.done()
            or not TOKEN.fullmatch(supplied) or not compare_digest(supplied, slot.reply_token)
        ):
            raise HTTPException(404)
        packet = await opaque_body(request, MAX_ANSWER)
        if slots.get(room) is not slot or slot.answer.done():
            raise HTTPException(404)
        slot.answer.set_result(packet)
        return Response(status_code=204)

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="启动智学实时加密中继")
    parser.add_argument("--origin", required=True, help="手机 PWA 的独立可信来源")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=3221, type=int)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args()
    if bool(args.tls_cert) != bool(args.tls_key) or (args.host != "127.0.0.1" and not args.tls_cert):
        parser.error("公开监听需要同时设置 TLS 证书和私钥。回环监听可置于 TLS 反向代理之后。")
    try:
        app = create_relay(args.origin)
    except ValueError as error:
        parser.error(str(error))
    uvicorn.run(
        app, host=args.host, port=args.port, access_log=False, proxy_headers=False,
        loop="asyncio", http="h11", ws="none", limit_concurrency=160,
        timeout_graceful_shutdown=3,
        ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key,
    )


if __name__ == "__main__":
    main()
