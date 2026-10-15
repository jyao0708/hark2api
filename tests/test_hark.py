import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hark import HarkClient, history_messages, parse_cookie_header, reply_attachments, reply_messages, reply_text
from app import ChatMessage, ResponsesRequest, _response_envelope, _responses_prompt, build_prompt


def test_attachment_upload():
    cfg = SimpleNamespace(
        upstream="https://hark.com", user_agent="test", locale="zh-CN",
        cookie_header="", cookie_file="", max_media_bytes=100, request_timeout=1,
    )
    client = HarkClient(cfg)
    client._request_json = Mock(side_effect=[
        {"upload_url": "https://storage.example/upload", "content_type": "image/png",
         "attachment_id": "a1", "s3_key": "k1", "path": "p1"},
        {"attachment_id": "a1", "name": "reference.png", "media_type": "image/png",
         "s3_key": "k1", "path": "p1", "size": 4},
    ])
    put = Mock(return_value=SimpleNamespace(status_code=200, close=Mock()))
    client.session.put = put

    attachment = client.upload_attachment("conversation", b"data", "dir/reference.png", "image/png")

    assert attachment == {
        "attachment_id": "a1", "name": "reference.png", "media_type": "image/png",
        "size": 4, "path": "p1", "s3_key": "k1", "kind": "file",
    }
    assert put.call_args.kwargs["headers"] == {"Content-Type": "image/png"}
    assert client._request_json.call_args_list[1].kwargs["json"] == {"s3_key": "k1", "path": "p1"}


def main():
    cookies = parse_cookie_header("a=1; __Secure-hark.session_token=x%3Dy; empty=")
    assert cookies == {"a": "1", "__Secure-hark.session_token": "x%3Dy", "empty": ""}

    user_id = "user-1"
    payload = {
        "shape": "records",
        "log": [{"id": user_id, "kind": "message"}, {"id": "answer", "kind": "message"}],
        "messages": {
            user_id: {"id": user_id, "role": "user", "content": "draw"},
            "answer": {"id": "answer", "role": "assistant", "source": "send_bubble",
                       "triggeredByMessageId": user_id, "content": "done", "isStreaming": False,
                       "attachments": [{"id": "file-1", "mediaType": "image/png", "name": "x.png"}]},
        },
    }
    messages = history_messages(payload)
    replies = reply_messages(payload, user_id)
    assert [item["id"] for item in messages] == [user_id, "answer"]
    assert reply_text(replies) == "done"
    assert reply_attachments(replies)[0]["name"] == "x.png"

    prompt = build_prompt([ChatMessage(role="system", content="be concise"), ChatMessage(role="user", content="hello")])
    assert "SYSTEM:" in prompt and "USER:" in prompt and prompt.endswith("ASSISTANT:")
    assert build_prompt([ChatMessage(role="user", content="hello")]) == "hello"
    response_prompt = _responses_prompt(ResponsesRequest(input=[{"type": "input_text", "text": "hello"}]))
    assert response_prompt == "hello"
    assert _response_envelope("r", "m", "hark", "completed", "ok")["output_text"] == "ok"
    test_attachment_upload()
    print("PASS hark helpers")


if __name__ == "__main__":
    main()
