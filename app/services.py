from __future__ import annotations

from datetime import date, datetime
import re

from app.models import migrate_schema
from app.security import hash_password, is_hashed, sign_student_token, verify_password

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")

def _required_text(value, label):
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{label}不能为空")
    return value

def _valid_email(value):
    value = (value or "").strip()
    if not EMAIL_RE.fullmatch(value):
        raise ValueError("请输入有效的邮箱地址")
    return value

def _valid_schedule(weekday, planned_time):
    if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
        raise ValueError("星期必须是 0–6")
    planned_time = (planned_time or "").strip()
    if not TIME_RE.fullmatch(planned_time):
        raise ValueError("上课时间必须是 HH:MM（00:00–23:59）")
    return weekday, planned_time

def _nonnegative_int(value, label="购买课时"):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label}必须是非负整数")
    return value

from app.emailer import (
    notify_lesson_completed,
    notify_low_balance_if_needed,
    notify_renewal_requested,
    notify_schedule_changed,
    portal_url,
    reset_balance_alerts,
    suppress_inactive_emails,
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
    conn.execute("INSERT INTO users (username, password, role) VALUES ('admin', ?, 'admin')", (hash_password("admin123"),))
    conn.execute("INSERT INTO users (username, password, role, teacher_id) VALUES ('wang', ?, 'teacher', ?)", (hash_password("teacher123"), wang_id))
    conn.execute("INSERT INTO users (username, password, role, teacher_id) VALUES ('li', ?, 'teacher', ?)", (hash_password("teacher123"), li_id))
    create_student(conn, "Maria", "11 99999-9999", "maria@example.com", "钢琴", wang_id, 10, 0, "14:00")
    create_student(conn, "Lucas", "11 98888-8888", "lucas@example.com", "吉他", li_id, 8, 0, "16:00")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reminder_time', '19:00')")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('low_balance_thresholds', '10,7,5,3,1')")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('overdraft_limit', '1')")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('public_base_url', '')")


def ensure_default_settings(conn):
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('reminder_time', '19:00')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('low_balance_thresholds', '10,7,5,3,1')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('overdraft_limit', '1')")
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('public_base_url', '')")


def current_user(conn, user_id: int | None):
    if not user_id:
        return None
    return conn.execute("""SELECT u.* FROM users u WHERE u.id = ?
        AND (u.role = 'admin' OR EXISTS (SELECT 1 FROM teachers t WHERE t.id = u.teacher_id AND t.active = 1))""", (user_id,)).fetchone()


def authenticate(conn, username: str, password: str):
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not current_user(conn, user["id"]) or not verify_password(password, user["password"]):
        return None
    if not is_hashed(user["password"]):
        # 老库里的明文密码，登录成功时顺手升级成哈希
        conn.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(password), user["id"]))
    return user


def change_password(conn, user_id: int, current_password: str, new_password: str):
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or not verify_password(current_password, user["password"]):
        raise PermissionError("当前密码不正确")
    if len(new_password) < 8:
        raise ValueError("新密码至少 8 位")
    conn.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(new_password), user_id))


def create_teacher(conn, name, email):
    name = _required_text(name, "姓名")
    email = _valid_email(email)
    return conn.execute("INSERT INTO teachers (name, email) VALUES (?, ?)", (name, email)).lastrowid


def create_student(conn, name, phone, email, course_name, teacher_id, purchased_lessons, weekday, planned_time):
    name = _required_text(name, "姓名")
    phone = (phone or "").strip()
    email = _valid_email(email)
    course_name = _required_text(course_name, "课程名称")
    purchased_lessons = _nonnegative_int(purchased_lessons)
    weekday, planned_time = _valid_schedule(weekday, planned_time)
    require_active_teacher(conn, teacher_id)
    cur = conn.execute("INSERT INTO students (name, phone, email) VALUES (?, ?, ?)", (name, phone, email))
    student_id = cur.lastrowid
    package_id = add_course_package(conn, student_id, course_name, teacher_id, purchased_lessons)
    conn.execute(
        "INSERT INTO schedules (course_package_id, weekday, planned_time) VALUES (?, ?, ?)",
        (package_id, weekday, planned_time),
    )
    return student_id


