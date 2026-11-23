"""新しい Web 画面を起動する。

使い方:
    python -m rag_web
接続先・ポートは .env の RAG_WEB_HOST / RAG_WEB_PORT（既定 127.0.0.1:8502）。
プロキシの背後で動かす設定は RAG_WEB_FORWARDED_ALLOW_IPS（.env.example を参照）。
"""
import sys

import uvicorn
from dotenv import load_dotenv

from ingest.vllm_config import load_vllm_settings, reject_legacy_env

from .app import create_app
from .auth import validate_password_hash
from .settings import load_web_settings


def main() -> int:
    # 接続設定は呼ぶたびに環境変数から読む（ingest.vllm_config）ので、import の順序には
    # 依存しない。ただし設定を読む前（create_app より前）に .env を読み込んでおく。
    load_dotenv()

    try:
        # 古い OLLAMA_* が残ったまま起動すると、利用者は設定が効いていると思い込む。
        # 画面を立ち上げる前に、移行が要ることを日本語で伝えて止める。
        reject_legacy_env()
        vllm_settings = load_vllm_settings()
        settings = load_web_settings()
        validate_password_hash(settings.admin_password_hash)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2

    app = create_app(settings, chat_model=vllm_settings.chat_model)
    # Nginx の背後では接続元が常にプロキシになり、ログインの試行回数制限が全利用者で
    # 共有されてしまう。信頼するアドレスからの X-Forwarded-For だけを接続元にする。
    # 空なら、偽のヘッダーで制限を逃れられないよう一切信頼しない。
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=bool(settings.forwarded_allow_ips),
        forwarded_allow_ips=settings.forwarded_allow_ips or None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
