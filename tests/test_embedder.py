import pytest
import requests

import ingest.embedder as embedder
from ingest.embedder import (
    EMBED_BATCH_SIZE,
    EMBED_DIM,
    EmbeddingError,
    check_embedder,
    embed_query,
    embed_texts,
)
from ingest.vllm_config import load_vllm_settings


class _FakeResponse:
    def __init__(self, payload, status=200):
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


def _vectors(count):
    return _FakeResponse(
        {"data": [{"index": i, "embedding": [0.1] * EMBED_DIM} for i in range(count)]}
    )


def test_posts_to_the_embeddings_endpoint_with_the_configured_model(monkeypatch):
    monkeypatch.setenv("VLLM_EMBED_URL", "http://embed.example:9000/")
    monkeypatch.setenv("VLLM_EMBED_MODEL", "my-embed")
    session = _FakeSession([_vectors(2)])
    embed_texts(["a", "b"], session=session)
    assert session.urls == ["http://embed.example:9000/v1/embeddings"]
    assert session.payloads[0] == {"model": "my-embed", "input": ["a", "b"]}


def test_uses_bge_m3_by_default():
    session = _FakeSession([_vectors(1)])
    embed_texts(["本文"], session=session)
    assert session.payloads[0]["model"] == "bge-m3"


def test_embedding_dimension_is_1024():
    assert EMBED_DIM == 1024


def test_vectors_are_returned_in_input_order_even_if_the_server_reverses_them():
    """data[] の並びは保証されない。index で並べ直さないと、本文とベクトルが
    ずれたまま DB に入り、検索が黙って外れる。"""
    first = [1.0] + [0.0] * (EMBED_DIM - 1)
    second = [0.0, 1.0] + [0.0] * (EMBED_DIM - 2)
    reversed_reply = _FakeResponse(
        {"data": [{"index": 1, "embedding": second}, {"index": 0, "embedding": first}]}
    )
    session = _FakeSession([reversed_reply])
    assert embed_texts(["a", "b"], session=session) == [first, second]


def test_a_reply_with_the_wrong_number_of_vectors_is_rejected():
    session = _FakeSession([_vectors(1)])
    with pytest.raises(EmbeddingError):
        embed_texts(["a", "b"], session=session)


def test_batches_are_capped_at_eight():
    """仮の既定値。GB10 で計測して決め直す（設計書「計測前の既定値」）。"""
    assert EMBED_BATCH_SIZE == 8
    session = _FakeSession([_vectors(8), _vectors(8), _vectors(4)])
    result = embed_texts([f"本文{i}" for i in range(20)], session=session)
    assert [len(p["input"]) for p in session.payloads] == [8, 8, 4]
    assert len(result) == 20


def test_empty_input_makes_no_request():
    session = _FakeSession([])
    assert embed_texts([], session=session) == []
    assert session.payloads == []


def test_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(embedder.time, "sleep", lambda _s: None)
    session = _FakeSession([requests.ConnectionError("boom"), _vectors(1)])
    assert len(embed_texts(["本文"], session=session)) == 1


def test_backoff_is_exponential(monkeypatch):
    waits = []
    monkeypatch.setattr(embedder.time, "sleep", waits.append)
    session = _FakeSession(
        [requests.ConnectionError("x"), requests.ConnectionError("x"), _vectors(1)]
    )
    embed_texts(["本文"], session=session)
    assert waits == [1, 2]


def test_gives_up_after_all_attempts(monkeypatch):
    monkeypatch.setattr(embedder.time, "sleep", lambda _s: None)
    session = _FakeSession([requests.ConnectionError("x")] * 4)
    with pytest.raises(EmbeddingError):
        embed_texts(["本文"], session=session)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_rejection_is_not_retried(monkeypatch, status):
    """鍵が違えば何度送っても通らない。待たずに、鍵を疑える文面で止める。"""
    monkeypatch.setattr(embedder.time, "sleep", lambda _s: pytest.fail("再試行した"))
    session = _FakeSession([_FakeResponse({}, status=status)])
    with pytest.raises(EmbeddingError, match="VLLM_API_KEY") as caught:
        embed_texts(["本文"], session=session)
    assert len(session.payloads) == 1
    assert caught.value.__cause__ is not None


def test_wrong_dimension_is_rejected():
    """モデルを取り違えると次元が変わり、DBに入れてから壊れていることに気付く。"""
    session = _FakeSession(
        [_FakeResponse({"data": [{"index": 0, "embedding": [0.1] * 768}]})]
    )
    with pytest.raises(EmbeddingError):
        embed_texts(["本文"], session=session)


def test_embed_query_returns_a_single_vector():
    session = _FakeSession([_vectors(1)])
    assert len(embed_query("質問", session=session)) == EMBED_DIM


def test_the_default_embed_url_never_uses_localhost():
    """Windowsでは localhost が先にIPv6の ::1 に解決され、IPv4でしか待ち受けない
    サーバーではタイムアウト待ちが発生する。既定値は 127.0.0.1 のままにする。"""
    url = load_vllm_settings({}).embed_url
    assert "localhost" not in url
    assert "127.0.0.1" in url


def test_check_embedder_passes_when_the_model_is_served():
    session = _FakeSession([_FakeResponse({"data": [{"id": "bge-m3"}]})])
    check_embedder(session=session)
    assert session.urls == ["http://127.0.0.1:8002/v1/models"]


def test_check_embedder_names_the_variable_and_the_served_models_when_missing():
    session = _FakeSession([_FakeResponse({"data": [{"id": "other-model"}]})])
    with pytest.raises(EmbeddingError) as caught:
        check_embedder(session=session)
    message = str(caught.value)
    assert "VLLM_EMBED_MODEL" in message
    assert "other-model" in message
    assert "bge-m3" in message


def test_check_embedder_raises_when_unreachable():
    session = _FakeSession([requests.ConnectionError("refused")])
    with pytest.raises(
        EmbeddingError, match=r"vLLM（埋め込み）に接続できません（http://127\.0\.0\.1:8002）"
    ):
        check_embedder(session=session)


def test_check_embedder_reports_an_auth_rejection():
    session = _FakeSession([_FakeResponse({}, status=401)])
    with pytest.raises(EmbeddingError, match="VLLM_API_KEY"):
        check_embedder(session=session)
