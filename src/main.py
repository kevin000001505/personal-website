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

    if NTFY_ENDPOINT:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    NTFY_ENDPOINT,
                    content=f"IP: {ip}\nRef: {ref}",
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


# ── Visitor Analytics ──

ANALYTICS_FILE = os.getenv("ANALYTICS_FILE", "/data/analytics.jsonl")
NTFY_ANALYTICS_URL = NTFY_ENDPOINT


def _append_analytics_record(record: dict) -> None:
    try:
        dir_path = os.path.dirname(ANALYTICS_FILE)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(ANALYTICS_FILE, "a") as f:
            f.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
    except Exception as exc:
        _log_event(
            "analytics_write_failed",
            error=str(exc),
        )


def _notify_analytics(
    page: str, duration_s: int, max_scroll_pct: int, client_ip: str = ""
) -> None:
    if not NTFY_ANALYTICS_URL:
        return
    try:

        async def _post():
            async with httpx.AsyncClient(timeout=3) as client:
                await client.post(
                    NTFY_ANALYTICS_URL,
                    content=(
                        f"Visitor IP: {client_ip} | {duration_s}s | Scrolled {max_scroll_pct}%"
                    ),
                    headers={"Title": "Portfolio Visit", "Priority": "min"},
                )

        import asyncio

        try:
            asyncio.get_running_loop()
            asyncio.create_task(_post())
        except RuntimeError:
            asyncio.run(_post())
    except Exception:
        pass


@app.post("/api/analytics")
async def track_analytics(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {"ok": False}

    page = body.get("page", "/")
    duration_s = body.get("duration_s", 0)
    max_scroll_pct = body.get("max_scroll_pct", 0)
    milestones = body.get("milestones", [])
    referrer = body.get("referrer", "")
    user_agent = body.get("user_agent", "")
    session_id = body.get("session_id", "")
    section_dwells = body.get("section_dwells", {})
    chat_opened = body.get("chat_opened", False)
    chat_messages = body.get("chat_messages", 0)

    record = {
        "page": page,
        "duration_s": duration_s,
        "max_scroll_pct": max_scroll_pct,
        "milestones": milestones,
        "referrer": referrer,
        "user_agent": user_agent,
        "session_id": session_id,
        "section_dwells": section_dwells,
        "chat_opened": chat_opened,
        "chat_messages": chat_messages,
        "timestamp": _utc_now_iso(),
    }

    ip = _get_client_ip(request)
    _append_analytics_record(record)
    _notify_analytics(page, duration_s, max_scroll_pct, ip)

    _log_event(
        "analytics_session_end",
        session_id=session_id,
        client_ip=ip,
        page=page,
        duration_s=duration_s,
        max_scroll_pct=max_scroll_pct,
        milestones=milestones,
        section_dwells=section_dwells,
        chat_opened=chat_opened,
        chat_messages=chat_messages,
        referrer=_truncate(referrer, 200),
    )

    return {"ok": True}


@app.post("/api/analytics/event")
async def track_analytics_event(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {"ok": False}

    event_type = body.get("event_type", "unknown")
    session_id = body.get("session_id", "")
    client_ip = _get_client_ip(request)

    base = {"session_id": session_id, "client_ip": client_ip, "event_type": event_type}

    if event_type == "section_dwell":
        _log_event(
            "analytics_section_dwell",
            **base,
            section_id=body.get("section_id", ""),
            dwell_ms=body.get("dwell_ms", 0),
        )

    elif event_type == "link_click":
        _log_event(
            "analytics_link_click",
            **base,
            href=_truncate(body.get("href", ""), 200),
            link_text=_truncate(body.get("link_text", ""), 60),
            section_id=body.get("section_id", ""),
        )

    elif event_type == "text_copy":
        _log_event(
            "analytics_text_copy",
            **base,
            selection=_truncate(body.get("selection", ""), 100),
        )

    elif event_type == "chat_open":
        _log_event(
            "analytics_chat_open",
            **base,
            time_on_page_s=body.get("time_on_page_s", 0),
            scroll_pct=body.get("scroll_pct", 0),
        )

    elif event_type == "chat_message":
        _log_event(
            "analytics_chat_message",
            **base,
            message_number=body.get("message_number", 0),
            time_on_page_s=body.get("time_on_page_s", 0),
        )

    elif event_type == "heartbeat":
        _log_event(
            "analytics_heartbeat",
            **base,
            time_on_page_s=body.get("time_on_page_s", 0),
            scroll_pct=body.get("scroll_pct", 0),
            milestones=body.get("milestones", []),
            sections_active=body.get("sections_active", []),
            chat_opened=body.get("chat_opened", False),
            chat_messages=body.get("chat_messages", 0),
        )

    elif event_type == "project_click":
        _log_event(
            "analytics_project_click",
            **base,
            project_name=_truncate(body.get("project_name", ""), 60),
            href=_truncate(body.get("href", ""), 200),
            section_id=body.get("section_id", ""),
        )

    elif event_type == "project_view":
        _log_event(
            "analytics_project_view",
            **base,
            project_name=_truncate(body.get("project_name", ""), 60),
            dwell_ms=body.get("dwell_ms", 0),
        )

    elif event_type == "ask_chip_click":
        _log_event(
            "analytics_ask_chip_click",
            **base,
            question=_truncate(body.get("question", ""), 120),
            chip_label=_truncate(body.get("chip_label", ""), 40),
        )

    elif event_type == "nav_click":
        _log_event(
            "analytics_nav_click",
            **base,
            target_section=body.get("target_section", ""),
            link_text=_truncate(body.get("link_text", ""), 60),
        )

    elif event_type == "button_click":
        _log_event(
            "analytics_button_click",
            **base,
            button_id=body.get("button_id", ""),
            button_label=_truncate(body.get("button_label", ""), 60),
            section_id=body.get("section_id", ""),
        )

    else:
        _log_event(f"analytics_{event_type}", **base, raw=body)

    return {"ok": True}


@app.get("/api/analytics/summary")
async def analytics_summary():
    if not ANALYTICS_FILE or not os.path.exists(ANALYTICS_FILE):
        return {"visits": 0}
    try:
        with open(ANALYTICS_FILE) as f:
            records = [json.loads(line) for line in f if line.strip()]
    except (json.JSONDecodeError, OSError):
        return {"visits": 0}

    if not records:
        return {"visits": 0}

    total_duration = sum(r.get("duration_s", 0) for r in records)
    total_scroll = sum(r.get("max_scroll_pct", 0) for r in records)
    avg_duration = round(total_duration / len(records))
    avg_scroll = round(total_scroll / len(records))

    return {
        "visits": len(records),
        "avg_duration_s": avg_duration,
        "avg_scroll_pct": avg_scroll,
        "latest": records[-1].get("timestamp", ""),
    }
