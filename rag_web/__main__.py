"""新しい Web 画面を起動する。

使い方:
    python -m rag_web
接続先・ポートは .env の RAG_WEB_HOST / RAG_WEB_PORT（既定 127.0.0.1:8502）。
"""
import sys

import uvicorn
from dotenv import load_dotenv


def main() -> int:
    # OLLAMA_HOST を ingest.embedder がインポート時に読むため、ingest を取り込む
    # rag_web.app などより先に .env を読み込む必要がある（rag_chat_app.py 冒頭と同じ理由）。
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
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
