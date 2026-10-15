"""ASGI entrypoint for production (FastAPI + uvicorn).

NOT EXECUTED IN THE BUILD SANDBOX (PyPI was unreachable there). It is a thin adapter over the
tested `App.handle()`; run tests/test_api.py plus a smoke test after `pip install -r requirements.txt`.

    uvicorn railvision.api.fastapi_app:asgi --host 0.0.0.0 --port 8000
Run the scheduler as a separate process:  python -m railvision.worker
"""
from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, Request as FRequest
from fastapi.responses import Response, StreamingResponse

from .app import App, Request

core = App()
asgi = FastAPI(title="RAILVISION AI", version="2.0.0", docs_url="/api/docs", openapi_url="/api/openapi.json")


@asgi.get("/api/events")
async def events(request: FRequest):
    req = Request("GET", "/api/events", dict(request.query_params), {k.lower(): v for k, v in request.headers.items()})
    uid = core._auth(req)
    q = core.bus.subscribe(uid)

    async def gen():
        try:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.to_thread(q.get, True, 20)
                    yield f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except Exception:
                    yield ": keepalive\n\n"
        finally:
            core.bus.unsubscribe(uid, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@asgi.api_route("/{path:path}", methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"])
async def catch_all(path: str, request: FRequest):
    body = await request.body()
    req = Request(request.method, "/" + path, {k: request.query_params.getlist(k) for k in request.query_params},
                  {k.lower(): v for k, v in request.headers.items()}, body,
                  request.client.host if request.client else "?")
    st, hd, out = await asyncio.to_thread(core.handle, req)
    return Response(content=out, status_code=st, headers=hd)
