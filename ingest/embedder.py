"""vLLM の OpenAI 互換埋め込みAPI（/v1/embeddings）を叩く。

接続先に localhost を使ってはならない。Windowsでは localhost が先にIPv6の ::1 に
解決され、IPv4でしか待ち受けないサーバーではタイムアウト待ちが発生する。これは
Ollama で実測した事象（localhost + 毎回新規接続 2,151ms / 127.0.0.1 79ms /
Session再利用 77ms）だが、解決順はサーバーに依らず OS 側の挙動なので vLLM でも
同じ理由で 127.0.0.1 を既定にしている（ingest/vllm_config.py）。
"""
import time

import requests

from ingest.vllm_config import (
    AuthError,
    list_model_ids,
    load_vllm_settings,
    new_session,
    raise_for_auth,
)

EMBED_DIM = 1024

# 仮の既定値。Ollama（16GBのメモリ）では batch=8 が batch=32 より速かったが、
# vLLM + GB10 では前提が違う。GB10 で計測して決め直す（設計書「計測前の既定値」）。
EMBED_BATCH_SIZE = 8

_MAX_ATTEMPTS = 4  # 初回 + 3回の再試行（1秒 → 2秒 → 4秒）
_TIMEOUT = 600


class EmbeddingError(Exception):
    """埋め込みの取得に失敗した。"""


def _vectors_in_input_order(data: list[dict], expected_count: int) -> list[list[float]]:
    # data[] の並びは保証されない。index で並べ直さないと、本文とベクトルが
    # ずれたまま DB に入り、検索が黙って外れる。
    if len(data) != expected_count:
        raise EmbeddingError(
            f"埋め込みの件数が合いません（要求 {expected_count} 件、応答 {len(data)} 件）。"
        )
    return [item["embedding"] for item in sorted(data, key=lambda item: item["index"])]


def _post_batch(session, texts: list[str]) -> list[list[float]]:
    settings = load_vllm_settings()
    url = f"{settings.embed_url}/v1/embeddings"
    payload = {"model": settings.embed_model, "input": texts}
    last_error = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = session.post(url, json=payload, timeout=_TIMEOUT)
            # 401/403 は何度送っても通らないので、再試行の対象にしない。
            raise_for_auth(response, url)
            response.raise_for_status()
            vectors = _vectors_in_input_order(response.json()["data"], len(texts))
        except AuthError as error:
            raise EmbeddingError(str(error)) from error
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            last_error = error
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2**attempt)
            continue
        # モデルを取り違えると次元が変わる。DBに書き込む前にここで止める。
        if any(len(vector) != EMBED_DIM for vector in vectors):
            raise EmbeddingError(
                f"埋め込みの次元が想定と違います（期待 {EMBED_DIM}）。"
                f"モデル {settings.embed_model} が正しいか確認してください。"
            )
        return vectors
    raise EmbeddingError(
        f"{settings.embed_url} への埋め込みリクエストが{_MAX_ATTEMPTS}回失敗しました: {last_error}"
    )


def embed_texts(texts: list[str], session=None) -> list[list[float]]:
    """テキスト列をバッチに分けて埋め込む。"""
    if not texts:
        return []
    own_session = session is None
    session = session or new_session()
    try:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            vectors.extend(_post_batch(session, texts[start : start + EMBED_BATCH_SIZE]))
        return vectors
    finally:
        if own_session:
            session.close()


def embed_query(text: str, session=None) -> list[float]:
    return embed_texts([text], session=session)[0]


def check_embedder(session=None) -> None:
    """取り込み開始前の疎通確認。

    460チャンクの処理を始めてから落ちるのを防ぐため、先に一度だけ確認する。
    """
    settings = load_vllm_settings()
    own_session = session is None
    session = session or new_session()
    try:
        try:
            served = list_model_ids(session, settings.embed_url)
        except AuthError as error:
            raise EmbeddingError(str(error)) from error
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            raise EmbeddingError(
                f"vLLM（埋め込み）に接続できません（{settings.embed_url}）。"
                f"起動しているか確認してください: {error}"
            ) from error
        if settings.embed_model not in served:
            raise EmbeddingError(
                f"埋め込みモデル {settings.embed_model} がサーバーにありません。"
                f"VLLM_EMBED_MODEL を確かめてください（サーバーにあるモデル: {', '.join(served) or 'なし'}）。"
            )
    finally:
        if own_session:
            session.close()
