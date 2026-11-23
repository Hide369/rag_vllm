import getpass

import pytest

from rag_web.auth import LoginThrottle, SessionStore, hash_password, validate_password_hash, verify_password
from rag_web import password as password_cli

PASSWORD = "correct horse battery"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_hash_roundtrip_and_wrong_password():
    stored = hash_password(PASSWORD, n=2**10)
    assert verify_password(PASSWORD, stored) is True
    assert verify_password("wrong password!!", stored) is False


def test_hash_rejects_empty_password():
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_returns_false_for_malformed_hash():
    assert verify_password(PASSWORD, "plain") is False


def test_validate_accepts_generated_hash():
    validate_password_hash(hash_password(PASSWORD, n=2**10))


@pytest.mark.parametrize("bad", ["plain", "scrypt:1:2", "scrypt:0:0:0::", "scrypt:0:8:1:AAAA:AAAA", "scrypt:1024:8:1::AAAA", "scrypt:3:8:1:AAAA:AAAA", "scrypt:1:8:1:AAAA:AAAA", "scrypt:1024:0:1:AAAA:AAAA", "scrypt:1024:8:0:AAAA:AAAA"])
def test_validate_rejects_malformed_hash(bad):
    with pytest.raises(ValueError, match="RAG_WEB_ADMIN_PASSWORD_HASH"):
        validate_password_hash(bad)


def test_validate_rejects_parameters_over_scrypt_memory_limit():
    # 形式は正しいが N=2**17 は verify_password の maxmem（64MB）を超え、二度と照合できない。
    with pytest.raises(ValueError, match="RAG_WEB_ADMIN_PASSWORD_HASH"):
        validate_password_hash("scrypt:131072:8:1:AAAAAAAAAAAAAAAAAAAAAA==:AAAAAAAAAAAAAAAAAAAAAA==")


def test_validate_accepts_default_parameters():
    # 既定の N=2**15 は上限内に収まり、そのまま照合もできる。
    stored = hash_password(PASSWORD)
    validate_password_hash(stored)
    assert verify_password(PASSWORD, stored) is True


def test_validate_rejects_non_numeric_parameters():
    with pytest.raises(ValueError, match="rag_web.password"):
        validate_password_hash("scrypt:a:8:1:AAAA:AAAA")


def test_session_expires_and_revokes():
    clock = FakeClock()
    store = SessionStore(ttl_s=60, clock=clock)
    token = store.create()
    assert store.is_valid(token) is True
    clock.now += 61
    assert store.is_valid(token) is False

    token2 = store.create()
    assert store.is_valid(token2) is True
    store.revoke(token2)
    assert store.is_valid(token2) is False
    assert store.is_valid(None) is False
    store.revoke(None)


def test_throttle_locks_after_five_failures_and_resets_on_success():
    clock = FakeClock()
    throttle = LoginThrottle(clock=clock)
    for _ in range(4):
        throttle.record_failure("a")
    assert throttle.retry_after("a") == 0
    throttle.record_failure("a")
    assert throttle.retry_after("a") == pytest.approx(900)
    assert throttle.retry_after("b") == 0
    clock.now += 901
    assert throttle.retry_after("a") == 0

    for _ in range(5):
        throttle.record_failure("c")
    throttle.record_success("c")
    assert throttle.retry_after("c") == 0


def _feed_passwords(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(it))


def test_password_main_rejects_short_and_mismatch(monkeypatch, capsys):
    _feed_passwords(monkeypatch, ["short"])
    assert password_cli.main() == 2
    _feed_passwords(monkeypatch, [PASSWORD, "different password!"])
    assert password_cli.main() == 2
    assert "RAG_WEB_ADMIN_PASSWORD_HASH" not in capsys.readouterr().out


def test_password_main_accepts_nine_characters_and_rejects_eight(monkeypatch, capsys):
    """最小の長さは9文字（利用者の指定 2026-11-23）。境界の前後を固定する。"""
    monkeypatch.setattr(password_cli, "hash_password", lambda p: hash_password(p, n=2**10))
    _feed_passwords(monkeypatch, ["a" * 8])
    assert password_cli.main() == 2
    assert "9 文字以上" in capsys.readouterr().err
    _feed_passwords(monkeypatch, ["a" * 9, "a" * 9])
    assert password_cli.main() == 0
    assert capsys.readouterr().out.startswith("RAG_WEB_ADMIN_PASSWORD_HASH=scrypt:")


def test_password_main_prints_env_line(monkeypatch, capsys):
    monkeypatch.setattr(password_cli, "hash_password", lambda p: hash_password(p, n=2**10))
    _feed_passwords(monkeypatch, [PASSWORD, PASSWORD])
    assert password_cli.main() == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("RAG_WEB_ADMIN_PASSWORD_HASH=scrypt:")
    validate_password_hash(out.split("=", 1)[1])
