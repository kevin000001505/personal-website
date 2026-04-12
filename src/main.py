import httpx

import logfire
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any


from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from agent_design import create_agent
from pydantic_ai import ModelMessagesTypeAdapter

logfire.configure(service_name="kevin-personal-website")
logfire.instrument_pydantic_ai()
logfire.instrument_httpx(capture_all=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting up...")
    app.state.agent = create_agent()
    yield
    print("Shutting down...")


app = FastAPI(title="Kevin Personal Website API", version="1.0.0", lifespan=lifespan)
logfire.instrument_fastapi(app)

CONTENT_TYPE_JSON = "application/json"


DEFAULT_AI_RESPONSE_TEXT = os.getenv(
    "DEFAULT_AI_RESPONSE_TEXT", "Hello! How can I help you today?"
)
FALLBACK_RESPONSE_TEXT = os.getenv(
    "FALLBACK_RESPONSE_TEXT",
    "Sorry, Kevin is busy fixing me right now! 🔧 But you can still look around the page to learn more about him. Check out his projects, skills, and homelab sections — there's a lot of cool stuff!",
)


def _resolve_ntfy_endpoint(raw_topic: str | None) -> str | None:
    if not raw_topic:
        return None
    topic = raw_topic.strip()
    if not topic:
        return None
    if topic.startswith(("http://", "https://")):
        return topic
    return f"http://{topic.lstrip('/')}"


NTFY_ENDPOINT = _resolve_ntfy_endpoint(os.getenv("NTFY_TOPIC"))

logger = logging.getLogger("fastapi_app")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logger.propagate = False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: str | None, max_len: int = 500) -> str | None:
    if value is None:
        return None
    if len(value) <= max_len:
        return value
    return value[:max_len] + "..."


def _get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return "unknown"


def _extract_user_prompt(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None

    for key in ("message", "prompt", "question", "input"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    messages = payload.get("messages")
    if isinstance(messages, list):
        for item in reversed(messages):
            if not isinstance(item, dict):
                continue
            if item.get("role") != "user":
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                text_parts: list[str] = []
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
                if text_parts:
                    return " ".join(text_parts).strip()
    return None


def _log_event(event: str, **fields: Any) -> None:
    record = {"event": event, "ts": _utc_now_iso(), **fields}
    logger.info(json.dumps(record, ensure_ascii=True, default=str))


def _stream_ai_response(
    *,
    prompt: str,
    history_json: bytes | None,
    event_name: str,
    request_id: str | None,
    client_ip: str,
    user_prompt_preview: str | None,
    upstream_status: int,
) -> StreamingResponse:
    started = time.perf_counter()

    async def generator():
        stream_error: str | None = None
        fallback_reason: str | None = None
        emitted_parts: list[str] = []
        new_messages_json = None

        try:
            history = (
                ModelMessagesTypeAdapter.validate_json(history_json)
                if history_json
                else []
            )

            async with app.state.agent.run_stream(
                prompt, message_history=history
            ) as response:
                async for text in response.stream_output(debounce_by=0.01):
                    delta = text[len("".join(emitted_parts)) :]
                    if delta:
                        emitted_parts.append(delta)
                        yield delta

                new_messages_json = response.new_messages_json()

        except Exception as exc:
            stream_error = str(exc)
            fallback_reason = "local_agent_error"
            emitted_parts.append(FALLBACK_RESPONSE_TEXT)
            yield FALLBACK_RESPONSE_TEXT
        finally:
            # 👇 Send history trailer BEFORE logging so client gets it
            if new_messages_json:
                yield b"\n__HISTORY__" + new_messages_json

            duration_ms = int((time.perf_counter() - started) * 1000)
            full_text = "".join(emitted_parts).strip()
            _log_event(
                event_name,
                request_id=request_id,
                client_ip=client_ip,
                upstream_status=upstream_status,
                duration_ms=duration_ms,
                user_prompt_preview=user_prompt_preview,
                ai_response_preview=_truncate(full_text, 300),
                fallback_reason=fallback_reason,
                stream_error=stream_error,
            )

    return StreamingResponse(generator(), media_type="text/plain; charset=utf-8")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    start = time.perf_counter()
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    client_ip = _get_client_ip(request)
    request.state.request_id = request_id
    request.state.client_ip = client_ip

    body_payload: dict[str, Any] | None = None
    content_type = request.headers.get("content-type", "")
    if request.method in {"POST", "PUT", "PATCH"} and CONTENT_TYPE_JSON in content_type:
        raw_body = await request.body()
        if raw_body:
            try:
                body_payload = json.loads(raw_body)
            except json.JSONDecodeError:
                body_payload = None

    if request.url.path != "/health":
        _log_event(
            "http_request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client_ip=client_ip,
            user_prompt_preview=_truncate(_extract_user_prompt(body_payload), 300),
        )

    response = None
    try:
        response = await call_next(request)
        return response
    except Exception as exc:
        _log_event(
            "http_error",
            request_id=request_id,
            path=request.url.path,
            client_ip=client_ip,
            error=str(exc),
        )
        raise
    finally:
        if request.url.path != "/health":
            duration_ms = int((time.perf_counter() - start) * 1000)
            _log_event(
                "http_response",
                request_id=request_id,
                path=request.url.path,
                client_ip=client_ip,
                status_code=response.status_code if response is not None else 500,
                duration_ms=duration_ms,
            )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {"message": "Personal Website API"}


@app.post("/ai/proxy/stream")
async def ai_proxy_stream(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="Request body must be valid JSON"
        ) from exc

    request_id = getattr(request.state, "request_id", None)
    client_ip = getattr(request.state, "client_ip", _get_client_ip(request))

    full_prompt = _extract_user_prompt(payload)
    user_prompt_preview = _truncate(full_prompt, 300)

    if not full_prompt:
        raise HTTPException(
            status_code=400, detail="No user prompt found in request body"
        )

    # Accept serialized history from client (pydantic-ai format)
    history_json: bytes | None = None
    raw_history = payload.get("history")
    if isinstance(raw_history, (list, dict)):
        try:
            # Validate it's actually pydantic-ai format before using it

            history_json = json.dumps(raw_history).encode()
            ModelMessagesTypeAdapter.validate_json(history_json)  # test parse
        except Exception:
            history_json = None

    return _stream_ai_response(
        prompt=full_prompt,
        history_json=history_json,
        event_name="ai_proxy_stream_ai",
        request_id=request_id,
        client_ip=client_ip,
        user_prompt_preview=user_prompt_preview,
        upstream_status=200,
    )


# stays server-side only


@app.post("/api/ping")
async def visitor_ping(request: Request):
    ip = getattr(request.state, "client_ip", _get_client_ip(request))
    ref = request.headers.get("referer", "direct")

    if not NTFY_ENDPOINT:
        _log_event(
            "visitor_ping_notify_skipped",
            reason="ntfy_not_configured",
            client_ip=ip,
            ref=_truncate(ref, 200),
        )
        return {"ok": True}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                NTFY_ENDPOINT,
                content=f"IP: {ip}\nRef: {ref}",
                # HTTP headers must be ASCII; keep title plain text.
                headers={"Title": "Visitor", "Priority": "min"},
            )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        _log_event(
            "visitor_ping_notify_failed",
            client_ip=ip,
            ref=_truncate(ref, 200),
            endpoint=_truncate(NTFY_ENDPOINT, 200),
            error=str(exc),
        )
    return {"ok": True}
