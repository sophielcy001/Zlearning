from hmac import compare_digest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import ROOT


def local_app(session_token: str, *, lifespan=None) -> FastAPI:
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    authorization = f"Bearer {session_token}".encode("utf-8")

    @app.middleware("http")
    async def local_session(request: Request, call_next):
        host = request.headers.get("host", "")
        if host.partition(":")[0] != "127.0.0.1":
            return JSONResponse({"detail": "请从本机启动入口打开智学。"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if request.headers.get("origin") not in (None, f"http://{host}"):
                return JSONResponse({"detail": "请求来源不可用。"}, status_code=403)
            token = request.headers.get("authorization", "").encode("utf-8")
            if not compare_digest(token, authorization):
                return JSONResponse({"detail": "本机会话已过期，请从启动入口重新打开。"}, status_code=401)
        response = await call_next(request)
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "microphone=(self), camera=()"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def health():
        return {"app": "zhixue-demo", "status": "ready"}

    return app


def mount_frontend(app: FastAPI) -> None:
    if (ROOT / "dist" / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ROOT / "dist", html=True), name="frontend")
