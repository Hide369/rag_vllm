"""新しい Web 画面を起動する。

使い方:
    python -m rag_web
接続先・ポートは .env の RAG_WEB_HOST / RAG_WEB_PORT（既定 127.0.0.1:8502）。
プロキシの背後で動かす設定は RAG_WEB_FORWARDED_ALLOW_IPS（.env.example を参照）。
"""
import sys

import uvicorn
from dotenv import load_dotenv


def main() -> int:
    # OLLAMA_HOST を ingest.embedder がインポート時に読むため、ingest を取り込む
    # rag_web.app などより先に .env を読み込む必要がある（ingest 側の設定は import 時に決まる）。
    # そのため .settings / .auth / .app はここで遅延インポートする。
    load_dotenv()

    from .app import create_app
    from .auth import validate_password_hash
    from .settings import load_web_settings

    try:
        settings = load_web_settings()
        validate_password_hash(settings.admin_password_hash)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2

    app = create_app(settings)
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
