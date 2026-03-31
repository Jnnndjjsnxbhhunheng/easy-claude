"""
无 Node.js 依赖的 Python 前端。

运行方式：
    cd python_frontend
    python app.py --host 127.0.0.1 --port 3001 --backend-url http://127.0.0.1:8015
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
INDEX_PATH = APP_DIR / "templates" / "index.html"


def create_app(*, backend_url: str | None = None) -> FastAPI:
    app = FastAPI(title="Easy Claude Python Frontend")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    configured_backend = (backend_url or os.environ.get("PY_FRONTEND_BACKEND_URL", "")).strip()

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        html = INDEX_PATH.read_text(encoding="utf-8")
        config_script = (
            "<script>"
            f"window.EASY_CLAUDE_CONFIG = {json.dumps({'backendUrl': configured_backend}, ensure_ascii=False)};"
            "</script>"
        )
        html = html.replace("<!--APP_CONFIG-->", config_script)
        return HTMLResponse(html)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Easy Claude Python Frontend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=3001, type=int)
    parser.add_argument("--backend-url", default=os.environ.get("PY_FRONTEND_BACKEND_URL", ""))
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        create_app(backend_url=args.backend_url),
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
