"""Persistent Hark account selection with authentication-failure failover."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from hark import HarkAuthError, HarkClient, HarkError, HarkResult, parse_cookie_header


def _cookie_header(account: dict[str, Any]) -> str:
    header = account.get("cookie_header")
    if isinstance(header, str) and header.strip():
        return header.strip()
    cookies = account.get("cookies")
    if isinstance(cookies, dict):
        return "; ".join(f"{name}={value}" for name, value in cookies.items())
    return ""


def _has_credential(account: dict[str, Any]) -> bool:
    return bool(_cookie_header(account) or account.get("cookie_file"))


class AccountStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.accounts: list[dict[str, Any]] = self._read()

    def _read(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(value, dict):
            value = value.get("accounts", [])
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".accounts.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.accounts, handle, ensure_ascii=False, indent=2)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _public(account: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in account.items() if key not in {"cookie_header", "cookie_file", "cookies"}}
        result["cookie_count"] = len(parse_cookie_header(_cookie_header(account)))
        result["cookie_configured"] = _has_credential(account)
        return result

    def has_persisted_accounts(self) -> bool:
        with self.lock:
            return bool(self.accounts)

    def list_public(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._public(account) for account in self.accounts]

    def list_internal(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(account) for account in self.accounts]

    def get(self, account_id: str) -> dict[str, Any] | None:
        with self.lock:
            for account in self.accounts:
                if str(account.get("id")) == account_id:
                    return dict(account)
        return None

    def add(self, label: str, cookie_header: str) -> dict[str, Any]:
        if not cookie_header.strip():
            raise ValueError("cookie_header must not be empty")
        now = int(time.time())
        account = {
            "id": "acc_" + os.urandom(6).hex(),
            "label": label.strip() or "Hark account",
            "cookie_header": cookie_header.strip(),
            "enabled": True,
            "ok": None,
            "note": "",
            "created_at": now,
            "updated_at": now,
            "last_used": None,
            "use_count": 0,
        }
        with self.lock:
            self.accounts.append(account)
            self._write()
        return self._public(account)

    def update(self, account_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"label", "cookie_header", "enabled"}
        with self.lock:
            for account in self.accounts:
                if str(account.get("id")) != account_id:
                    continue
                for key, value in fields.items():
                    if key in allowed and value is not None:
                        if key == "cookie_header" and not str(value).strip():
                            raise ValueError("cookie_header must not be empty")
                        account[key] = value.strip() if isinstance(value, str) else value
                account["updated_at"] = int(time.time())
                account["ok"] = None if "cookie_header" in fields else account.get("ok")
                self._write()
                return self._public(account)
        return None

    def mark(self, account_id: str, ok: bool, note: str = "") -> None:
        with self.lock:
            for account in self.accounts:
                if str(account.get("id")) == account_id:
                    account["ok"] = ok
                    account["note"] = note[:300]
                    account["checked_at"] = int(time.time())
                    self._write()
                    return

    def used(self, account_id: str) -> None:
        with self.lock:
            for account in self.accounts:
                if str(account.get("id")) == account_id:
                    account["last_used"] = time.time()
                    account["use_count"] = int(account.get("use_count") or 0) + 1
                    self._write()
                    return

    def set_usage(self, account_id: str, usage: dict[str, Any]) -> None:
        with self.lock:
            for account in self.accounts:
                if str(account.get("id")) == account_id:
                    account["usage"] = usage
                    self._write()
                    return

    def delete(self, account_id: str) -> bool:
        with self.lock:
            original = len(self.accounts)
            self.accounts = [account for account in self.accounts if str(account.get("id")) != account_id]
            if len(self.accounts) == original:
                return False
            self._write()
            return True

    def stats(self) -> dict[str, int]:
        with self.lock:
            enabled = [account for account in self.accounts if account.get("enabled", True)]
            return {
                "total": len(self.accounts),
                "enabled": len(enabled),
                "disabled": len(self.accounts) - len(enabled),
                "healthy": sum(account.get("ok") is True for account in enabled),
                "error": sum(account.get("ok") is False for account in enabled),
            }


class HarkAccountPool:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = AccountStore(cfg.accounts_file)
        self.lock = threading.RLock()
        self.clients: dict[str, HarkClient] = {}
        self.virtual_status: dict[str, Any] = {
            "ok": None,
            "note": "",
            "use_count": 0,
            "last_used": None,
            "usage": None,
        }

    def _records(self) -> list[dict[str, Any]]:
        persisted = self.store.list_internal()
        if persisted:
            return persisted
        if self.cfg.cookie_header or self.cfg.cookie_file:
            return [{
                "id": "env",
                "label": "environment cookie",
                "cookie_header": self.cfg.cookie_header,
                "cookie_file": self.cfg.cookie_file,
                "enabled": True,
                **self.virtual_status,
            }]
        return []

    def _client(self, account: dict[str, Any]) -> HarkClient:
        account_id = str(account["id"])
        with self.lock:
            client = self.clients.get(account_id)
            if client is None:
                account_cfg = replace(
                    self.cfg,
                    cookie_header=_cookie_header(account),
                    cookie_file=str(account.get("cookie_file") or ""),
                )
                client = HarkClient(account_cfg)
                self.clients[account_id] = client
            return client

    def _ordered(self) -> list[dict[str, Any]]:
        records = [record for record in self._records() if record.get("enabled", True) and _has_credential(record)]
        healthy = [record for record in records if record.get("ok") is not False]
        return sorted(healthy or records, key=lambda record: (record.get("last_used") or 0, record.get("use_count") or 0))

    def run(
        self,
        prompt: str,
        timeout: int | None = None,
        on_text: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        require_attachment: bool = False,
        input_attachment: tuple[str, str, bytes] | None = None,
    ) -> HarkResult:
        candidates = self._ordered()
        if not candidates:
            raise HarkAuthError("no Hark account is configured")
        last_error: HarkError | None = None
        for account in candidates:
            account_id = str(account["id"])
            try:
                result = self._client(account).run(
                    prompt,
                    timeout=timeout,
                    on_text=on_text,
                    on_status=on_status,
                    require_attachment=require_attachment,
                    input_attachment=input_attachment,
                )
                result.account_id = account_id
                self._mark_success(account)
                return result
            except HarkAuthError as exc:
                last_error = exc
                self._mark_failure(account, str(exc))
                continue
            except HarkError:
                raise
        raise last_error or HarkAuthError("no Hark account is available")

    def _mark_success(self, account: dict[str, Any]) -> None:
        account_id = str(account["id"])
        if account_id == "env":
            self.virtual_status.update(ok=True, note="", last_used=time.time(), use_count=self.virtual_status["use_count"] + 1)
        else:
            self.store.mark(account_id, True)
            self.store.used(account_id)

    def _mark_failure(self, account: dict[str, Any], note: str) -> None:
        account_id = str(account["id"])
        if account_id == "env":
            self.virtual_status.update(ok=False, note=note[:300])
        else:
            self.store.mark(account_id, False, note)

    def cookie_count(self) -> int:
        count = 0
        for account in self._records():
            try:
                count += self._client(account).cookie_count()
            except HarkError:
                continue
        return count

    def list_accounts(self) -> list[dict[str, Any]]:
        persisted = self.store.list_public()
        if persisted:
            return persisted
        records = self._records()
        return [{
            "id": "env",
            "label": "environment cookie",
            "enabled": True,
            "ok": self.virtual_status["ok"],
            "note": self.virtual_status["note"],
            "use_count": self.virtual_status["use_count"],
            "last_used": self.virtual_status["last_used"],
            "usage": self.virtual_status["usage"],
            "cookie_count": self.cookie_count(),
            "cookie_configured": bool(records),
            "persistent": False,
        }] if records else []

    def stats(self) -> dict[str, Any]:
        stats = self.store.stats()
        if not self.store.has_persisted_accounts():
            stats = {
                "total": 1 if self._records() else 0,
                "enabled": 1 if self._records() else 0,
                "disabled": 0,
                "healthy": int(self.virtual_status["ok"] is True),
                "error": int(self.virtual_status["ok"] is False),
            }
        stats["cookie_count"] = self.cookie_count()
        return stats

    def add_account(self, label: str, cookie_header: str) -> dict[str, Any]:
        account = self.store.add(label, cookie_header)
        self.clients.pop(str(account["id"]), None)
        return account

    def update_account(self, account_id: str, **fields: Any) -> dict[str, Any] | None:
        result = self.store.update(account_id, **fields)
        if result and ("cookie_header" in fields or "enabled" in fields):
            self.clients.pop(account_id, None)
        return result

    def delete_account(self, account_id: str) -> bool:
        self.clients.pop(account_id, None)
        return self.store.delete(account_id)

    def test_account(self, account_id: str) -> dict[str, Any]:
        account = next((item for item in self._records() if str(item.get("id")) == account_id), None)
        if account is None:
            raise KeyError(account_id)
        try:
            user_id = self._client(account).get_user_id()
            self._mark_success(account)
            return {"ok": True, "account_id": account_id, "user_id_present": bool(user_id)}
        except HarkAuthError as exc:
            self._mark_failure(account, str(exc))
            return {"ok": False, "account_id": account_id, "error": str(exc)}

    def set_usage_pending(self, account_id: str) -> None:
        self._set_usage(account_id, {
            "status": "loading",
            "updated_at": int(time.time()),
        })

    def refresh_usage(self, account_id: str, timeout: int | None = None) -> dict[str, Any]:
        account = next((item for item in self._records() if str(item.get("id")) == account_id), None)
        if account is None:
            raise KeyError(account_id)
        try:
            usage = self._client(account).query_usage(timeout=timeout)
            record = {"status": "ready", **usage, "updated_at": int(time.time())}
            self._set_usage(account_id, record)
            if account_id == "env":
                self.virtual_status.update(ok=True, note="")
            else:
                self.store.mark(account_id, True)
        except HarkAuthError as exc:
            self._mark_failure(account, str(exc))
            self._set_usage(account_id, {
                "status": "error",
                "error": str(exc),
                "updated_at": int(time.time()),
            })
        except HarkError as exc:
            self._set_usage(account_id, {
                "status": "error",
                "error": str(exc),
                "updated_at": int(time.time()),
            })
        current = next((item for item in self.list_accounts() if str(item.get("id")) == account_id), None)
        return current or {}

    def _set_usage(self, account_id: str, usage: dict[str, Any]) -> None:
        if account_id == "env":
            self.virtual_status["usage"] = usage
        else:
            self.store.set_usage(account_id, usage)

    @staticmethod
    def attachment_markdown(attachments: list[dict[str, Any]], base_url: str) -> str:
        return HarkClient.attachment_markdown(attachments, base_url)
