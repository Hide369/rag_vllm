"""DB に保存済みのベクトルと、いま設定の vLLM が返すベクトルが一致するかを測る。

Ollama で作った DB を vLLM の埋め込みへ切り替えるとき、同じ bge-m3 でも量子化や
前処理の違いでベクトルがずれることがある。ずれていても例外は出ず、検索の距離だけが
静かに狂う。全件を入れ直す前に、サンプルで実測して判断するためのスクリプトである。

標本は ID（本文の SHA-256）の昇順で先頭から --limit 件を取る。ID は本文のハッシュ
なので実質ランダムな標本であり、乱数を使わないため同じ DB なら何度走らせても
同じ件を測る（--seed は要らない）。

使い方:
    python -m scripts.check_embedding_compat --db vector_store.sqlite3 --limit 300

終了コード: 0 = しきい値以上 / 1 = 最小値がしきい値未満 / 2 = 測れなかった
（空の DB、設定の誤り、埋め込みの失敗）。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

# 接続先（VLLM_*）は呼び出し時に読むが、.env を読まないと既定の宛先へ繋がる。
# 宛先が違えば実測の前提が崩れるため、他のプロジェクト内 import より先に読む。
load_dotenv()

from ingest import embedder, store  # noqa: E402
from ingest.vector_store import open_store  # noqa: E402
from ingest.vllm_config import (  # noqa: E402
    load_vllm_settings,
    new_session,
    reject_legacy_env,
)

DEFAULT_LIMIT = 300
# 同じモデルなら、コサイン類似度は 1 にごく近い。0.99 を割るなら検索順位が
# 入れ替わりうるので、入れ直しを検討する目安にする。
DEFAULT_THRESHOLD = 0.99


def _normalised(matrix: np.ndarray) -> np.ndarray:
    # DB 側は正規化済みで保存されている。新しく埋め込んだ側も揃えないと、
    # 長さの違いが類似度に混ざり、モデルの違いと区別できない。
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="保存済みベクトルと vLLM の埋め込みのコサイン類似度を測る"
    )
    parser.add_argument("--db", type=Path, default=store.DB_PATH, help="測る DB のパス")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="標本の件数")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="最小値がこれ未満なら終了コード 1",
    )
    args = parser.parse_args(argv)

    try:
        reject_legacy_env()
        settings = load_vllm_settings()
    except ValueError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 2

    # DB が無いパスを開くと、空の DB が黙って作られる。作らせずに止める。
    if not args.db.exists():
        print(f"エラー: DB が見つかりません: {args.db}", file=sys.stderr)
        return 2
    # 何を測ったかを必ず出す。
    print(f"対象: {args.db.resolve()}")
    print(f"vLLM 埋め込み: {settings.embed_url}（{settings.embed_model}）")
    # store.open_store ではなく生の open_store を使う。食い違いの確認で止まると、
    # 食い違いの度合いを測るこのスクリプトが、測りたい DB で動かなくなる。
    collection = open_store(str(args.db))
    try:
        samples = collection.sample_embeddings(args.limit)
    finally:
        collection.close()
    if not samples:
        print("DB が空です。測るベクトルがありません。")
        return 2

    texts = [text for text, _ in samples]
    stored = _normalised(np.stack([vector for _, vector in samples]))
    session = new_session(settings)
    try:
        fresh = _normalised(
            np.asarray(embedder.embed_texts(texts, session=session), dtype="float32")
        )
    except embedder.EmbeddingError as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 2
    finally:
        session.close()

    if fresh.shape != stored.shape:
        print(
            f"エラー: 次元が違います（保存済み {stored.shape[1]}、vLLM {fresh.shape[1]}）。",
            file=sys.stderr,
        )
        return 2

    similarities = np.sum(stored * fresh, axis=1)
    minimum = float(similarities.min())
    print(f"件数: {len(similarities)}")
    print(f"最小: {minimum:.6f}")
    print(f"平均: {float(similarities.mean()):.6f}")
    if minimum < args.threshold:
        print(f"最小値が {args.threshold} を下回りました。入れ直しを検討してください。")
        return 1
    print(f"最小値が {args.threshold} 以上です。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
