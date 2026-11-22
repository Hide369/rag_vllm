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
