import pytest

from rag_web.settings import load_web_settings


def test_defaults_when_only_hash_is_set():
    s = load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "scrypt:..."})
    assert (s.host, s.port, s.max_upload_bytes, s.session_ttl_s, s.cookie_secure) == (
        "127.0.0.1", 8502, 50 * 1024 * 1024, 12 * 3600, False)


def test_missing_hash_is_rejected():
    with pytest.raises(ValueError, match="RAG_WEB_ADMIN_PASSWORD_HASH"):
        load_web_settings({})


@pytest.mark.parametrize("name,value", [
    ("RAG_WEB_PORT", "abc"), ("RAG_WEB_PORT", "0"), ("RAG_WEB_PORT", "65536"),
    ("RAG_WEB_MAX_UPLOAD_MB", "-1"), ("RAG_WEB_SESSION_TTL_HOURS", "x"),
    ("RAG_WEB_COOKIE_SECURE", "maybe")])
def test_invalid_values_name_the_variable(name, value):
    with pytest.raises(ValueError, match=name):
        load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "h", name: value})


def test_port_accepts_upper_bound():
    s = load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "h", "RAG_WEB_PORT": "65535"})
    assert s.port == 65535


def test_proxy_and_concurrency_defaults():
    s = load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "h"})
    assert (s.forwarded_allow_ips, s.threadpool_size, s.max_cowork_jobs) == ("127.0.0.1", 40, 2)


def test_empty_forwarded_allow_ips_is_kept_empty():
    # 空は「プロキシのヘッダーを信頼しない」という意思表示。既定値へ戻してはいけない。
    s = load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "h", "RAG_WEB_FORWARDED_ALLOW_IPS": ""})
    assert s.forwarded_allow_ips == ""


def test_forwarded_allow_ips_is_stripped():
    s = load_web_settings(
        {"RAG_WEB_ADMIN_PASSWORD_HASH": "h", "RAG_WEB_FORWARDED_ALLOW_IPS": "  10.0.0.1,10.0.0.2 "})
    assert s.forwarded_allow_ips == "10.0.0.1,10.0.0.2"


@pytest.mark.parametrize("name,value", [
    ("RAG_WEB_THREADPOOL_SIZE", "0"), ("RAG_WEB_THREADPOOL_SIZE", "x"),
    ("RAG_WEB_MAX_COWORK_JOBS", "abc"), ("RAG_WEB_MAX_COWORK_JOBS", "0")])
def test_concurrency_values_name_the_variable(name, value):
    with pytest.raises(ValueError, match=name):
        load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "h", name: value})


@pytest.mark.parametrize("value,expected", [
    ("127.0.0.1", "127.0.0.1"),
    ("::1", "::1"),
    (" 127.0.0.1 , 10.0.0.0/24,fd00::/8 ", "127.0.0.1,10.0.0.0/24,fd00::/8"),
    ("", ""),
])
def test_forwarded_allow_ips_accepts_ips_and_cidrs(value, expected):
    s = load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "h", "RAG_WEB_FORWARDED_ALLOW_IPS": value})
    assert s.forwarded_allow_ips == expected


@pytest.mark.parametrize("value", [
    "*", "127.0.0.1,*", "127.0.0.l", "localhost", "10.0.0.1,", "10.0.0.1/24"])
def test_forwarded_allow_ips_rejects_wildcards_and_typos(value):
    with pytest.raises(ValueError, match="RAG_WEB_FORWARDED_ALLOW_IPS") as error:
        load_web_settings({"RAG_WEB_ADMIN_PASSWORD_HASH": "h", "RAG_WEB_FORWARDED_ALLOW_IPS": value})
    assert repr(value) in str(error.value)
