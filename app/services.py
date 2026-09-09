from __future__ import annotations

from datetime import date, datetime

from app.models import migrate_schema

from app.emailer import (
    notify_lesson_completed,
    notify_low_balance_if_needed,
    notify_schedule_changed,
    reset_balance_alerts,
)


def init_schema(conn, schema_sql: str):
    conn.executescript(schema_sql)
    migrate_schema(conn)


def seed_data(conn):
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    conn.execute("INSERT INTO teachers (name, email) VALUES (?, ?)", ("王老师", "wang@example.com"))
    conn.execute("INSERT INTO teachers (name, email) VALUES (?, ?)", ("李老师", "li@example.com"))
    wang_id = conn.execute("SELECT id FROM teachers WHERE name = '王老师'").fetchone()[0]
    li_id = conn.execute("SELECT id FROM teachers WHERE name = '李老师'").fetchone()[0]
    conn.execute("INSERT INTO users (username, password, role) VALUES ('admin', 'admin123', 'admin')")
    conn.execute("INSERT INTO users (username, password, role, teacher_id) VALUES ('wang', 'teacher123', 'teacher', ?)", (wang_id,))
    conn.execute("INSERT INTO users (username, password, role, teacher_id) VALUES ('li', 'teacher123', 'teacher', ?)", (li_id,))
    create_student(conn, "Maria", "11 99999-9999", "maria@example.com", "钢琴", wang_id, 10, 0, "14:00")
    create_student(conn, "Lucas", "11 98888-8888", "lucas@example.com", "吉他", li_id, 8, 0, "16:00")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reminder_time', '19:00')")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('low_balance_thresholds', '10,7,5,3,1')")


def ensure_default_settings(conn):
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('reminder_time', '19:00')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('low_balance_thresholds', '10,7,5,3,1')")


def current_user(conn, user_id: int | None):
    if not user_id:
        return None
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def authenticate(conn, username: str, password: str):
    return conn.execute("SELECT * FROM users WHERE username = ? AND password = ?", (username, password)).fetchone()


def create_student(conn, name, phone, email, course_name, teacher_id, purchased_lessons, weekday, planned_time):
    cur = conn.execute("INSERT INTO students (name, phone, email) VALUES (?, ?, ?)", (name, phone, email))
    student_id = cur.lastrowid
    package_id = add_course_package(conn, student_id, course_name, teacher_id, purchased_lessons)
    conn.execute(
        "INSERT INTO schedules (course_package_id, weekday, planned_time) VALUES (?, ?, ?)",
        (package_id, weekday, planned_time),
    )
    return student_id


