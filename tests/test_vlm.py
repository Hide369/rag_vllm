import base64

import pytest
import requests

import ingest.vlm as vlm
from ingest.vlm import CAPTION_PROMPT, VlmError, caption_image

PNG = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A]) + b"rest"
JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"rest"


class _FakeResponse:
    def __init__(self, payload, status=200, text=""):
        self.text = text
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _FakeSession:
    """POSTされたペイロードとURLを記録し、あらかじめ決めた応答を返す。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.payloads = []
        self.urls = []

    def post(self, url, json, timeout):
        self.urls.append(url)
        self.payloads.append(json)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, timeout):
        self.urls.append(url)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _reply(text):
    return _FakeResponse({"choices": [{"message": {"content": text}}]})


def _models(*ids):
    return _FakeResponse({"data": [{"id": model_id} for model_id in ids]})


def test_sends_the_image_as_a_data_url_to_the_configured_model(monkeypatch):
    monkeypatch.setenv("VLLM_VLM_URL", "http://vlm.example:9100/")
    monkeypatch.setenv("VLLM_VLM_MODEL", "my-vlm")
    session = _FakeSession([_reply("赤い四角形の画像です。")])
    result = caption_image(PNG, session=session)
    assert result == "赤い四角形の画像です。"
    assert session.urls == ["http://vlm.example:9100/v1/chat/completions"]
    payload = session.payloads[0]
    assert payload["model"] == "my-vlm"
    encoded = base64.b64encode(PNG).decode("ascii")
    assert payload["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": CAPTION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ],
        }
    ]


@pytest.mark.parametrize(
    ("image", "mime"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (b"GIF89a....", "image/png"), (b"x", "image/png")],
)
def test_mime_type_is_sniffed_from_the_leading_bytes(image, mime):
    session = _FakeSession([_reply("説明")])
    caption_image(image, session=session)
    url = session.payloads[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith(f"data:{mime};base64,")


def test_strips_surrounding_whitespace():
    session = _FakeSession([_reply("  説明文  \n")])
    assert caption_image(b"x", session=session) == "説明文"


def test_a_reply_without_content_fails_fast(monkeypatch):
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: pytest.fail("再試行した"))
    reply = _FakeResponse({"choices": [{"message": {"content": None}, "finish_reason": "length"}]})
    session = _FakeSession([reply])
    with pytest.raises(VlmError, match="finish_reason=length"):
        caption_image(b"x", session=session)
    assert len(session.payloads) == 1


def test_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)
    session = _FakeSession([requests.ConnectionError("boom"), _reply("説明")])
    assert caption_image(b"x", session=session) == "説明"


def test_backoff_is_exponential(monkeypatch):
    waits = []
    monkeypatch.setattr(vlm.time, "sleep", waits.append)
    session = _FakeSession(
        [requests.ConnectionError("x"), requests.ConnectionError("x"), _reply("説明")]
    )
    caption_image(b"x", session=session)
    assert waits == [1, 2]


def test_gives_up_after_all_attempts(monkeypatch):
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)
    session = _FakeSession([requests.ConnectionError("x")] * 4)
    with pytest.raises(VlmError):
        caption_image(b"x", session=session)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_is_not_retried(monkeypatch, status):
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: pytest.fail("再試行した"))
    session = _FakeSession([_FakeResponse({}, status=status)])
    with pytest.raises(VlmError, match="VLLM_API_KEY") as caught:
        caption_image(b"x", session=session)
    assert len(session.payloads) == 1
    assert caught.value.__cause__ is not None


def test_check_vlm_raises_when_unreachable():
    session = _FakeSession([requests.ConnectionError("refused")])
    with pytest.raises(
        VlmError, match=r"vLLM（画像説明）に接続できません（http://127\.0\.0\.1:8003）"
    ):
        vlm.check_vlm(session=session)


def test_check_vlm_passes_when_model_present():
    session = _FakeSession([_models("qwen3-vl-30b-a3b")])
    vlm.check_vlm(session=session)
    assert session.urls == ["http://127.0.0.1:8003/v1/models"]


def test_check_vlm_names_the_variable_and_the_served_models_when_missing():
    session = _FakeSession([_models("other-vlm")])
    with pytest.raises(VlmError) as caught:
        vlm.check_vlm(session=session)
    message = str(caught.value)
    assert "VLLM_VLM_MODEL" in message
    assert "other-vlm" in message


def test_check_vlm_reports_an_auth_rejection():
    session = _FakeSession([_FakeResponse({}, status=403)])
    with pytest.raises(VlmError, match="VLLM_API_KEY"):
        vlm.check_vlm(session=session)


def test_client_error_is_not_retried_and_shows_the_server_reason(monkeypatch):
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: pytest.fail("再試行した"))
    session = _FakeSession([_FakeResponse({}, status=404, text="The model `x` does not exist.")])
    with pytest.raises(VlmError) as caught:
        caption_image(b"x", session=session)
    assert "404" in str(caught.value)
    assert "The model `x` does not exist." in str(caught.value)
    assert len(session.payloads) == 1


def test_server_error_is_still_retried(monkeypatch):
    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)
    session = _FakeSession([_FakeResponse({}, status=500, text="boom"), _reply("説明")])
    assert caption_image(b"x", session=session) == "説明"
    assert len(session.payloads) == 2
