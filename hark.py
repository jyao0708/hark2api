"""Small HTTP client for Hark's authenticated web API."""
from __future__ import annotations

import mimetypes
import os
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import requests


class HarkError(RuntimeError):
    """Base error for upstream failures."""


class HarkAuthError(HarkError):
    pass


class HarkNetworkError(HarkError):
    pass


class HarkGenerationError(HarkError):
    pass


class HarkUsageError(HarkError):
    pass


USAGE_PROMPT = (
    "查额度。只输出一个 JSON 对象，第一个字符必须是 {，最后一个字符必须是 }。"
    "禁止任何解释、寒暄、markdown、代码块或换行。"
    "格式（严格照抄键名和顺序）："
    "{\"d\":{\"left\":\"98%\",\"reset\":\"2026-10-16 11:07\"},"
    "\"m\":{\"left\":\"100%\",\"reset\":\"2026-11-01 08:00\"}}"
    "规则：d = 每日额度，m = 月度额度，left = 100 减去已用百分比，整数加 %，"
    "reset 用 Asia/Shanghai 时间，格式 YYYY-MM-DD HH:mm。"
    "取不到数据时只输出 {\"error\":\"原因\"}。"
)


def parse_cookie_header(text: str | None) -> dict[str, str]:
    """Parse a browser Cookie header without retaining cookie attributes."""
    out: dict[str, str] = {}
    for item in (text or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


def parse_usage_response(text: str) -> dict[str, dict[str, str]]:
    raw = str(text or "")
    if not raw.startswith("{") or not raw.endswith("}") or "\n" in raw or "\r" in raw:
        raise HarkUsageError("额度响应不是严格 JSON 对象")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise HarkUsageError("额度响应 JSON 解析失败") from None
    if not isinstance(value, dict):
        raise HarkUsageError("额度响应不是 JSON 对象")
    if "error" in value:
        raise HarkUsageError(f"Hark 未返回额度：{str(value.get('error') or '未知原因')[:160]}")

    def window(name: str, label: str) -> dict[str, str]:
        item = value.get(name)
        if not isinstance(item, dict):
            raise HarkUsageError(f"额度响应缺少 {label} 数据")
        left = item.get("left")
        reset = item.get("reset")
        if not isinstance(left, str) or not re.fullmatch(r"(?:0|[1-9]\d?|100)%", left):
            raise HarkUsageError(f"{label}剩余额度格式无效")
        if not isinstance(reset, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", reset):
            raise HarkUsageError(f"{label}重置时间格式无效")
        try:
            datetime.strptime(reset, "%Y-%m-%d %H:%M")
        except ValueError:
            raise HarkUsageError(f"{label}重置时间格式无效") from None
        return {"left": left, "reset": reset}

    return {"daily": window("d", "每日"), "monthly": window("m", "月度")}


def history_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize both Hark history shapes into log order."""
    raw = payload.get("messages") or {}
    if isinstance(raw, dict):
        by_id = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    elif isinstance(raw, list):
        by_id = {str(v.get("id")): v for v in raw if isinstance(v, dict) and v.get("id")}
    else:
        by_id = {}

    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in payload.get("log") or []:
        if not isinstance(entry, dict):
            continue
        message_id = str(entry.get("id") or "")
        message = by_id.get(message_id)
        if message is not None:
            ordered.append(message)
            seen.add(message_id)
    for message_id, message in by_id.items():
        if message_id not in seen:
            ordered.append(message)
    return ordered


def reply_messages(payload: dict[str, Any], user_message_id: str) -> list[dict[str, Any]]:
    return [
        message
        for message in history_messages(payload)
        if message.get("role") == "assistant"
        and message.get("triggeredByMessageId") == user_message_id
    ]


def reply_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        if message.get("source") in {"web", "onboarding"}:
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip() and content not in parts:
            parts.append(content.strip())
    return "\n\n".join(parts)


def reply_attachments(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        for attachment in message.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            key = str(attachment.get("id") or attachment.get("downloadUrl") or attachment.get("name") or "")
            if key and key not in seen:
                seen.add(key)
                out.append(attachment)
    return out


def reply_stage(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "waiting"
    active_states = {"queued", "pending", "running", "processing", "in_progress", "streaming"}
    if any(
        message.get("isStreaming") is True
        or str(message.get("jobStatus") or "").lower() in active_states
        for message in messages
    ):
        return "generating"
    if any(
        message.get("attachments")
        or isinstance(message.get("content"), str) and message["content"].strip()
        for message in messages
    ):
        return "finalizing"
    return "waiting"


def reply_terminal(messages: list[dict[str, Any]]) -> bool:
    """Return true once Hark has a visible answer or a completed tool run."""
    complete_states = {"completed", "failed", "cancelled", "interrupted"}
    active_states = {"queued", "pending", "running", "processing", "in_progress", "streaming"}
    if any(
        message.get("isStreaming") is True
        or str(message.get("jobStatus") or "").lower() in active_states
        for message in messages
    ):
        return False
    for message in messages:
        source = message.get("source")
        if source not in {"web", "onboarding"}:
            if isinstance(message.get("content"), str) and message["content"].strip():
                return True
            if message.get("attachments"):
                return True
        if message.get("jobStatus") in complete_states:
            return True
    return False


@dataclass(slots=True)
class HarkResult:
    conversation_id: str
    message_id: str
    text: str
    attachments: list[dict[str, Any]]
    account_id: str | None = None


class HarkClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base_url = cfg.upstream.rstrip("/") + "/api/"
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "User-Agent": cfg.user_agent,
            "Origin": cfg.upstream.rstrip("/"),
            "Referer": cfg.upstream.rstrip("/") + "/",
            "X-Hark-Locale": cfg.locale,
        })
        self._load_cookies()
        self.lock = threading.Lock()

    def _load_cookies(self) -> None:
        header = self.cfg.cookie_header
        if self.cfg.cookie_file:
            try:
                header = Path(self.cfg.cookie_file).read_text(encoding="utf-8")
            except OSError as exc:
                raise HarkAuthError(f"cannot read cookie file: {exc}") from None
        for name, value in parse_cookie_header(header).items():
            self.session.cookies.set(name, value, domain="hark.com", path="/")

    def cookie_count(self) -> int:
        return len(self.session.cookies)

    def _request_json(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        kwargs.setdefault("timeout", self.cfg.request_timeout)
        kwargs.setdefault("allow_redirects", False)
        try:
            response = self.session.request(method, urljoin(self.base_url, path.lstrip("/")), **kwargs)
        except requests.RequestException as exc:
            raise HarkNetworkError(f"Hark request failed: {type(exc).__name__}") from None
        if response.status_code in (401, 403):
            raise HarkAuthError("Hark session is not authenticated or has expired")
        if 300 <= response.status_code < 400:
            raise HarkAuthError("Hark redirected the API request; refresh the cookie")
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:240]
            raise HarkError(f"Hark API HTTP {response.status_code}: {detail or 'upstream error'}")
        try:
            data = response.json()
        except ValueError:
            raise HarkError("Hark returned a non-JSON response") from None
        if not isinstance(data, dict):
            raise HarkError("Hark returned an unexpected JSON response")
        return data

    def get_user_id(self) -> str:
        data = self._request_json("GET", "/auth/get-session")
        user_id = ((data.get("user") or {}).get("id"))
        if not user_id:
            raise HarkAuthError("Hark session did not contain a user")
        return str(user_id)

    def create_conversation(self) -> str:
        data = self._request_json("POST", "/conversations", json={
            "title": "未命名项目",
            "timezone": self.cfg.timezone,
            "autoTitle": True,
        })
        conversation_id = data.get("conversationId")
        if not conversation_id:
            raise HarkError("Hark did not return a conversation id")
        return str(conversation_id)

    def send_message(
        self,
        conversation_id: str,
        prompt: str,
        user_id: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> str:
        message_id = str(uuid.uuid4())
        payload = {
            "timezone": self.cfg.timezone,
            "conversationId": conversation_id,
            "message": prompt,
            "idempotencyKey": message_id,
            "responseMessageId": str(uuid.uuid4()),
            "expectedUserId": user_id,
        }
        if attachments:
            payload["attachments"] = attachments
        data = self._request_json(
            "POST",
            "/messages/send",
            params={"cid": conversation_id},
            json=payload,
        )
        returned_id = data.get("messageId") or message_id
        if not data.get("success", True):
            raise HarkGenerationError("Hark rejected the message")
        return str(returned_id)

    def upload_attachment(
        self,
        conversation_id: str,
        content: bytes,
        filename: str,
        media_type: str,
    ) -> dict[str, Any]:
        if len(content) > self.cfg.max_media_bytes:
            raise HarkGenerationError("input attachment exceeds the configured size limit")
        filename = Path(filename).name or "attachment"
        media_type = media_type.split(";", 1)[0].strip().lower() or "application/octet-stream"
        presigned = self._request_json(
            "POST",
            f"/files/upload/{conversation_id}/presign",
            json={"filename": filename, "content_type": media_type, "size": len(content)},
        )
        upload_url = presigned.get("upload_url")
        if not isinstance(upload_url, str) or not upload_url.startswith(("http://", "https://")):
            raise HarkGenerationError("Hark did not return a valid attachment upload URL")
        try:
            response = self.session.put(
                upload_url,
                data=content,
                headers={"Content-Type": str(presigned.get("content_type") or media_type)},
                timeout=max(self.cfg.request_timeout, 60),
            )
        except requests.RequestException as exc:
            raise HarkNetworkError(f"attachment upload failed: {type(exc).__name__}") from None
        try:
            if response.status_code >= 400:
                raise HarkError(f"attachment upload failed: HTTP {response.status_code}")
        finally:
            response.close()

        attachment_id = presigned.get("attachment_id")
        s3_key = presigned.get("s3_key")
        path = presigned.get("path")
        if not all(isinstance(value, str) and value for value in (attachment_id, s3_key, path)):
            raise HarkGenerationError("Hark returned incomplete attachment metadata")
        finalized = self._request_json(
            "POST",
            f"/files/{conversation_id}/{attachment_id}/finalize",
            json={"s3_key": s3_key, "path": path},
        )
        return {
            "attachment_id": finalized.get("attachment_id") or attachment_id,
            "name": finalized.get("name") or filename,
            "media_type": finalized.get("media_type") or media_type,
            "size": finalized.get("size") or len(content),
            "path": finalized.get("path") or path,
            "s3_key": finalized.get("s3_key") or s3_key,
            "kind": "file",
        }

    def get_history(self, conversation_id: str) -> dict[str, Any]:
        return self._request_json(
            "GET", "/messages/history", params={"conversationId": conversation_id, "v": "2"}
        )

    def query_usage(self, timeout: int | None = None) -> dict[str, dict[str, str]]:
        result = self.run(USAGE_PROMPT, timeout=timeout)
        return parse_usage_response(result.text)

    def _download_attachment(self, conversation_id: str, attachment: dict[str, Any]) -> dict[str, Any]:
        download_url = attachment.get("downloadUrl")
        if not isinstance(download_url, str) or not download_url.startswith("/api/files/"):
            raise HarkGenerationError("Hark returned an invalid attachment URL")
        try:
            response = self.session.get(
                urljoin(self.cfg.upstream.rstrip("/") + "/", download_url.lstrip("/")),
                timeout=max(self.cfg.request_timeout, 60),
                allow_redirects=True,
                stream=True,
            )
        except requests.RequestException as exc:
            raise HarkNetworkError(f"attachment download failed: {type(exc).__name__}") from None
        try:
            if response.status_code >= 400:
                raise HarkError(f"attachment download failed: HTTP {response.status_code}")

            media_type = str(attachment.get("mediaType") or response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0]
            if media_type == "text/html":
                raise HarkAuthError("Hark returned a login page while downloading an attachment")
            extension = Path(str(attachment.get("name") or "")).suffix.lower()
            if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
                extension = mimetypes.guess_extension(media_type) or ".bin"
            if extension == ".jpe":
                extension = ".jpg"
            filename = f"{uuid.uuid4().hex}{extension}"
            destination = Path(self.cfg.media_dir) / filename
            temporary = destination.with_suffix(destination.suffix + ".part")
            total = 0
            try:
                with open(temporary, "wb") as fh:
                    for chunk in response.iter_content(1024 * 256):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self.cfg.max_media_bytes:
                            raise HarkGenerationError("attachment exceeds the configured size limit")
                        fh.write(chunk)
                os.replace(temporary, destination)
            except Exception:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
            return {**attachment, "filename": filename, "path": str(destination), "mediaType": media_type, "size": total}
        finally:
            response.close()

    @staticmethod
    def attachment_markdown(attachments: list[dict[str, Any]], base_url: str) -> str:
        links = []
        for attachment in attachments:
            filename = str(attachment.get("name") or attachment.get("filename") or "attachment")
            url = f"{base_url.rstrip('/')}/v1/media/{attachment['filename']}"
            if str(attachment.get("mediaType", "")).startswith("image/"):
                links.append(f"![{filename}]({url})")
            else:
                links.append(f"[{filename}]({url})")
        return "\n\n".join(links)

    def run(
        self,
        prompt: str,
        timeout: int | None = None,
        on_text: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        require_attachment: bool = False,
        input_attachment: tuple[str, str, bytes] | None = None,
    ) -> HarkResult:
        timeout = int(timeout or self.cfg.chat_timeout)
        deadline = time.monotonic() + timeout
        with self.lock:
            user_id = self.get_user_id()
            conversation_id = self.create_conversation()
            attachments = None
            if input_attachment:
                filename, media_type, content = input_attachment
                attachments = [self.upload_attachment(conversation_id, content, filename, media_type)]
            message_id = self.send_message(conversation_id, prompt, user_id, attachments)
            emitted = ""
            last_state: Any = None
            last_stage: str | None = None
            terminal_at: float | None = None
            final_messages: list[dict[str, Any]] = []

            while True:
                payload = self.get_history(conversation_id)
                messages = reply_messages(payload, message_id)
                text = reply_text(messages)
                attachments = reply_attachments(messages)
                stage = reply_stage(messages)
                if on_status and stage != last_stage:
                    on_status(stage)
                    last_stage = stage
                if on_text and text != emitted:
                    delta = text[len(emitted):] if text.startswith(emitted) else text
                    if delta:
                        on_text(delta)
                    emitted = text

                state = (
                    text,
                    tuple(str(a.get("id") or a.get("downloadUrl") or a.get("name")) for a in attachments),
                    tuple((str(m.get("id")), m.get("isStreaming"), m.get("jobStatus"), m.get("content")) for m in messages),
                )
                if reply_terminal(messages):
                    if state != last_state:
                        terminal_at = time.monotonic()
                    elif terminal_at is not None and time.monotonic() - terminal_at >= self.cfg.settle_seconds:
                        final_messages = messages
                        break
                else:
                    terminal_at = None
                last_state = state
                if time.monotonic() >= deadline:
                    raise HarkGenerationError("timed out waiting for Hark's response")
                time.sleep(max(0.05, self.cfg.poll_interval))

            if on_status:
                on_status("finalizing")
            downloaded = [self._download_attachment(conversation_id, a) for a in reply_attachments(final_messages)]
            final_text = reply_text(final_messages)
            if require_attachment and not any(str(a.get("mediaType", "")).startswith("image/") for a in downloaded):
                raise HarkGenerationError("Hark finished without returning an image attachment")
            if not final_text and not downloaded:
                raise HarkGenerationError("Hark finished without a visible response")
            return HarkResult(conversation_id, message_id, final_text, downloaded)
