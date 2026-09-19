"""El Turno — the socket the clinic's calls arrive on, and the console over it."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.agent.llm import LLMClient
from src.config import settings
from src.domain.catalog import Catalog
from src.obs import api as console_api
from src.obs.store import store
from src.platform_api.client import PlatformClient
from src.telephony import twilio_ws

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("elturno")

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


async def load_catalog() -> Catalog:
    """From disk if it is cached, otherwise straight from the clinic."""
    path = Path(settings.catalog_path)
    if path.exists():
        logger.info("catalogue loaded from %s", path.name)
        return Catalog.load(path)

    logger.info("no cached catalogue; fetching it from the platform")
    client = PlatformClient()
    try:
        raw = await client.clinic()
    finally:
        await client.aclose()
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(json.dumps({"clinic": raw}, indent=2, ensure_ascii=False), encoding="utf-8")
    return Catalog({"clinic": raw})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    catalog = await load_catalog()
    llm = LLMClient()

    twilio_ws.configure(catalog, llm)
    console_api.configure(catalog, llm)
    app.state.catalog = catalog
    app.state.llm = llm

    missing = settings.missing_voice_keys()
    logger.info(
        "ready on :%s%s — stt=%s llm=%s tts=%s",
        settings.port, "/ws",
        settings.stt_provider if settings.deepgram_api_key else "whisper-fallback",
        settings.llm_model if settings.llm_api_key else "NONE",
        settings.tts_provider,
    )
    if missing:
        logger.warning("voice pipeline incomplete, missing: %s", ", ".join(missing))

    try:
        yield
    finally:
        await llm.aclose()


app = FastAPI(title="El Turno · Clínica Arenal", version="1.0.0", lifespan=lifespan)
app.include_router(twilio_ws.router)
app.include_router(console_api.router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def control() -> FileResponse:
    """Every call and every agent at once."""
    return FileResponse(STATIC_DIR / "ops.html")


@app.get("/console", include_in_schema=False)
async def console() -> FileResponse:
    """The original one-call-at-a-time console, kept for the deep read of a
    single call. The control dashboard links to it rather than replacing it."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "endpoint": "/ws",
        "public_ws_url": settings.public_ws_url or None,
        "public_console_url": settings.public_console_url or None,
        "platform_configured": settings.configured,
        "voice_ready": not settings.missing_voice_keys(),
        "missing_keys": settings.missing_voice_keys(),
        "live_calls": len(store.live_calls()),
    }


@app.get("/api/platform/check")
async def platform_check() -> JSONResponse:
    """Confirms the key and the host, without making a call."""
    client = PlatformClient()
    try:
        await client.health()
        matches = await client.directory(name="Marta Ruiz")
        return JSONResponse({
            "platform_reachable": True,
            "authenticated": True,
            "directory_sample": len(matches),
        })
    except Exception as exc:
        return JSONResponse({"platform_reachable": False, "detail": str(exc)}, status_code=502)
    finally:
        await client.aclose()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )
