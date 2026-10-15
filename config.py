"""Runtime configuration for hark2api."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv() -> None:
    path = os.path.join(os.environ.get("HARK2API_HOME", BASE_DIR), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(slots=True)
class Config:
    base_dir: str = field(default_factory=lambda: _env("HARK2API_HOME", BASE_DIR))
    host: str = field(default_factory=lambda: _env("HARK2API_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("HARK2API_PORT", "18611")))
    upstream: str = field(default_factory=lambda: _env("HARK2API_UPSTREAM", "https://hark.com"))
    cookie_header: str = field(default_factory=lambda: _env("HARK2API_COOKIE", ""))
    cookie_file: str = field(default_factory=lambda: _env("HARK2API_COOKIE_FILE", ""))
    locale: str = field(default_factory=lambda: _env("HARK2API_LOCALE", "zh-CN"))
    timezone: str = field(default_factory=lambda: _env("HARK2API_TIMEZONE", "Asia/Shanghai"))
    user_agent: str = field(default_factory=lambda: _env(
        "HARK2API_USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    ))
    api_key: str = field(default_factory=lambda: _env("HARK2API_KEY", ""))
    public_base: str = field(default_factory=lambda: _env("HARK2API_PUBLIC_BASE", ""))
    request_timeout: int = field(default_factory=lambda: int(_env("HARK2API_REQUEST_TIMEOUT", "30")))
    chat_timeout: int = field(default_factory=lambda: int(_env("HARK2API_CHAT_TIMEOUT", "300")))
    poll_interval: float = field(default_factory=lambda: float(_env("HARK2API_POLL_INTERVAL", "1")))
    settle_seconds: float = field(default_factory=lambda: float(_env("HARK2API_SETTLE_SECONDS", "1.5")))
    max_media_bytes: int = field(default_factory=lambda: int(_env("HARK2API_MAX_MEDIA_BYTES", str(128 * 1024 * 1024))))
    task_queue_limit: int = field(default_factory=lambda: int(_env("HARK2API_TASK_QUEUE_LIMIT", "8")))

    @property
    def data_dir(self) -> str:
        return os.path.join(self.base_dir, "data")

    @property
    def media_dir(self) -> str:
        return os.path.join(self.data_dir, "media")

    @property
    def accounts_file(self) -> str:
        return _env("HARK2API_ACCOUNTS_FILE", os.path.join(self.data_dir, "accounts.json"))

    @property
    def tasks_file(self) -> str:
        return _env("HARK2API_TASKS_FILE", os.path.join(self.data_dir, "tasks.json"))

    def ensure_dirs(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.media_dir, exist_ok=True)


CFG = Config()
