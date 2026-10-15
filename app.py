"""OpenAI-compatible HTTP facade for Hark."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from config import CFG
from account_pool import HarkAccountPool
from hark import HarkAuthError, HarkError, HarkGenerationError, HarkNetworkError
from task_store import TaskStore


CFG.ensure_dirs()
app = FastAPI(title="hark2api", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)
client = HarkAccountPool(CFG)
tasks = TaskStore(CFG.tasks_file)


def _error_type(status: int) -> str:
    if status == 401:
        return "authentication_error"
    if status == 404:
        return "not_found_error"
    if status == 504:
        return "timeout_error"
    if status == 501:
        return "not_implemented_error"
    if status >= 500:
        return "server_error"
    return "invalid_request_error"


def _openai_error(message: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {
        "message": message,
        "type": _error_type(status),
        "param": None,
        "code": status,
    }})


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/v1/"):
        return _openai_error(str(exc.detail), exc.status_code)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/v1/"):
        return _openai_error("request validation failed", 422)
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(HarkAuthError)
async def auth_error(request: Request, exc: HarkAuthError):
    return _openai_error(str(exc), 401) if request.url.path.startswith("/v1/") else JSONResponse(status_code=401, content={"detail": str(exc)})


@app.exception_handler(HarkGenerationError)
async def generation_error(request: Request, exc: HarkGenerationError):
    status = 504 if "timed out" in str(exc).lower() else 502
    return _openai_error(str(exc), status) if request.url.path.startswith("/v1/") else JSONResponse(status_code=status, content={"detail": str(exc)})


@app.exception_handler(HarkNetworkError)
async def network_error(request: Request, exc: HarkNetworkError):
    return _openai_error(str(exc), 502) if request.url.path.startswith("/v1/") else JSONResponse(status_code=502, content={"detail": str(exc)})


@app.exception_handler(HarkError)
async def upstream_error(request: Request, exc: HarkError):
    return _openai_error(str(exc), 502) if request.url.path.startswith("/v1/") else JSONResponse(status_code=502, content={"detail": str(exc)})


def auth(authorization: str | None = Header(default=None)):
    if not CFG.api_key:
        return True
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing Authorization: Bearer <key>")
    if authorization.split(None, 1)[1].strip() != CFG.api_key:
        raise HTTPException(401, "invalid API key")
    return True


class ChatMessage(BaseModel):
    role: str
    content: Any = None


class ChatRequest(BaseModel):
    model: str = "hark"
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    timeout: int | None = Field(default=None, ge=1, le=900)


class ResponsesRequest(BaseModel):
    model: str = "hark"
    input: Any = ""
    instructions: str | None = None
    stream: bool = False
    timeout: int | None = Field(default=None, ge=1, le=900)


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1)
    model: str = "hark-image"
    n: int = Field(default=1, ge=1, le=1)
    response_format: str = "url"
    timeout: int | None = Field(default=None, ge=1, le=900)
    async_: bool = False

    @model_validator(mode="before")
    @classmethod
    def accept_async_alias(cls, value: Any):
        if isinstance(value, dict) and "async" in value and "async_" not in value:
            value = dict(value)
            value["async_"] = value.pop("async")
        return value


class AccountCreateRequest(BaseModel):
    label: str = ""
    cookie_header: str = Field(min_length=1)


class AccountPatchRequest(BaseModel):
    label: str | None = None
    cookie_header: str | None = None
    enabled: bool | None = None


class MediaCleanupRequest(BaseModel):
    max_age_seconds: int = Field(default=7 * 86400, ge=1)
    dry_run: bool = False


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return "" if content is None else str(content)


def build_prompt(messages: list[ChatMessage]) -> str:
    blocks = []
    for message in messages:
        text = _content_text(message.content).strip()
        if text:
            blocks.append(f"{message.role.upper()}:\n{text}")
    if not blocks:
        raise HTTPException(422, "messages must contain text")
    if len(blocks) == 1 and messages[0].role == "user":
        return _content_text(messages[0].content).strip()
    return "\n\n".join(blocks) + "\n\nASSISTANT:"


def _responses_prompt(req: ResponsesRequest) -> str:
    messages: list[ChatMessage] = []
    if req.instructions:
        messages.append(ChatMessage(role="system", content=req.instructions))
    if isinstance(req.input, str):
        messages.append(ChatMessage(role="user", content=req.input))
    elif isinstance(req.input, list):
        for item in req.input:
            if isinstance(item, str):
                messages.append(ChatMessage(role="user", content=item))
            elif isinstance(item, dict) and item.get("role"):
                messages.append(ChatMessage(role=str(item["role"]), content=item.get("content")))
            elif isinstance(item, dict) and item.get("type") == "input_text":
                messages.append(ChatMessage(role="user", content=item.get("text")))
            elif isinstance(item, dict) and item.get("type") == "function_call_output":
                messages.append(ChatMessage(role="user", content=str(item.get("output") or "")))
    return build_prompt(messages)


def _base_url(request: Request) -> str:
    return (CFG.public_base.rstrip("/") or str(request.base_url).rstrip("/"))


def _completion_content(result, base_url: str) -> str:
    parts = [result.text] if result.text else []
    attachments = client.attachment_markdown(result.attachments, base_url)
    if attachments:
        parts.append(attachments)
    return "\n\n".join(parts)


def _image_data(images: list[dict[str, Any]], prompt: str, response_format: str, request: Request) -> list[dict[str, Any]]:
    images = [a for a in images if str(a.get("mediaType", "")).startswith("image/")]
    if not images:
        raise HarkGenerationError("Hark did not return an image")
    data = []
    for image in images:
        filename = Path(str(image.get("filename") or "")).name
        if not filename or filename != str(image.get("filename") or ""):
            raise HarkGenerationError("Hark returned an invalid image filename")
        item = {"revised_prompt": prompt}
        if response_format == "b64_json":
            path = Path(str(image.get("path") or (Path(CFG.media_dir) / filename)))
            item["b64_json"] = base64.b64encode(path.read_bytes()).decode()
        else:
            item["url"] = f"{_base_url(request)}/v1/media/{filename}"
        data.append(item)
    return data


def _image_response(result, prompt: str, response_format: str, request: Request) -> dict[str, Any]:
    return {"created": int(time.time()), "data": _image_data(result.attachments, prompt, response_format, request)}


def _task_result_images(result) -> list[dict[str, Any]]:
    images = []
    for attachment in result.attachments:
        if not str(attachment.get("mediaType", "")).startswith("image/"):
            continue
        filename = Path(str(attachment.get("filename") or "")).name
        if not filename or filename != str(attachment.get("filename") or ""):
            continue
        images.append({
            "filename": filename,
            "mediaType": str(attachment.get("mediaType") or "image/png"),
            "size": int(attachment.get("size") or 0),
        })
    return images


def _task_response(task: dict[str, Any], request: Request) -> dict[str, Any]:
    result = task.get("result") or {}
    output = {
        key: value
        for key, value in task.items()
        if key not in {"request_hash", "idempotency_key_hash", "account_id"}
    }
    if task.get("status") == "completed":
        output["data"] = _image_data(
            result.get("images") or [],
            str(task.get("prompt") or ""),
            str(task.get("response_format") or "url"),
            request,
        )
    return output


def _queue_image(
    prompt: str,
    timeout: int | None,
    response_format: str,
    idempotency_key: str | None = None,
    input_attachment: tuple[str, str, bytes] | None = None,
):
    if response_format not in {"url", "b64_json"}:
        raise HTTPException(422, "response_format must be url or b64_json")
    if idempotency_key and len(idempotency_key) > 256:
        raise HTTPException(400, "Idempotency-Key too long")
    input_meta = None
    if input_attachment:
        filename, media_type, content = input_attachment
        input_meta = {
            "filename": filename,
            "media_type": media_type,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    request_hash = hashlib.sha256(json.dumps({
        "prompt": prompt,
        "response_format": response_format,
        "input": input_meta,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    existing = tasks.list(limit=1000)
    if key_hash:
        for task in existing:
            if task.get("idempotency_key_hash") != key_hash:
                continue
            if task.get("request_hash") != request_hash:
                raise HTTPException(409, "Idempotency-Key reused with different request")
            return JSONResponse(status_code=202, content={
                "id": task["id"], "task_id": task["id"], "object": "image.task",
                "status": task["status"], "progress": task.get("progress"),
                "progress_known": bool(task.get("progress_known")),
                "phase": task.get("phase", "排队中"),
                "created_at": task["created_at"],
            })
    active = sum(
        task.get("kind") == "image" and task.get("status") in {"queued", "processing"}
        for task in existing
    )
    if active >= CFG.task_queue_limit:
        raise HTTPException(429, "image task queue is full")
    task = tasks.create(
        "image",
        prompt,
        response_format=response_format,
        request_hash=request_hash,
        idempotency_key_hash=key_hash,
    )
    task_id = task["id"]

    def worker():
        started = time.time()
        tasks.update(
            task_id,
            status="processing",
            progress=None,
            progress_known=False,
            phase="等待 Hark 响应",
        )

        def on_status(stage: str):
            tasks.update(
                task_id,
                phase=_task_phase(stage),
                progress=None,
                progress_known=False,
            )

        try:
            result = client.run(
                prompt,
                timeout=timeout,
                on_status=on_status,
                require_attachment=True,
                input_attachment=input_attachment,
            )
            images = _task_result_images(result)
            if not images:
                raise HarkGenerationError("Hark did not return an image")
            tasks.update(
                task_id,
                status="completed",
                progress=100,
                progress_known=True,
                phase="已完成",
                elapsed=round(time.time() - started, 1),
                result={"images": images},
                account_id=result.account_id,
                usage_status="idle",
            )
            _schedule_usage_refresh(result, task_id)
        except Exception as exc:
            tasks.update(
                task_id,
                status="failed",
                progress=None,
                progress_known=False,
                phase="失败",
                elapsed=round(time.time() - started, 1),
                error=str(exc),
            )

    threading.Thread(target=worker, daemon=True, name=f"hark-task-{task_id}").start()
    return JSONResponse(status_code=202, content={
        "id": task_id,
        "task_id": task_id,
        "object": "image.task",
        "status": "queued",
        "progress": None,
        "progress_known": False,
        "phase": "排队中",
        "created_at": task["created_at"],
    })


def _sse(data: dict[str, Any]) -> str:
    return "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def _response_event(event: str, data: dict[str, Any]) -> str:
    return "event: " + event + "\n" + _sse(data)


def _response_envelope(response_id: str, message_id: str, model: str, status: str, text: str = "") -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": [{
            "type": "message",
            "id": message_id,
            "role": "assistant",
            "status": "completed" if status == "completed" else "in_progress",
            "content": ([{"type": "output_text", "text": text, "annotations": []}] if text else []),
        }],
        "output_text": text,
    }


def _stream_completion(prompt: str, model: str, timeout: int | None, base_url: str):
    completion_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    events: queue.Queue[tuple[str, Any]] = queue.Queue()

    def worker():
        try:
            def on_text(delta: str):
                events.put(("delta", delta))

            result = client.run(prompt, timeout=timeout, on_text=on_text)
            attachment_text = client.attachment_markdown(result.attachments, base_url)
            if attachment_text:
                events.put(("delta", ("\n\n" if result.text else "") + attachment_text))
            events.put(("done", None))
        except Exception as exc:  # the generator must report errors after headers are sent
            events.put(("error", exc))

    threading.Thread(target=worker, daemon=True).start()
    yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    while True:
        kind, value = events.get()
        if kind == "delta":
            yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0, "delta": {"content": value}, "finish_reason": None}]})
        elif kind == "error":
            yield _sse({"error": {"message": str(value), "type": "server_error"}})
            break
        else:
            yield _sse({"id": completion_id, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"
            break


def _stream_response(prompt: str, model: str, timeout: int | None, base_url: str):
    response_id = "resp_" + uuid.uuid4().hex
    message_id = "msg_" + uuid.uuid4().hex
    events: queue.Queue[tuple[str, Any]] = queue.Queue()

    def worker():
        try:
            def on_text(delta: str):
                events.put(("delta", delta))

            result = client.run(prompt, timeout=timeout, on_text=on_text)
            attachment_text = client.attachment_markdown(result.attachments, base_url)
            if attachment_text:
                events.put(("delta", ("\n\n" if result.text else "") + attachment_text))
            events.put(("done", None))
        except Exception as exc:  # the generator must report errors after headers are sent
            events.put(("error", exc))

    threading.Thread(target=worker, daemon=True).start()
    yield _response_event("response.created", {
        "type": "response.created",
        "response": _response_envelope(response_id, message_id, model, "in_progress"),
    })
    yield _response_event("response.output_item.added", {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"id": message_id, "type": "message", "role": "assistant", "status": "in_progress", "content": []},
    })
    yield _response_event("response.content_part.added", {
        "type": "response.content_part.added",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })
    full = ""
    while True:
        kind, value = events.get()
        if kind == "delta":
            full += value
            yield _response_event("response.output_text.delta", {
                "type": "response.output_text.delta", "item_id": message_id,
                "output_index": 0, "content_index": 0, "delta": value,
            })
        elif kind == "error":
            yield _response_event("response.failed", {
                "type": "response.failed",
                "response": _response_envelope(response_id, message_id, model, "failed"),
            })
            break
        else:
            yield _response_event("response.output_text.done", {
                "type": "response.output_text.done", "item_id": message_id,
                "output_index": 0, "content_index": 0, "text": full,
            })
            yield _response_event("response.output_item.done", {
                "type": "response.output_item.done", "output_index": 0,
                "item": {"id": message_id, "type": "message", "role": "assistant", "status": "completed",
                          "content": [{"type": "output_text", "text": full, "annotations": []}]},
            })
            yield _response_event("response.completed", {
                "type": "response.completed",
                "response": _response_envelope(response_id, message_id, model, "completed", full),
            })
            break


def _admin_html() -> str:
    try:
        return Path(__file__).with_name("admin.html").read_text(encoding="utf-8")
    except OSError:
        return "<!doctype html><title>hark2api</title><p>admin.html is missing.</p>"


@app.get("/", response_class=HTMLResponse)
def root():
    return _admin_html()


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return _admin_html()


@app.get("/favicon.ico")
def favicon():
    return Response(content=b"", media_type="image/x-icon")


@app.get("/docs/QQ群.png")
def community_qr():
    path = Path(__file__).with_name("docs") / "QQ群.png"
    if not path.is_file():
        raise HTTPException(404, "community QR code not found")
    return FileResponse(path, media_type="image/png")


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "hark2api", "cookie_configured": client.cookie_count() > 0}


@app.get("/readyz")
def readyz():
    if not client.cookie_count():
        raise HTTPException(503, "HARK2API_COOKIE or HARK2API_COOKIE_FILE is not configured")
    return {"ok": True}


@app.get("/v1/models")
def models(_=Depends(auth)):
    now = int(time.time())
    return {"object": "list", "data": [
        {"id": "hark", "object": "model", "created": now, "owned_by": "hark"},
        {"id": "hark-image", "object": "model", "created": now, "owned_by": "hark"},
    ]}


@app.post("/v1/chat/completions")
def chat_completions(req: ChatRequest, request: Request, _=Depends(auth)):
    prompt = build_prompt(req.messages)
    if req.stream:
        return StreamingResponse(
            _stream_completion(prompt, req.model, req.timeout, _base_url(request)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    result = client.run(prompt, timeout=req.timeout)
    content = _completion_content(result, _base_url(request))
    return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion",
            "created": int(time.time()), "model": req.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


@app.post("/v1/responses")
def responses(req: ResponsesRequest, request: Request, _=Depends(auth)):
    prompt = _responses_prompt(req)
    response_id = "resp_" + uuid.uuid4().hex
    message_id = "msg_" + uuid.uuid4().hex
    if req.stream:
        return StreamingResponse(
            _stream_response(prompt, req.model, req.timeout, _base_url(request)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    result = client.run(prompt, timeout=req.timeout)
    content = _completion_content(result, _base_url(request))
    return _response_envelope(response_id, message_id, req.model, "completed", content)


@app.post("/v1/images/generations")
def image_generations(req: ImageRequest, request: Request, _=Depends(auth)):
    if req.response_format not in {"url", "b64_json"}:
        raise HTTPException(422, "response_format must be url or b64_json")
    if req.async_:
        return _queue_image(req.prompt, req.timeout, req.response_format)
    result = client.run(req.prompt, timeout=req.timeout, require_attachment=True)
    _schedule_usage_refresh(result)
    return _image_response(result, req.prompt, req.response_format, request)


@app.post("/v1/images/tasks")
def create_image_task(
    req: ImageRequest,
    idempotency_key: str | None = Header(default=None),
    _=Depends(auth),
):
    return _queue_image(req.prompt, req.timeout, req.response_format, idempotency_key)


@app.get("/v1/images/tasks/{task_id}")
def get_image_task(task_id: str, request: Request, _=Depends(auth)):
    task = tasks.get(task_id)
    if not task or task.get("kind") != "image":
        raise HTTPException(404, "image task not found")
    return _task_response(task, request)


@app.post("/v1/images/edits")
def image_edits(
    request: Request,
    prompt: str = Form(...),
    image: UploadFile = File(...),
    model: str = Form("hark-image"),
    response_format: str = Form("url"),
    n: int = Form(1),
    timeout: int | None = Form(default=None),
    size: str | None = Form(default=None),
    mask: UploadFile | None = File(default=None),
    async_mode: Annotated[bool, Form(alias="async")] = False,
    idempotency_key: str | None = Header(default=None),
    _=Depends(auth),
):
    prompt = prompt.strip()
    if not prompt:
        raise HTTPException(422, "prompt must not be empty")
    if response_format not in {"url", "b64_json"}:
        raise HTTPException(422, "response_format must be url or b64_json")
    if n != 1:
        raise HTTPException(422, "Hark supports one edited image per request")
    if timeout is not None and not 1 <= timeout <= 900:
        raise HTTPException(422, "timeout must be between 1 and 900 seconds")
    if mask is not None:
        raise HTTPException(422, "mask is not supported by Hark")
    if size:
        raise HTTPException(422, "size is not supported by Hark")

    filename = Path(image.filename or "reference.png").name or "reference.png"
    media_type = (image.content_type or mimetypes.guess_type(filename)[0] or "").split(";", 1)[0].lower()
    if not media_type.startswith("image/"):
        raise HTTPException(422, "image must be an image file")
    content = image.file.read(CFG.max_media_bytes + 1)
    if len(content) > CFG.max_media_bytes:
        raise HTTPException(413, "image exceeds the configured size limit")

    if async_mode:
        return _queue_image(
            prompt,
            timeout,
            response_format,
            idempotency_key,
            input_attachment=(filename, media_type, content),
        )

    result = client.run(
        prompt,
        timeout=timeout,
        require_attachment=True,
        input_attachment=(filename, media_type, content),
    )
    _schedule_usage_refresh(result)
    return _image_response(result, prompt, response_format, request)


@app.get("/v1/media/{name}")
def media(name: str):
    if Path(name).name != name:
        raise HTTPException(404, "media not found")
    path = Path(CFG.media_dir) / name
    if not path.is_file():
        raise HTTPException(404, "media not found")
    return FileResponse(path)


def _media_items(request: Request) -> list[dict[str, Any]]:
    items = []
    for path in Path(CFG.media_dir).iterdir():
        if not path.is_file() or path.name == ".gitkeep":
            continue
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        items.append({
            "name": path.name,
            "url": f"{_base_url(request)}/v1/media/{path.name}",
            "bytes": path.stat().st_size,
            "mtime": int(path.stat().st_mtime),
            "kind": "video" if media_type.startswith("video/") else "image",
            "media_type": media_type,
        })
    return sorted(items, key=lambda item: item["mtime"], reverse=True)


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in task.items()
        if key not in {"request_hash", "idempotency_key_hash", "account_id"}
    }


def _task_phase(stage: str) -> str:
    return {
        "waiting": "等待 Hark 响应",
        "generating": "Hark 生成中",
        "finalizing": "整理结果",
    }.get(stage, "处理中")


def _schedule_usage_refresh(result, task_id: str | None = None) -> None:
    account_id = getattr(result, "account_id", None)
    if not account_id:
        return
    client.set_usage_pending(account_id)
    if task_id:
        tasks.update(task_id, usage_status="loading")

    def worker():
        account = client.refresh_usage(
            account_id,
            timeout=min(int(getattr(CFG, "chat_timeout", 120)), 180),
        )
        if task_id:
            usage = account.get("usage")
            tasks.update(
                task_id,
                usage=usage,
                usage_status=(usage or {}).get("status", "error"),
            )

    threading.Thread(
        target=worker,
        daemon=True,
        name=f"hark-usage-{account_id}",
    ).start()


@app.get("/admin/accounts")
def admin_accounts(_=Depends(auth)):
    return {"accounts": client.list_accounts(), "stats": client.stats()}


@app.post("/admin/accounts")
def admin_add_account(req: AccountCreateRequest, _=Depends(auth)):
    try:
        return client.add_account(req.label, req.cookie_header)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@app.patch("/admin/accounts/{account_id}")
def admin_update_account(account_id: str, req: AccountPatchRequest, _=Depends(auth)):
    try:
        account = client.update_account(account_id, **req.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if account is None:
        raise HTTPException(404, "account not found")
    return account


@app.delete("/admin/accounts/{account_id}")
def admin_delete_account(account_id: str, _=Depends(auth)):
    if not client.delete_account(account_id):
        raise HTTPException(404, "account not found")
    return {"ok": True, "deleted": account_id}


@app.post("/admin/accounts/{account_id}/test")
def admin_test_account(account_id: str, _=Depends(auth)):
    try:
        result = client.test_account(account_id)
    except KeyError:
        raise HTTPException(404, "account not found") from None
    return result


@app.post("/admin/accounts/{account_id}/usage")
def admin_refresh_usage(account_id: str, _=Depends(auth)):
    try:
        return client.refresh_usage(
            account_id,
            timeout=min(int(getattr(CFG, "chat_timeout", 120)), 180),
        )
    except KeyError:
        raise HTTPException(404, "account not found") from None


@app.get("/admin/tasks")
def admin_tasks(limit: int = 50, _=Depends(auth)):
    return {"tasks": [_public_task(task) for task in tasks.list(max(1, min(limit, 200)))]}


@app.delete("/admin/tasks/{task_id}")
def admin_delete_task(task_id: str, _=Depends(auth)):
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    if task.get("status") in {"queued", "processing"}:
        raise HTTPException(409, "cannot delete an active task")
    tasks.remove(task_id)
    return {"ok": True, "deleted": task_id}


@app.post("/admin/tasks/clear")
def admin_clear_tasks(_=Depends(auth)):
    removed = 0
    for task in tasks.list(limit=10000):
        if task.get("status") in {"completed", "failed"} and tasks.remove(task["id"]):
            removed += 1
    return {"ok": True, "removed": removed}


@app.get("/admin/media")
def admin_media(request: Request, _=Depends(auth)):
    items = _media_items(request)
    return {"media": items, "count": len(items), "bytes": sum(item["bytes"] for item in items)}


@app.post("/admin/media/cleanup")
def admin_cleanup_media(req: MediaCleanupRequest, _=Depends(auth)):
    cutoff = time.time() - req.max_age_seconds
    removed = 0
    bytes_removed = 0
    errors = []
    for path in Path(CFG.media_dir).iterdir():
        if not path.is_file() or path.name == ".gitkeep" or path.stat().st_mtime > cutoff:
            continue
        size = path.stat().st_size
        if req.dry_run:
            removed += 1
            bytes_removed += size
            continue
        try:
            path.unlink()
            removed += 1
            bytes_removed += size
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    return {"ok": not errors, "dry_run": req.dry_run, "removed": removed,
            "bytes_removed": bytes_removed, "errors": errors}


@app.delete("/admin/media/{name}")
def admin_delete_media(name: str, _=Depends(auth)):
    if Path(name).name != name or name == ".gitkeep":
        raise HTTPException(400, "invalid media filename")
    path = Path(CFG.media_dir) / name
    if not path.is_file():
        raise HTTPException(404, "media not found")
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(500, f"media delete failed: {exc}") from None
    return {"ok": True, "deleted": name}


@app.get("/admin/status")
def admin_status(_=Depends(auth)):
    cookie_count = client.cookie_count()
    media = [path for path in Path(CFG.media_dir).iterdir() if path.is_file() and path.name != ".gitkeep"]
    return {
        "service": "hark2api",
        "upstream": CFG.upstream,
        "cookie_count": cookie_count,
        "cookie_configured": cookie_count > 0,
        "api_key_enabled": bool(CFG.api_key),
        "accounts": client.stats(),
        "media": {"count": len(media), "bytes": sum(path.stat().st_size for path in media)},
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=CFG.host, port=CFG.port)