def add_course_package(conn, student_id, course_name, teacher_id, purchased_lessons):
    existing = conn.execute(
        """
        SELECT id, purchased_lessons, current_balance
        FROM course_packages
        WHERE student_id = ? AND course_name = ? AND teacher_id = ? AND active = 1
        ORDER BY id
        LIMIT 1
        """,
        (student_id, course_name, teacher_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE course_packages SET purchased_lessons = ?, current_balance = ? WHERE id = ?",
            (
                existing["purchased_lessons"] + purchased_lessons,
                existing["current_balance"] + purchased_lessons,
                existing["id"],
            ),
        )
        conn.execute(
            "INSERT INTO lesson_transactions (course_package_id, delta_lessons, reason) VALUES (?, ?, 'purchase')",
            (existing["id"], purchased_lessons),
        )
        reset_balance_alerts(conn, existing["id"], existing["current_balance"] + purchased_lessons)
        return existing["id"]
    cur = conn.execute(
        """
        INSERT INTO course_packages (student_id, course_name, teacher_id, purchased_lessons, current_balance)
        VALUES (?, ?, ?, ?, ?)
        """,
        (student_id, course_name, teacher_id, purchased_lessons, purchased_lessons),
    )
    package_id = cur.lastrowid
    conn.execute(
        "INSERT INTO lesson_transactions (course_package_id, delta_lessons, reason) VALUES (?, ?, 'purchase')",
        (package_id, purchased_lessons),
    )
    reset_balance_alerts(conn, package_id, purchased_lessons)
    return package_id


def update_schedule(conn, schedule_id, teacher_id, weekday, planned_time):
    conn.execute(
        """
        UPDATE course_packages SET teacher_id = ?
        WHERE id = (SELECT course_package_id FROM schedules WHERE id = ?)
        """,
        (teacher_id, schedule_id),
    )
    conn.execute(
        "UPDATE schedules SET weekday = ?, planned_time = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (weekday, planned_time, schedule_id),
    )
    notify_schedule_changed(conn, schedule_id)


def ensure_today_instances(conn, today: date | None = None):
    today = today or date.today()
    weekday = today.weekday()
    schedules = conn.execute("SELECT id FROM schedules WHERE active = 1 AND weekday = ?", (weekday,)).fetchall()
    for schedule in schedules:
        conn.execute(
            "INSERT OR IGNORE INTO lesson_instances (schedule_id, lesson_date) VALUES (?, ?)",
            (schedule["id"], today.isoformat()),
        )


def list_today_lessons(conn, user, today: date | None = None):
    ensure_today_instances(conn, today)
    today = today or date.today()
    params = [today.isoformat()]
    teacher_filter = ""
    if user["role"] == "teacher":
        teacher_filter = "AND cp.teacher_id = ?"
        params.append(user["teacher_id"])
    return conn.execute(
        f"""
        SELECT li.id, li.status, li.confirmed_at, s.planned_time, st.name AS student_name,
               cp.course_name, cp.current_balance, t.name AS teacher_name
        FROM lesson_instances li
        JOIN schedules s ON s.id = li.schedule_id
        JOIN course_packages cp ON cp.id = s.course_package_id
        JOIN students st ON st.id = cp.student_id
        JOIN teachers t ON t.id = cp.teacher_id
        WHERE li.lesson_date = ? {teacher_filter}
        ORDER BY s.planned_time, st.name
        """,
        params,
    ).fetchall()


def can_access_instance(conn, user, instance_id):
    if user["role"] == "admin":
        return True
    row = conn.execute(
        """
        SELECT cp.teacher_id
        FROM lesson_instances li
        JOIN schedules s ON s.id = li.schedule_id
        JOIN course_packages cp ON cp.id = s.course_package_id
        WHERE li.id = ?
        """,
        (instance_id,),
    ).fetchone()
    return row and row["teacher_id"] == user["teacher_id"]


def record_lesson(conn, user, instance_id, action):
    if action not in ("complete", "skip"):
        raise ValueError("invalid action")
    if not can_access_instance(conn, user, instance_id):
        raise PermissionError("无权操作这节课")
    row = conn.execute(
        """
        SELECT li.status, s.course_package_id, cp.current_balance
        FROM lesson_instances li
        JOIN schedules s ON s.id = li.schedule_id
        JOIN course_packages cp ON cp.id = s.course_package_id
        WHERE li.id = ?
        """,
        (instance_id,),
    ).fetchone()
    if not row:
        raise ValueError("课程不存在")
    if row["status"] != "planned":
        raise ValueError("这节课已处理")
    delta = -1 if action == "complete" else 0
    new_balance = row["current_balance"] + delta
    if new_balance < 0:
        raise ValueError("剩余课时不足")
    status = "completed" if action == "complete" else "skipped"
    confirmed_at = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE course_packages SET current_balance = ? WHERE id = ?", (new_balance, row["course_package_id"]))
    conn.execute("UPDATE lesson_instances SET status = ?, confirmed_at = ? WHERE id = ?", (status, confirmed_at, instance_id))
    cur = conn.execute(
        """
        INSERT INTO lesson_records
        (lesson_instance_id, course_package_id, action, delta_lessons, balance_after, actor_user_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (instance_id, row["course_package_id"], action, delta, new_balance, user["id"]),
    )
    conn.execute(
        "INSERT INTO lesson_transactions (course_package_id, lesson_record_id, delta_lessons, reason) VALUES (?, ?, ?, ?)",
        (row["course_package_id"], cur.lastrowid, delta, action),
    )
    if action == "complete":
        notify_lesson_completed(conn, instance_id, new_balance)
        notify_low_balance_if_needed(conn, row["course_package_id"], new_balance)


def update_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def undo_last_record(conn, user, record_id):
    record = conn.execute("SELECT * FROM lesson_records WHERE id = ?", (record_id,)).fetchone()
    if not record:
        raise ValueError("记录不存在")
    if not can_access_instance(conn, user, record["lesson_instance_id"]):
        raise PermissionError("无权撤销")
    package = conn.execute("SELECT current_balance FROM course_packages WHERE id = ?", (record["course_package_id"],)).fetchone()
    reverse_delta = -record["delta_lessons"]
    new_balance = package["current_balance"] + reverse_delta
    reverse_action = "undo_complete" if record["action"] == "complete" else "undo_skip"
    conn.execute("UPDATE course_packages SET current_balance = ? WHERE id = ?", (new_balance, record["course_package_id"]))
    conn.execute("UPDATE lesson_instances SET status = 'planned', confirmed_at = NULL WHERE id = ?", (record["lesson_instance_id"],))
    cur = conn.execute(
        """
        INSERT INTO lesson_records
        (lesson_instance_id, course_package_id, action, delta_lessons, balance_after, actor_user_id, note)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (record["lesson_instance_id"], record["course_package_id"], reverse_action, reverse_delta, new_balance, user["id"], f"撤销记录 #{record_id}"),
    )
    conn.execute(
        "INSERT INTO lesson_transactions (course_package_id, lesson_record_id, delta_lessons, reason) VALUES (?, ?, ?, ?)",
        (record["course_package_id"], cur.lastrowid, reverse_delta, reverse_action),
    )
    if reverse_delta > 0:
        reset_balance_alerts(conn, record["course_package_id"], new_balance)
