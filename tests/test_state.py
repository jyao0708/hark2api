import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from account_pool import HarkAccountPool, AccountStore
from config import Config
from hark import HarkAuthError, HarkError, HarkResult
from task_store import TaskStore


def main():
    with tempfile.TemporaryDirectory() as directory:
        account_path = os.path.join(directory, "accounts.json")
        accounts = AccountStore(account_path)
        first = accounts.add("first", "a=1; b=2")
        second = accounts.add("second", "a=3")
        assert "cookie_header" not in first
        assert "cookie_count" not in first
        assert "cookie_header" not in accounts.list_public()[1]
        assert os.stat(account_path).st_mode & 0o077 == 0

        cfg = SimpleNamespace(accounts_file=account_path, cookie_header="", cookie_file="")
        pool = HarkAccountPool(cfg)

        class FailingClient:
            def run(self, *args, **kwargs):
                raise HarkAuthError("expired")

        class WorkingClient:
            def run(self, *args, **kwargs):
                return HarkResult("c", "m", "ok", [])

            def query_usage(self, *args, **kwargs):
                return {
                    "daily": {"left": "95%", "reset": "2026-10-16 11:07"},
                    "monthly": {"left": "99%", "reset": "2026-11-01 08:00"},
                }

        pool.clients[first["id"]] = FailingClient()
        pool.clients[second["id"]] = WorkingClient()
        assert pool.run("hello").text == "ok"
        assert AccountStore(account_path).get(first["id"])["ok"] is False
        account = pool.refresh_usage(second["id"])
        assert account["usage"]["status"] == "ready"
        assert account["usage"]["daily"]["left"] == "95%"

        class ProfileClient:
            def get_profile(self):
                return {"email": "person@example.com", "name": "", "nickname": "person"}

            def query_usage(self, *args, **kwargs):
                return {"plan": "hark pro", "daily": {"left": "90%"}, "monthly": {"left": "99%"}}

        pool._client = lambda _account: ProfileClient()
        onboarded = pool.add_account("", "token")
        assert onboarded["profile"]["email"] == "person@example.com"
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            saved = AccountStore(account_path).get(onboarded["id"])
            if (saved.get("usage") or {}).get("status") == "ready":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("background usage refresh did not finish")

        class InvalidProfileClient:
            def get_profile(self):
                raise HarkAuthError("expired")

        pool._client = lambda _account: InvalidProfileClient()
        try:
            pool.add_account("invalid", "expired-token")
        except HarkAuthError:
            pass
        else:
            raise AssertionError("invalid profile was accepted")
        assert not any(item.get("label") == "invalid" for item in AccountStore(account_path).list_internal())

        class MissingUsageClient:
            def get_profile(self):
                return {"email": "quota-later@example.com", "name": "", "nickname": ""}

            def query_usage(self, *args, **kwargs):
                raise HarkError("billing unavailable")

        pool._client = lambda _account: MissingUsageClient()
        quota_later = pool.add_account("quota-later", "quota-token")
        assert quota_later["ok"] is True
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            saved = AccountStore(account_path).get(quota_later["id"])
            if (saved.get("usage") or {}).get("status") == "error":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("background usage failure was not recorded")

        task_path = os.path.join(directory, "tasks.json")
        task_store = TaskStore(task_path)
        task = task_store.create("image", "draw", response_format="url")
        task_store.update(task["id"], status="processing")
        recovered = TaskStore(task_path).get(task["id"])
        assert recovered["status"] == "failed"
        assert recovered["progress"] is None and recovered["phase"] == "失败"
        assert "interrupted" in recovered["error"]

        cookie_file = os.path.join(directory, "cookie.txt")
        Path(cookie_file).write_text("first=one; second=two", encoding="utf-8")
        file_pool = HarkAccountPool(Config(
            base_dir=directory,
            cookie_file=cookie_file,
            request_timeout=1,
            chat_timeout=1,
            poll_interval=0.05,
            settle_seconds=0,
        ))
        assert file_pool.cookie_count() == 2
    print("PASS state helpers")


if __name__ == "__main__":
    main()
