"""Small persistent store for locally queued Hark image jobs."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class TaskStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.tasks: dict[str, dict[str, Any]] = self._read()
        changed = False
        for task in self.tasks.values():
            if task.get("status") in {"queued", "processing"}:
                task.update(status="failed", error="worker interrupted by service restart")
                task["updated_at"] = int(time.time())
                changed = True
        if changed:
            self._write()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".tasks.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.tasks, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def create(self, kind: str, prompt: str, **fields: Any) -> dict[str, Any]:
        now = int(time.time())
        task = {
            "id": "task_" + uuid.uuid4().hex[:20],
            "object": "image.task",
            "kind": kind,
            "prompt": prompt,
            "status": "queued",
            "progress": 0,
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": None,
            **fields,
        }
        with self.lock:
            self.tasks[task["id"]] = task
            self._write()
        return dict(task)

    def update(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return None
            task.update(fields, updated_at=int(time.time()))
            self._write()
            return dict(task)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self.tasks.get(task_id)
            return dict(task) if task else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            tasks = sorted(self.tasks.values(), key=lambda item: item.get("created_at", 0), reverse=True)
            return [dict(task) for task in tasks[:limit]]

    def remove(self, task_id: str) -> bool:
        with self.lock:
            if task_id not in self.tasks:
                return False
            del self.tasks[task_id]
            self._write()
            return True

    def clear(self, keep: int = 0) -> int:
        with self.lock:
            ordered = sorted(self.tasks.values(), key=lambda item: item.get("created_at", 0), reverse=True)
            keep_ids = {task["id"] for task in ordered[:max(0, keep)]}
            removed = len(self.tasks) - len(keep_ids)
            self.tasks = {task_id: task for task_id, task in self.tasks.items() if task_id in keep_ids}
            self._write()
            return removed
