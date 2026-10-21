from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ontoagents.config import get_settings
from ontoagents.domain import Answer
from ontoagents.graph import Pipeline
from ontoagents.wiring import pipeline


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


def create_app(injected: Pipeline | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if injected is not None:
            app.state.pipeline = injected
            yield
            return
        async with pipeline(get_settings()) as p:
            app.state.pipeline = p
            yield

    app = FastAPI(title="ontoagents", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/ask", response_model=Answer)
    async def ask(body: AskRequest, request: Request) -> Answer:
        p: Pipeline = request.app.state.pipeline
        return await p.ask(body.question)

    @app.post("/v1/ask/stream")
    async def ask_stream(body: AskRequest, request: Request) -> StreamingResponse:
        p: Pipeline = request.app.state.pipeline

        async def events() -> AsyncIterator[str]:
            async for event in p.stream(body.question):
                yield f"event: {event['node']}\ndata: {json.dumps(event['update'])}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return app
