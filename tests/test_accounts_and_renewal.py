from datetime import date

import pytest

from app.models import SCHEMA_SQL
from app.security import hash_password, read_student_token, sign_student_token, verify_password
from app.services import (
    authenticate,
    change_password,
    list_renewal_requests,
    list_today_lessons,
    mark_renewal_handled,
    record_lesson,
    request_renewal,
    init_schema,
    seed_data,
    student_portal_data,
    update_setting,
)


@pytest.fixture()
def conn(tmp_path):
    import sqlite3

    connection = sqlite3.connect(tmp_path / "test.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    init_schema(connection, SCHEMA_SQL)
    seed_data(connection)
    yield connection
    connection.close()


def user(conn, username):
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def package_of(conn, student_name):
    return conn.execute(
        "SELECT cp.id, cp.student_id FROM course_packages cp JOIN students st ON st.id = cp.student_id WHERE st.name = ?",
        (student_name,),
    ).fetchone()


def first_lesson(conn, username="admin", day=date(2026, 9, 7)):
    return list_today_lessons(conn, user(conn, username), day)[0]


def test_seeded_passwords_are_hashed_not_plaintext(conn):
    stored = user(conn, "admin")["password"]
    assert stored != "admin123"
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("admin123", stored)


def test_login_still_works_with_correct_password(conn):
    assert authenticate(conn, "admin", "admin123") is not None
    assert authenticate(conn, "admin", "wrong") is None
    assert authenticate(conn, "nobody", "admin123") is None


def test_legacy_plaintext_password_logs_in_and_is_upgraded(conn):
    conn.execute("UPDATE users SET password = 'teacher123' WHERE username = 'wang'")
    assert authenticate(conn, "wang", "teacher123") is not None
    stored = user(conn, "wang")["password"]
    assert stored.startswith("pbkdf2_sha256$")
    assert authenticate(conn, "wang", "teacher123") is not None


def test_change_password_requires_current_password_and_minimum_length(conn):
    admin = user(conn, "admin")
    with pytest.raises(PermissionError):
        change_password(conn, admin["id"], "wrong", "brandnewpassword")
    with pytest.raises(ValueError):
        change_password(conn, admin["id"], "admin123", "short")
    change_password(conn, admin["id"], "admin123", "brandnewpassword")
    assert authenticate(conn, "admin", "brandnewpassword") is not None
    assert authenticate(conn, "admin", "admin123") is None


def test_overdraft_allows_recording_beyond_zero_balance(conn):
    package = package_of(conn, "Maria")
    conn.execute("UPDATE course_packages SET current_balance = 0 WHERE id = ?", (package["id"],))
    lesson = first_lesson(conn)
    record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")
    balance = conn.execute("SELECT current_balance FROM course_packages WHERE id = ?", (package["id"],)).fetchone()[0]
    assert balance == -1


def test_overdraft_limit_is_enforced(conn):
    update_setting(conn, "overdraft_limit", "0")
    package = package_of(conn, "Maria")
    conn.execute("UPDATE course_packages SET current_balance = 0 WHERE id = ?", (package["id"],))
    lesson = first_lesson(conn)
    with pytest.raises(ValueError):
        record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")


def test_zero_balance_triggers_low_balance_alert(conn):
    update_setting(conn, "low_balance_thresholds", "1")
    package = package_of(conn, "Maria")
    conn.execute("UPDATE course_packages SET current_balance = 1 WHERE id = ?", (package["id"],))
    conn.execute("DELETE FROM balance_alerts WHERE course_package_id = ?", (package["id"],))
    lesson = first_lesson(conn)
    record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")
    bodies = [row["body"] for row in conn.execute("SELECT body FROM email_logs WHERE subject = '课时余额提醒'").fetchall()]
    assert bodies and "课时已用完" in bodies[0]


def test_student_token_round_trip_and_rejects_tampering(conn):
    package = package_of(conn, "Maria")
    token = sign_student_token(package["student_id"])
    assert read_student_token(token) == package["student_id"]
    assert read_student_token(token + "x") is None


def test_portal_shows_only_that_students_packages(conn):
    package = package_of(conn, "Maria")
    data = student_portal_data(conn, package["student_id"])
    assert data["student"]["name"] == "Maria"
    assert {row["course_name"] for row in data["packages"]} == {"钢琴"}


def test_renewal_request_notifies_teacher_and_is_deduplicated(conn):
    package = package_of(conn, "Maria")
    first = request_renewal(conn, package["student_id"], package["id"])
    second = request_renewal(conn, package["student_id"], package["id"])
    assert first == second
    subjects = [row["subject"] for row in conn.execute("SELECT subject FROM email_logs").fetchall()]
    assert subjects.count("续费申请") == 1


def test_renewal_request_rejects_other_students_package(conn):
    maria = package_of(conn, "Maria")
    lucas = package_of(conn, "Lucas")
    with pytest.raises(ValueError):
        request_renewal(conn, maria["student_id"], lucas["id"])


def test_teacher_only_sees_own_renewal_requests(conn):
    maria = package_of(conn, "Maria")
    lucas = package_of(conn, "Lucas")
    request_renewal(conn, maria["student_id"], maria["id"])
    request_renewal(conn, lucas["student_id"], lucas["id"])
    wang = list_renewal_requests(conn, user(conn, "wang"))
    assert [row["student_name"] for row in wang] == ["Maria"]
    assert len(list_renewal_requests(conn, user(conn, "admin"))) == 2


def test_teacher_cannot_handle_other_teachers_renewal(conn):
    lucas = package_of(conn, "Lucas")
    renewal_id = request_renewal(conn, lucas["student_id"], lucas["id"])
    with pytest.raises(PermissionError):
        mark_renewal_handled(conn, user(conn, "wang"), renewal_id)
    mark_renewal_handled(conn, user(conn, "li"), renewal_id)
    assert list_renewal_requests(conn, user(conn, "admin"), "open") == []
    assert len(list_renewal_requests(conn, user(conn, "admin"), "handled")) == 1


def test_portal_link_is_added_to_emails_when_base_url_is_set(conn):
    update_setting(conn, "public_base_url", "https://music.example.com/")
    lesson = first_lesson(conn)
    record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")
    body = conn.execute(
        "SELECT body FROM email_logs WHERE recipient_type = 'student' AND subject = '课程完成通知'"
    ).fetchone()[0]
    assert "https://music.example.com/p/" in body


def test_no_portal_link_when_base_url_is_empty(conn):
    lesson = first_lesson(conn)
    record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")
    body = conn.execute(
        "SELECT body FROM email_logs WHERE recipient_type = 'student' AND subject = '课程完成通知'"
    ).fetchone()[0]
    assert "/p/" not in body