def add_course_package(conn, student_id, course_name, teacher_id, purchased_lessons):
    course_name = _required_text(course_name, "课程名称")
    purchased_lessons = _nonnegative_int(purchased_lessons)
    require_active_teacher(conn, teacher_id)
    if not conn.execute("SELECT 1 FROM students WHERE id = ? AND active = 1", (student_id,)).fetchone():
        raise ValueError("学生已停用或不存在")
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
    weekday, planned_time = _valid_schedule(weekday, planned_time)
    require_active_teacher(conn, teacher_id)
    if not conn.execute("""SELECT 1 FROM schedules s JOIN course_packages cp ON cp.id=s.course_package_id
        JOIN students st ON st.id=cp.student_id WHERE s.id=? AND st.active=1 AND cp.active=1""", (schedule_id,)).fetchone():
        raise ValueError("排课不存在或学生已停用")
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
    suppress_inactive_emails(conn)
    notify_schedule_changed(conn, schedule_id)


def ensure_today_instances(conn, today: date | None = None):
    today = today or date.today()
    weekday = today.weekday()
    schedules = conn.execute("""SELECT s.id FROM schedules s JOIN course_packages cp ON cp.id=s.course_package_id
        JOIN students st ON st.id=cp.student_id JOIN teachers t ON t.id=cp.teacher_id
        WHERE s.active=1 AND cp.active=1 AND st.active=1 AND t.active=1 AND s.weekday=?""", (weekday,)).fetchall()
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
        WHERE li.lesson_date = ? AND s.active=1 AND cp.active=1 AND st.active=1 AND t.active=1 {teacher_filter}
        ORDER BY s.planned_time, st.name
        """,
        params,
    ).fetchall()


def can_access_instance(conn, user, instance_id):
    row = conn.execute("""
        SELECT cp.teacher_id FROM lesson_instances li
        JOIN schedules s ON s.id=li.schedule_id JOIN course_packages cp ON cp.id=s.course_package_id
        JOIN students st ON st.id=cp.student_id JOIN teachers t ON t.id=cp.teacher_id
        WHERE li.id=? AND s.active=1 AND cp.active=1 AND st.active=1 AND t.active=1
    """, (instance_id,)).fetchone()
    return bool(row and (user["role"] == "admin" or row["teacher_id"] == user["teacher_id"]))


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
    if new_balance < -overdraft_limit(conn):
        raise ValueError("剩余课时不足，请先续费")
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
    if record["action"] not in ("complete", "skip"):
        raise ValueError("该记录不能撤销，请使用调整课时并填写原因")
    reverse_action = "undo_complete" if record["action"] == "complete" else "undo_skip"
    if conn.execute("SELECT 1 FROM lesson_records WHERE lesson_instance_id=? AND action=? AND note=?", (record["lesson_instance_id"], reverse_action, f"撤销记录 #{record_id}")).fetchone():
        raise ValueError("这条记录已经撤销，不能重复撤销")
    if not can_access_instance(conn, user, record["lesson_instance_id"]):
        raise PermissionError("无权撤销")
    package = conn.execute("SELECT current_balance FROM course_packages WHERE id = ?", (record["course_package_id"],)).fetchone()
    reverse_delta = -record["delta_lessons"]
    new_balance = package["current_balance"] + reverse_delta
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


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def overdraft_limit(conn) -> int:
    """允许透支的节数。学生人已经到教室了，不该因为余额为 0 就没法记录。"""
    value = get_setting(conn, "overdraft_limit", "1").strip()
    return int(value) if value.lstrip("-").isdigit() and int(value) > 0 else 0


def student_portal_path(student_id: int) -> str:
    return f"/p/{sign_student_token(student_id)}"


def student_portal_url(conn, student_id: int) -> str:
    return portal_url(conn, student_id)


def student_portal_data(conn, student_id: int):
    student = conn.execute("SELECT * FROM students WHERE id = ? AND active = 1", (student_id,)).fetchone()
    if not student:
        return None
    packages = conn.execute(
        """
        SELECT cp.id, cp.course_name, cp.current_balance, cp.purchased_lessons, t.name AS teacher_name,
               (SELECT COUNT(*) FROM renewal_requests rr WHERE rr.course_package_id = cp.id AND rr.status = 'open') AS pending_renewal
        FROM course_packages cp
        JOIN teachers t ON t.id = cp.teacher_id
        WHERE cp.student_id = ? AND cp.active = 1
        ORDER BY cp.course_name
        """,
        (student_id,),
    ).fetchall()
    records = conn.execute(
        """
        SELECT lr.action, lr.delta_lessons, lr.balance_after, lr.created_at, cp.course_name
        FROM lesson_records lr
        JOIN course_packages cp ON cp.id = lr.course_package_id
        WHERE cp.student_id = ?
        ORDER BY lr.id DESC
        LIMIT 20
        """,
        (student_id,),
    ).fetchall()
    return {"student": student, "packages": packages, "records": records}


def request_renewal(conn, student_id: int, course_package_id: int):
    """学生点“我要续费”，只登记意向，不做在线支付。"""
    package = conn.execute(
        """SELECT cp.id, cp.current_balance FROM course_packages cp JOIN students st ON st.id=cp.student_id
        WHERE cp.id = ? AND cp.student_id = ? AND cp.active=1 AND st.active=1""",
        (course_package_id, student_id),
    ).fetchone()
    if not package:
        raise ValueError("课程不存在")
    existing = conn.execute(
        "SELECT id FROM renewal_requests WHERE course_package_id = ? AND status = 'open'",
        (course_package_id,),
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO renewal_requests (course_package_id, balance_at_request) VALUES (?, ?)",
        (course_package_id, package["current_balance"]),
    )
    notify_renewal_requested(conn, cur.lastrowid)
    return cur.lastrowid


def list_renewal_requests(conn, user, status: str = "open"):
    teacher_filter = ""
    params = [status]
    if user["role"] == "teacher":
        teacher_filter = "AND cp.teacher_id = ?"
        params.append(user["teacher_id"])
    return conn.execute(
        f"""
        SELECT rr.*, st.id AS student_id, st.name AS student_name, st.email AS student_email, st.phone,
               cp.course_name, cp.current_balance, t.name AS teacher_name
        FROM renewal_requests rr
        JOIN course_packages cp ON cp.id = rr.course_package_id
        JOIN students st ON st.id = cp.student_id
        JOIN teachers t ON t.id = cp.teacher_id
        WHERE rr.status = ? AND st.active=1 {teacher_filter}
        ORDER BY rr.id DESC
        """,
        params,
    ).fetchall()


def mark_renewal_handled(conn, user, renewal_id: int):
    row = conn.execute(
        """
        SELECT rr.id, cp.teacher_id
        FROM renewal_requests rr
        JOIN course_packages cp ON cp.id = rr.course_package_id
        JOIN students st ON st.id = cp.student_id
        WHERE rr.id = ? AND st.active=1
        """,
        (renewal_id,),
    ).fetchone()
    if not row:
        raise ValueError("续费申请不存在")
    if user["role"] == "teacher" and row["teacher_id"] != user["teacher_id"]:
        raise PermissionError("无权处理")
    conn.execute(
        "UPDATE renewal_requests SET status = 'handled', handled_at = ? WHERE id = ?",
        (datetime.now().isoformat(timespec="seconds"), renewal_id),
    )


def require_active_teacher(conn, teacher_id):
    if not conn.execute("SELECT 1 FROM teachers WHERE id=? AND active=1", (teacher_id,)).fetchone():
        raise ValueError("请选择在职老师")


def require_student_access(conn, user, student_id):
    student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
    if not student:
        raise ValueError("学生不存在")
    if user["role"] != "admin" and not conn.execute("""SELECT 1 FROM course_packages cp
        JOIN teachers t ON t.id=cp.teacher_id WHERE cp.student_id=? AND cp.teacher_id=? AND t.active=1""",
        (student_id, user["teacher_id"])).fetchone():
        raise PermissionError("只能操作自己的学生")
    return student


def set_student_active(conn, user, student_id, active):
    require_student_access(conn, user, student_id)
    conn.execute("UPDATE students SET active=? WHERE id=?", (int(active), student_id))
    if not active:
        suppress_inactive_emails(conn)


def set_teacher_active(conn, user, teacher_id, active):
    if user["role"] != "admin":
        raise PermissionError("仅管理员可以操作老师")
    teacher = conn.execute("SELECT * FROM teachers WHERE id=?", (teacher_id,)).fetchone()
    if not teacher:
        raise ValueError("老师不存在")
    if teacher["active"] and not active:
        conn.execute("""INSERT INTO teacher_archive_courses(teacher_id, course_package_id, balance_at_archive, last_record_id)
            SELECT teacher_id, id, current_balance,
                COALESCE((SELECT MAX(lr.id) FROM lesson_records lr WHERE lr.course_package_id=cp.id), 0)
            FROM course_packages cp WHERE teacher_id=?
            ON CONFLICT(teacher_id, course_package_id) DO UPDATE SET
                balance_at_archive=excluded.balance_at_archive, last_record_id=excluded.last_record_id,
                archived_at=CURRENT_TIMESTAMP""", (teacher_id,))
    conn.execute("UPDATE teachers SET active=? WHERE id=?", (int(active), teacher_id))
    if not active:
        suppress_inactive_emails(conn)


def list_unassigned_courses(conn):
    return conn.execute("""SELECT cp.*, st.name AS student_name, t.name AS teacher_name
        FROM course_packages cp JOIN students st ON st.id=cp.student_id JOIN teachers t ON t.id=cp.teacher_id
        WHERE st.active=1 AND cp.active=1 AND t.active=0 ORDER BY st.name, cp.id""").fetchall()


def assign_teacher(conn, user, package_id, teacher_id):
    if user["role"] != "admin":
        raise PermissionError("仅管理员可以分配老师")
    require_active_teacher(conn, teacher_id)
    row = conn.execute("""SELECT cp.id FROM course_packages cp JOIN students st ON st.id=cp.student_id
        JOIN teachers t ON t.id=cp.teacher_id WHERE cp.id=? AND cp.active=1 AND st.active=1 AND t.active=0""",
        (package_id,)).fetchone()
    if not row:
        raise ValueError("该课程已分配老师、已停用或不存在，请刷新列表")
    conn.execute("UPDATE course_packages SET teacher_id=? WHERE id=?", (teacher_id, package_id))
    suppress_inactive_emails(conn)
    for row in conn.execute("SELECT id FROM schedules WHERE course_package_id=? AND active=1", (package_id,)):
        notify_schedule_changed(conn, row["id"])


def edit_teacher_profile(conn, user, teacher_id, name, email):
    if user["role"] != "admin":
        raise PermissionError("仅管理员可以修改老师资料")
    name = _required_text(name, "姓名")
    email = _valid_email(email)
    teacher = conn.execute("SELECT active FROM teachers WHERE id=?", (teacher_id,)).fetchone()
    if not teacher:
        raise ValueError("老师不存在")
    if not teacher["active"]:
        raise ValueError("请先恢复老师，再修改资料")
    conn.execute("UPDATE teachers SET name=?, email=? WHERE id=?", (name, email, teacher_id))


def edit_student_profile(conn, user, student_id, name, phone, email):
    if user["role"] != "admin":
        raise PermissionError("仅管理员可以修改学生资料")
    name = _required_text(name, "姓名")
    phone, email = (phone or "").strip(), _valid_email(email)
    student = require_student_access(conn, user, student_id)
    if not student["active"]:
        raise ValueError("请先恢复学生，再修改资料")
    conn.execute("UPDATE students SET name=?, phone=?, email=? WHERE id=?", (name, phone, email, student_id))


def editable_package(conn, user, package_id):
    if user["role"] != "admin":
        raise PermissionError("仅管理员可以修改课程包")
    package = conn.execute("""SELECT cp.* FROM course_packages cp JOIN students st ON st.id=cp.student_id
        WHERE cp.id=? AND cp.active=1 AND st.active=1""", (package_id,)).fetchone()
    if not package:
        raise ValueError("课程包不存在或已停用")
    return package


def rename_course_package(conn, user, package_id, course_name):
    package = editable_package(conn, user, package_id)
    course_name = course_name.strip()
    if not course_name:
        raise ValueError("课程名称不能为空")
    conn.execute("UPDATE course_packages SET course_name=? WHERE id=?", (course_name, package_id))
    return package["student_id"]


def adjust_package_balance(conn, user, package_id, new_balance, reason):
    if user["role"] != "admin":
        raise PermissionError("仅管理员可以调整课时")
    reason = reason.strip()
    if not reason:
        raise ValueError("必须填写调整原因")
    if not isinstance(new_balance, int) or isinstance(new_balance, bool) or new_balance < 0:
        raise ValueError("调整后的课时必须是大于或等于 0 的整数")
    # Read and update the balance under one short write transaction; no network I/O.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT adjust_balance")
    try:
        package = editable_package(conn, user, package_id)
        delta = new_balance - package["current_balance"]
        conn.execute("UPDATE course_packages SET current_balance=? WHERE id=?", (new_balance, package_id))
        record_id = conn.execute("""INSERT INTO lesson_records
            (lesson_instance_id, course_package_id, action, delta_lessons, balance_after, actor_user_id, note)
            VALUES (NULL, ?, 'adjust', ?, ?, ?, ?)""", (package_id, delta, new_balance, user["id"], reason)).lastrowid
        conn.execute("""INSERT INTO lesson_transactions(course_package_id,lesson_record_id,delta_lessons,reason)
            VALUES (?, ?, ?, ?)""", (package_id, record_id, delta, reason))
        if delta > 0:
            reset_balance_alerts(conn, package_id, new_balance)
        elif delta < 0:
            notify_low_balance_if_needed(conn, package_id, new_balance)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT adjust_balance")
        raise
    finally:
        conn.execute("RELEASE SAVEPOINT adjust_balance")
    return package["student_id"]
