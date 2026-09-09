import pytest

from app.emailer import MAX_ATTEMPTS, process_email_queue, queue_email
from app.models import SCHEMA_SQL
from app.services import add_course_package, create_student, init_schema, record_lesson, seed_data, undo_last_record
from datetime import date


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


@pytest.fixture()
def smtp_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USERNAME", "user@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")


def user(conn, username):
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def logs(conn):
    return conn.execute("SELECT * FROM email_logs ORDER BY id").fetchall()


def package_of(conn, student_name):
    return conn.execute(
        "SELECT cp.id FROM course_packages cp JOIN students st ON st.id = cp.student_id WHERE st.name = ?",
        (student_name,),
    ).fetchone()[0]


def first_lesson(conn, username="admin"):
    from app.services import list_today_lessons

    return list_today_lessons(conn, user(conn, username), date(2026, 9, 7))[0]


def test_queued_email_is_pending_and_not_sent_inline(conn):
    queue_email(conn, "a@example.com", "student", "主题", "正文", "test", 1)
    row = logs(conn)[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 0


def test_queue_is_delivered_and_marked_sent(conn, smtp_env):
    queue_email(conn, "a@example.com", "student", "主题", "正文", "test", 1)
    calls = []
    sent, failed = process_email_queue(conn, sender=lambda *args: calls.append(args))
    assert (sent, failed) == (1, 0)
    assert len(calls) == 1
    assert logs(conn)[0]["status"] == "sent"


def test_failed_delivery_is_retried_then_marked_failed(conn, smtp_env):
    queue_email(conn, "a@example.com", "student", "主题", "正文", "test", 1)

    def boom(*args):
        raise RuntimeError("smtp down")

    for attempt in range(1, MAX_ATTEMPTS):
        # 退避时间还没到，需要清掉 next_attempt_at 才能立刻重试
        conn.execute("UPDATE email_logs SET next_attempt_at = NULL")
        process_email_queue(conn, sender=boom)
        row = logs(conn)[0]
        assert row["attempts"] == attempt
        if attempt < MAX_ATTEMPTS:
            pass
    conn.execute("UPDATE email_logs SET next_attempt_at = NULL")
    process_email_queue(conn, sender=boom)
    row = logs(conn)[0]
    assert row["status"] == "failed"
    assert row["attempts"] == MAX_ATTEMPTS
    assert "smtp down" in row["error"]


def test_pending_email_waits_for_backoff_before_retry(conn, smtp_env):
    queue_email(conn, "a@example.com", "student", "主题", "正文", "test", 1)

    def boom(*args):
        raise RuntimeError("smtp down")

    process_email_queue(conn, sender=boom)
    assert logs(conn)[0]["status"] == "pending"
    calls = []
    process_email_queue(conn, sender=lambda *args: calls.append(args))
    assert calls == []  # 还在退避窗口内，不会立刻重发


def test_without_smtp_config_queue_is_marked_disabled(conn, monkeypatch):
    for key in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    queue_email(conn, "a@example.com", "student", "主题", "正文", "test", 1)
    process_email_queue(conn)
    assert logs(conn)[0]["status"] == "disabled"


def test_low_balance_alert_fires_even_when_exact_threshold_is_skipped(conn):
    conn.execute("UPDATE settings SET value = '5' WHERE key = 'low_balance_thresholds'")
    package_id = package_of(conn, "Maria")
    # 手工调整导致余额从 10 直接掉到 4，跳过了阈值 5
    conn.execute("UPDATE course_packages SET current_balance = 5 WHERE id = ?", (package_id,))
    conn.execute("DELETE FROM balance_alerts WHERE course_package_id = ?", (package_id,))
    lesson = first_lesson(conn)
    record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")
    subjects = [row["subject"] for row in logs(conn)]
    assert "课时余额提醒" in subjects


def test_low_balance_alert_is_sent_once_per_threshold(conn):
    conn.execute("UPDATE settings SET value = '5' WHERE key = 'low_balance_thresholds'")
    package_id = package_of(conn, "Maria")
    conn.execute("UPDATE course_packages SET current_balance = 6 WHERE id = ?", (package_id,))
    conn.execute("DELETE FROM balance_alerts WHERE course_package_id = ?", (package_id,))
    teacher_id = conn.execute("SELECT teacher_id FROM course_packages WHERE id = ?", (package_id,)).fetchone()[0]

    for day in (date(2026, 9, 7), date(2026, 9, 14)):
        from app.services import ensure_today_instances, list_today_lessons

        ensure_today_instances(conn, day)
        lesson = [row for row in list_today_lessons(conn, user(conn, "admin"), day) if row["student_name"] == "Maria"][0]
        record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")

    alerts = [row["subject"] for row in logs(conn) if row["subject"] == "课时余额提醒"]
    assert len(alerts) == 2  # 学生一封 + 老师一封，只提醒一次


def test_renewal_resets_alerts_so_future_drop_alerts_again(conn):
    conn.execute("UPDATE settings SET value = '5' WHERE key = 'low_balance_thresholds'")
    package_id = package_of(conn, "Maria")
    student_id = conn.execute("SELECT student_id FROM course_packages WHERE id = ?", (package_id,)).fetchone()[0]
    teacher_id = conn.execute("SELECT teacher_id FROM course_packages WHERE id = ?", (package_id,)).fetchone()[0]
    conn.execute("INSERT OR REPLACE INTO balance_alerts (course_package_id, threshold) VALUES (?, 5)", (package_id,))

    add_course_package(conn, student_id, "钢琴", teacher_id, 10)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM balance_alerts WHERE course_package_id = ? AND threshold = 5", (package_id,)
    ).fetchone()[0]
    assert remaining == 0  # 续费后阈值重新武装
