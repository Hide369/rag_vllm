"""Web UI の設定を環境変数から読む。

不正な値は黙って既定値に倒さず、変数名入りの ValueError で起動時に止める。
パスワードハッシュの形式検証は auth 側（validate_password_hash）の責務で、
ここでは空でないことだけを確かめる。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

_TRUE = {"true", "1", "yes"}
_FALSE = {"false", "0", "no"}
_MAX_PORT = 65535


@dataclass(frozen=True)
class WebSettings:
    admin_password_hash: str
    host: str = "127.0.0.1"
    port: int = 8502
    max_upload_bytes: int = 50 * 1024 * 1024
    session_ttl_s: int = 12 * 3600
    cookie_secure: bool = False
    # リバースプロキシ（Nginx）のアドレス。ここからの X-Forwarded-For だけを信じて、
    # ログインの試行回数制限を利用者ごとに数える。空文字は「ヘッダーを信頼しない」。
    forwarded_allow_ips: str = "127.0.0.1"
    # anyio のスレッドプール（同期ルートを走らせる）の大きさ。
    threadpool_size: int = 40
    # 同時に走らせる Cowork（文書生成）の数。1本ごとに添付と生成物がメモリに載る。
    max_cowork_jobs: int = 2


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} は整数で指定してください（現在の値: {raw!r}）") from None
    if value <= 0:
        raise ValueError(f"{name} は1以上の整数で指定してください（現在の値: {raw!r}）")
    return value


def _port(environ: Mapping[str, str], name: str, default: int) -> int:
    value = _positive_int(environ, name, default)
    if value > _MAX_PORT:
        raise ValueError(
            f"{name} は1〜{_MAX_PORT}の整数で指定してください（現在の値: {environ[name]!r}）")
    return value


def _bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(
        f"{name} は true/false/1/0/yes/no のいずれかで指定してください（現在の値: {raw!r}）")


def load_web_settings(environ: Mapping[str, str] = os.environ) -> WebSettings:
    password_hash = environ.get("RAG_WEB_ADMIN_PASSWORD_HASH", "").strip()
    if not password_hash:
        raise ValueError("RAG_WEB_ADMIN_PASSWORD_HASH が設定されていません")
    host = environ.get("RAG_WEB_HOST", "").strip() or WebSettings.host
    return WebSettings(
        admin_password_hash=password_hash,
        host=host,
        port=_port(environ, "RAG_WEB_PORT", WebSettings.port),
        max_upload_bytes=_positive_int(
            environ, "RAG_WEB_MAX_UPLOAD_MB", WebSettings.max_upload_bytes // (1024 * 1024)
        ) * 1024 * 1024,
        session_ttl_s=_positive_int(
            environ, "RAG_WEB_SESSION_TTL_HOURS", WebSettings.session_ttl_s // 3600
        ) * 3600,
        cookie_secure=_bool(environ, "RAG_WEB_COOKIE_SECURE", WebSettings.cookie_secure),
        # 空文字は有効な値（信頼しない）なので、未設定のときだけ既定値にする。
        forwarded_allow_ips=environ.get(
            "RAG_WEB_FORWARDED_ALLOW_IPS", WebSettings.forwarded_allow_ips
        ).strip(),
        threadpool_size=_positive_int(
            environ, "RAG_WEB_THREADPOOL_SIZE", WebSettings.threadpool_size),
        max_cowork_jobs=_positive_int(
            environ, "RAG_WEB_MAX_COWORK_JOBS", WebSettings.max_cowork_jobs),
    )
