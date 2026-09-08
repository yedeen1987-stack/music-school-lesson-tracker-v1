from datetime import date

import pytest

from app.models import SCHEMA_SQL
from app.services import (
    create_student,
    ensure_today_instances,
    init_schema,
    list_today_lessons,
    record_lesson,
    seed_data,
    undo_last_record,
)


@pytest.fixture()
def conn(tmp_path):
    import sqlite3

    db = tmp_path / "test.sqlite3"
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    init_schema(connection, SCHEMA_SQL)
    seed_data(connection)
    yield connection
    connection.close()


def user(conn, username):
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def first_lesson(conn, username="admin"):
    lessons = list_today_lessons(conn, user(conn, username), date(2026, 9, 7))
    return lessons[0]


def balance_for_lesson(conn, lesson_id):
    return conn.execute(
        """
        SELECT cp.current_balance
        FROM lesson_instances li
        JOIN schedules s ON s.id = li.schedule_id
        JOIN course_packages cp ON cp.id = s.course_package_id
        WHERE li.id = ?
        """,
        (lesson_id,),
    ).fetchone()[0]


def test_complete_lesson_deducts_one(conn):
    lesson = first_lesson(conn)
    before = balance_for_lesson(conn, lesson["id"])
    record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")
    assert balance_for_lesson(conn, lesson["id"]) == before - 1


def test_complete_lesson_writes_completion_email_logs(conn):
    lesson = first_lesson(conn)
    record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")
    subjects = [row[0] for row in conn.execute("SELECT subject FROM email_logs ORDER BY id").fetchall()]
    assert subjects == ["课程完成通知", "课程完成通知"]


def test_low_balance_threshold_writes_extra_email_logs(conn):
    conn.execute("UPDATE settings SET value = '9,7,5,3,1' WHERE key = 'low_balance_thresholds'")
    lesson = first_lesson(conn)
    record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")
    subjects = [row[0] for row in conn.execute("SELECT subject FROM email_logs ORDER BY id").fetchall()]
    assert subjects == ["课程完成通知", "课程完成通知", "课时余额提醒", "课时余额提醒"]


def test_skip_lesson_deducts_zero(conn):
    lesson = first_lesson(conn)
    before = balance_for_lesson(conn, lesson["id"])
    record_lesson(conn, user(conn, "admin"), lesson["id"], "skip")
    assert balance_for_lesson(conn, lesson["id"]) == before


def test_undo_complete_restores_balance_with_reverse_record(conn):
    lesson = first_lesson(conn)
    before = balance_for_lesson(conn, lesson["id"])
    record_lesson(conn, user(conn, "admin"), lesson["id"], "complete")
    record_id = conn.execute("SELECT MAX(id) FROM lesson_records").fetchone()[0]
    undo_last_record(conn, user(conn, "admin"), record_id)
    assert balance_for_lesson(conn, lesson["id"]) == before
    actions = [r[0] for r in conn.execute("SELECT action FROM lesson_records ORDER BY id").fetchall()]
    assert "complete" in actions
    assert "undo_complete" in actions


def test_teacher_only_sees_own_lessons(conn):
    wang_lessons = list_today_lessons(conn, user(conn, "wang"), date(2026, 9, 7))
    li_lessons = list_today_lessons(conn, user(conn, "li"), date(2026, 9, 7))
    assert [row["teacher_name"] for row in wang_lessons] == ["王老师"]
    assert [row["teacher_name"] for row in li_lessons] == ["李老师"]


def test_teacher_cannot_operate_other_teacher_lesson(conn):
    lesson = list_today_lessons(conn, user(conn, "li"), date(2026, 9, 7))[0]
    with pytest.raises(PermissionError):
        record_lesson(conn, user(conn, "wang"), lesson["id"], "complete")


def test_today_lessons_are_generated_from_weekday_schedule(conn):
    ensure_today_instances(conn, date(2026, 9, 7))
    lessons = list_today_lessons(conn, user(conn, "admin"), date(2026, 9, 7))
    assert {row["student_name"] for row in lessons} == {"Maria", "Lucas"}


def test_balances_are_per_course_package(conn):
    teacher_id = conn.execute("SELECT id FROM teachers WHERE name = '王老师'").fetchone()[0]
    student_id = create_student(conn, "Ana", "", "ana@example.com", "钢琴", teacher_id, 5, 0, "10:00")
    from app.services import add_course_package

    guitar_id = add_course_package(conn, student_id, "吉他", teacher_id, 3)
    piano_id = conn.execute(
        "SELECT id FROM course_packages WHERE student_id = ? AND course_name = '钢琴'",
        (student_id,),
    ).fetchone()[0]
    assert piano_id != guitar_id
    assert conn.execute("SELECT current_balance FROM course_packages WHERE id = ?", (piano_id,)).fetchone()[0] == 5
    assert conn.execute("SELECT current_balance FROM course_packages WHERE id = ?", (guitar_id,)).fetchone()[0] == 3
