from app.main import cookie_secure_enabled


def test_cookie_secure_enabled_when_enabled(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("COOKIE_SECURE", value)
        assert cookie_secure_enabled() is True


def test_cookie_secure_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    assert cookie_secure_enabled() is False


def test_cookie_secure_disabled_for_false_values(monkeypatch):
    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("COOKIE_SECURE", value)
        assert cookie_secure_enabled() is False


def test_login_cookie_secure_enabled(monkeypatch, tmp_path):
    from contextlib import contextmanager

    from app import main
    from app.database import connect
    from app.models import SCHEMA_SQL
    from app.security import hash_password
    from app.services import init_schema

    db_path = tmp_path / "cookie-test.sqlite3"

    with connect(db_path) as conn:
        init_schema(conn, SCHEMA_SQL)
        conn.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            ("cookie-test", hash_password("test-password-123"), "admin"),
        )
        conn.commit()

    @contextmanager
    def test_db_session():
        conn = connect(db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(main, "db_session", test_db_session)
    monkeypatch.setenv("COOKIE_SECURE", "1")

    response = main.login("cookie-test", "test-password-123")
    cookie = response.headers["set-cookie"]

    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_login_cookie_secure_disabled_by_default(monkeypatch, tmp_path):
    from contextlib import contextmanager

    from app import main
    from app.database import connect
    from app.models import SCHEMA_SQL
    from app.security import hash_password
    from app.services import init_schema

    db_path = tmp_path / "cookie-test.sqlite3"

    with connect(db_path) as conn:
        init_schema(conn, SCHEMA_SQL)
        conn.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            ("cookie-test", hash_password("test-password-123"), "admin"),
        )
        conn.commit()

    @contextmanager
    def test_db_session():
        conn = connect(db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(main, "db_session", test_db_session)
    monkeypatch.delenv("COOKIE_SECURE", raising=False)

    response = main.login("cookie-test", "test-password-123")
    cookie = response.headers["set-cookie"]

    assert "Secure" not in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_logout_cookie_uses_secure_attributes(monkeypatch):
    from app import main

    monkeypatch.setenv("COOKIE_SECURE", "1")

    response = main.logout()
    cookie = response.headers["set-cookie"]

    assert "session=" in cookie
    assert "Max-Age=0" in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
