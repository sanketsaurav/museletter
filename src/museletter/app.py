import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .db import ensure_default_list, ensure_secret, open_db, utcnow
from .sender import SenderLoop
from .ses import SESClient
from .sns import SNSVerifier


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    db = await open_db(settings.db_path)
    app.state.db = db
    app.state.secret = await ensure_secret(db, settings.secret)
    await ensure_default_list(db)
    app.state.ses = settings.extra.get("ses") or SESClient(
        settings.aws_region, settings.ses_configuration_set
    )
    app.state.sns_verifier = settings.extra.get("sns_verifier") or SNSVerifier()
    app.state.sender = SenderLoop(app)
    sender_task = None
    if not settings.extra.get("disable_sender"):
        sender_task = asyncio.create_task(app.state.sender.run())
    yield
    if sender_task is not None:
        app.state.sender.stop()
        try:
            await asyncio.wait_for(sender_task, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            sender_task.cancel()
    await db.close()


async def idempotency_middleware(request: Request, call_next):
    key = request.headers.get("Idempotency-Key", "")
    if (
        not key
        or request.method not in ("POST", "PATCH", "DELETE")
        or not request.url.path.startswith("/v1/")
    ):
        return await call_next(request)

    db = request.app.state.db
    scope = f"{request.method} {request.url.path}"
    async with db.execute(
        "SELECT status_code, response_body FROM idempotency WHERE key = ? AND path = ?", (key, scope)
    ) as cur:
        row = await cur.fetchone()
    if row:
        return Response(
            content=row["response_body"],
            status_code=row["status_code"],
            media_type="application/json",
            headers={"Idempotency-Replayed": "true"},
        )

    response = await call_next(request)
    body = b"".join([chunk async for chunk in response.body_iterator])
    if response.status_code < 500:
        await db.execute(
            "INSERT OR IGNORE INTO idempotency (key, path, status_code, response_body, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, scope, response.status_code, body.decode("utf-8", "replace"), utcnow()),
        )
        await db.commit()
    return Response(
        content=body,
        status_code=response.status_code,
        media_type=response.media_type,
        headers=dict(response.headers),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="museletter", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.middleware("http")(idempotency_middleware)

    from .api.admin import router as admin_router
    from .api.public import router as public_router

    app.include_router(admin_router)
    app.include_router(public_router)

    @app.get("/health")
    async def health():
        return {"ok": True, "name": "museletter", "version": __version__}

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"detail": f"internal error: {type(exc).__name__}"})

    return app
