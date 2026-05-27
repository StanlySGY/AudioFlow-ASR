from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as asr_router
from app.config import get_settings
from app.services.stream_manager import TaskManager


WEB_DIR = Path(__file__).parent / "web"


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    app = FastAPI(
        title="AudioFlow-ASR",
        description="Long-audio splitting + streaming ASR transcription service.",
        version="0.1.0",
    )
    app.state.manager = TaskManager(settings)
    app.include_router(asr_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, log_level=s.log_level)
