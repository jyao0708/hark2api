import base64
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app as module
from hark import HarkResult
from task_store import TaskStore


def wait_for_task(client, task_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(f"/v1/images/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] in {"completed", "failed"}:
            return task
        time.sleep(0.01)
    raise AssertionError("image task did not finish")


def main():
    original_client = module.client
    original_tasks = module.tasks
    original_config = module.CFG
    with tempfile.TemporaryDirectory() as directory:
        media_dir = Path(directory) / "media"
        media_dir.mkdir()
        output = media_dir / "fixture.png"
        output.write_bytes(b"fixture image")

        class FakeClient:
            def __init__(self):
                self.calls = []

            def run(self, prompt, **kwargs):
                self.calls.append((prompt, kwargs))
                return HarkResult(
                    "conversation", "message", "", [{
                        "filename": output.name,
                        "mediaType": "image/png",
                        "size": output.stat().st_size,
                    }],
                )

        fake_client = FakeClient()
        module.client = fake_client
        module.tasks = TaskStore(str(Path(directory) / "tasks.json"))
        module.CFG = SimpleNamespace(
            api_key="",
            max_media_bytes=128 * 1024 * 1024,
            media_dir=str(media_dir),
            public_base="",
            task_queue_limit=8,
        )
        try:
            with TestClient(module.app) as client:
                assert client.get("/").status_code == 200
                admin_page = client.get("/admin")
                assert "HARK" in admin_page.text and "设置" in admin_page.text and "jyao0708/hark2api" in admin_page.text
                assert client.get("/favicon.ico").status_code == 200
                qr = client.get("/docs/QQ群.png")
                assert qr.status_code == 200 and qr.headers["content-type"] == "image/png"
                response = client.post(
                    "/v1/images/generations",
                    json={"prompt": "draw", "async": True, "response_format": "b64_json"},
                )
                assert response.status_code == 202
                task = wait_for_task(client, response.json()["task_id"])
                assert task["status"] == "completed"
                assert base64.b64decode(task["data"][0]["b64_json"]) == output.read_bytes()
                calls = len(fake_client.calls)
                assert wait_for_task(client, task["id"])["status"] == "completed"
                assert len(fake_client.calls) == calls

                first = client.post(
                    "/v1/images/edits",
                    data={"prompt": "edit", "async": "true"},
                    files={"image": ("reference.png", b"aaaa", "image/png")},
                    headers={"Idempotency-Key": "reference-key"},
                )
                assert first.status_code == 202
                assert wait_for_task(client, first.json()["task_id"])["status"] == "completed"

                conflict = client.post(
                    "/v1/images/edits",
                    data={"prompt": "edit", "async": "true"},
                    files={"image": ("reference.png", b"bbbb", "image/png")},
                    headers={"Idempotency-Key": "reference-key"},
                )
                assert conflict.status_code == 409
                assert client.post("/v1/videos", json={"prompt": "unsupported"}).status_code == 404
        finally:
            module.client = original_client
            module.tasks = original_tasks
            module.CFG = original_config
    print("PASS image task routes")


if __name__ == "__main__":
    main()
