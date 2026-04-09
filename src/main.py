import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Personal Website API", version="1.0.0")

AI_SERVER_URL = os.getenv("AI_SERVER_URL") or os.getenv("URL")
AI_SERVER_API_KEY = os.getenv("AI_SERVER_API_KEY") or os.getenv("API_KEY")
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "60"))
DEFAULT_AI_RESPONSE_TEXT = os.getenv("DEFAULT_AI_RESPONSE_TEXT", "Hello! How can I help you today?")
FALLBACK_RESPONSE_TEXT = os.getenv(
    "FALLBACK_RESPONSE_TEXT",
    "Sorry, Kevin is busy fixing me right now! 🔧 But you can still look around the page to learn more about him. Check out his projects, skills, and homelab sections — there's a lot of cool stuff!",
)

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


def _extract_ai_preview(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("response", "answer", "content", "output_text", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return None


def _to_upstream_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        return {"messages": messages}

    user_text = _extract_user_prompt(payload)
    if user_text:
        return {"messages": [{"role": "user", "content": user_text}]}

    raise HTTPException(
        status_code=400,
        detail="Body must include `messages` or a user text field (`message`, `prompt`, `question`, `input`)",
    )


def _log_event(event: str, **fields: Any) -> None:
    record = {"event": event, "ts": _utc_now_iso(), **fields}
    logger.info(json.dumps(record, ensure_ascii=True, default=str))


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _should_use_default_response(payload: Any) -> bool:
    if isinstance(payload, dict) and _is_truthy(payload.get("use_default_response")):
        return True
    return _is_truthy(os.getenv("USE_DEFAULT_RESPONSE", "false"))


def _resolve_default_response_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return DEFAULT_AI_RESPONSE_TEXT

    if isinstance(payload.get("default_response"), str) and payload["default_response"].strip():
        return payload["default_response"].strip()

    if isinstance(payload.get("default_response"), dict):
        extracted = _extract_ai_preview(payload["default_response"])
        if extracted:
            return extracted

    return DEFAULT_AI_RESPONSE_TEXT


def _should_use_fallback_response(payload: Any) -> bool:
    if isinstance(payload, dict) and _is_truthy(payload.get("use_fallback_response")):
        return True
    return _is_truthy(os.getenv("USE_FALLBACK_RESPONSE", "false"))


def _stream_plain_text(
    *,
    text: str,
    event_name: str,
    request_id: str | None,
    client_ip: str,
    user_prompt_preview: str | None,
    upstream_status: int,
    fallback_reason: str | None = None,
) -> StreamingResponse:
    started = time.perf_counter()

    async def generator():
        stream_error: str | None = None
        emitted_parts: list[str] = []

        try:
            for token in text.split():
                chunk = token + " "
                emitted_parts.append(chunk)
                yield chunk
                await asyncio.sleep(0.02)
        except Exception as exc:
            stream_error = str(exc)
            raise
        finally:
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


def _extract_stream_token(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            delta = first.get("delta")
            if isinstance(delta, dict):
                delta_content = delta.get("content")
                if isinstance(delta_content, str):
                    return delta_content
            message = first.get("message")
            if isinstance(message, dict):
                message_content = message.get("content")
                if isinstance(message_content, str):
                    return message_content
            text = first.get("text")
            if isinstance(text, str):
                return text

    for key in ("content", "text", "response", "output_text"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


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
    if request.method in {"POST", "PUT", "PATCH"} and "application/json" in content_type:
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


@app.post("/ai/proxy")
async def ai_proxy(request: Request):
    if not AI_SERVER_URL:
        raise HTTPException(status_code=500, detail="AI_SERVER_URL (or URL) is not configured")
    if not AI_SERVER_API_KEY:
        raise HTTPException(status_code=500, detail="AI_SERVER_API_KEY (or API_KEY) is not configured")

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc

    upstream_payload = _to_upstream_payload(payload)
    headers: dict[str, str] = {
        "Authorization": f"Bearer {AI_SERVER_API_KEY}",
        "Content-Type": "application/json",
    }

    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as client:
            upstream_response = await client.post(AI_SERVER_URL, json=upstream_payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="AI server request timed out") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"AI server request failed: {exc}") from exc

    duration_ms = int((time.perf_counter() - start) * 1000)
    response_content_type = upstream_response.headers.get("content-type", "")

    if "application/json" in response_content_type:
        try:
            response_body: Any = upstream_response.json()
        except ValueError:
            response_body = {"text": upstream_response.text}
    else:
        response_body = {"text": upstream_response.text}

    _log_event(
        "ai_proxy",
        request_id=getattr(request.state, "request_id", None),
        client_ip=getattr(request.state, "client_ip", _get_client_ip(request)),
        upstream_status=upstream_response.status_code,
        duration_ms=duration_ms,
        user_prompt_preview=_truncate(_extract_user_prompt(upstream_payload), 300),
        ai_response_preview=_truncate(_extract_ai_preview(response_body), 300),
    )

    return JSONResponse(status_code=upstream_response.status_code, content=response_body)


@app.post("/ai/proxy/stream")
async def ai_proxy_stream(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc

    request_id = getattr(request.state, "request_id", None)
    client_ip = getattr(request.state, "client_ip", _get_client_ip(request))
    user_prompt_preview = _truncate(_extract_user_prompt(payload), 300)

    if _should_use_default_response(payload):
        default_text = _resolve_default_response_text(payload)
        return _stream_plain_text(
            text=default_text,
            event_name="ai_proxy_stream_default",
            request_id=request_id,
            client_ip=client_ip,
            user_prompt_preview=user_prompt_preview,
            upstream_status=200,
            fallback_reason="forced_default_response",
        )

    if _should_use_fallback_response(payload):
        return _stream_plain_text(
            text=FALLBACK_RESPONSE_TEXT,
            event_name="ai_proxy_stream_fallback",
            request_id=request_id,
            client_ip=client_ip,
            user_prompt_preview=user_prompt_preview,
            upstream_status=200,
            fallback_reason="forced_fallback_response",
        )

    if not AI_SERVER_URL:
        return _stream_plain_text(
            text=FALLBACK_RESPONSE_TEXT,
            event_name="ai_proxy_stream_fallback",
            request_id=request_id,
            client_ip=client_ip,
            user_prompt_preview=user_prompt_preview,
            upstream_status=500,
            fallback_reason="missing_ai_server_url",
        )
    if not AI_SERVER_API_KEY:
        return _stream_plain_text(
            text=FALLBACK_RESPONSE_TEXT,
            event_name="ai_proxy_stream_fallback",
            request_id=request_id,
            client_ip=client_ip,
            user_prompt_preview=user_prompt_preview,
            upstream_status=500,
            fallback_reason="missing_ai_server_api_key",
        )

    upstream_payload = _to_upstream_payload(payload)
    upstream_payload["stream"] = True
    headers: dict[str, str] = {
        "Authorization": f"Bearer {AI_SERVER_API_KEY}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(
        connect=min(AI_TIMEOUT_SECONDS, 30.0),
        read=AI_TIMEOUT_SECONDS,
        write=AI_TIMEOUT_SECONDS,
        pool=AI_TIMEOUT_SECONDS,
    )
    client = httpx.AsyncClient(timeout=timeout)
    request_obj = client.build_request("POST", AI_SERVER_URL, json=upstream_payload, headers=headers)

    try:
        upstream_response = await client.send(request_obj, stream=True)
    except httpx.TimeoutException as exc:
        await client.aclose()
        return _stream_plain_text(
            text=FALLBACK_RESPONSE_TEXT,
            event_name="ai_proxy_stream_fallback",
            request_id=request_id,
            client_ip=client_ip,
            user_prompt_preview=user_prompt_preview,
            upstream_status=504,
            fallback_reason=f"upstream_timeout: {exc}",
        )
    except httpx.RequestError as exc:
        await client.aclose()
        return _stream_plain_text(
            text=FALLBACK_RESPONSE_TEXT,
            event_name="ai_proxy_stream_fallback",
            request_id=request_id,
            client_ip=client_ip,
            user_prompt_preview=user_prompt_preview,
            upstream_status=502,
            fallback_reason=f"upstream_request_error: {exc}",
        )

    if upstream_response.status_code >= 400:
        error_text = await upstream_response.aread()
        await upstream_response.aclose()
        await client.aclose()
        error_detail = error_text.decode("utf-8", errors="replace") or "AI server returned an error"
        return _stream_plain_text(
            text=FALLBACK_RESPONSE_TEXT,
            event_name="ai_proxy_stream_fallback",
            request_id=request_id,
            client_ip=client_ip,
            user_prompt_preview=user_prompt_preview,
            upstream_status=upstream_response.status_code,
            fallback_reason=f"upstream_http_error: {error_detail[:200]}",
        )
    user_prompt_preview = _truncate(_extract_user_prompt(upstream_payload), 300)
    started = time.perf_counter()

    async def stream_generator():
        ai_response_preview_parts: list[str] = []
        stream_error: str | None = None
        content_type = upstream_response.headers.get("content-type", "")

        try:
            if "text/event-stream" in content_type:
                async for line in upstream_response.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if data == "[DONE]":
                        break

                    try:
                        event_payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    token = _extract_stream_token(event_payload)
                    if token:
                        ai_response_preview_parts.append(token)
                        yield token
            elif "application/json" in content_type:
                body_bytes = await upstream_response.aread()
                body_text = body_bytes.decode("utf-8", errors="replace")
                try:
                    json_body = json.loads(body_text)
                except json.JSONDecodeError:
                    token = body_text
                else:
                    token = _extract_ai_preview(json_body) or body_text

                if token:
                    ai_response_preview_parts.append(token)
                    yield token
            else:
                async for chunk in upstream_response.aiter_text():
                    if chunk:
                        ai_response_preview_parts.append(chunk)
                        yield chunk
        except Exception as exc:
            stream_error = str(exc)
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            _log_event(
                "ai_proxy_stream",
                request_id=request_id,
                client_ip=client_ip,
                upstream_status=upstream_response.status_code,
                duration_ms=duration_ms,
                user_prompt_preview=user_prompt_preview,
                ai_response_preview=_truncate("".join(ai_response_preview_parts), 300),
                stream_error=stream_error,
            )
            await upstream_response.aclose()
            await client.aclose()

    return StreamingResponse(stream_generator(), media_type="text/plain; charset=utf-8")
