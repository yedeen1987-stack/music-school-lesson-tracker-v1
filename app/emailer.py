import os
import smtplib
from email.message import EmailMessage


def send_email(conn, to_email: str, recipient_type: str, subject: str, body: str, related_type: str, related_id: int):
    if not to_email:
        return

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    from_email = os.getenv("SMTP_FROM", username or "no-reply@example.local")

    status = "disabled"
    error = None
    if host and username and password:
        try:
            msg = EmailMessage()
            msg["From"] = from_email
            msg["To"] = to_email
            msg["Subject"] = subject
            msg.set_content(body)
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(username, password)
                smtp.send_message(msg)
            status = "sent"
        except Exception as exc:
            status = "failed"
            error = str(exc)

    conn.execute(
        """
        INSERT INTO email_logs
        (recipient_email, recipient_type, subject, body, status, error, related_type, related_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (to_email, recipient_type, subject, body, status, error, related_type, related_id),
    )


def notify_schedule_changed(conn, schedule_id: int):
    lesson = conn.execute(
        """
        SELECT s.weekday, s.planned_time, st.name AS student_name, st.email AS student_email,
               t.name AS teacher_name, t.email AS teacher_email, cp.course_name
        FROM schedules s
        JOIN course_packages cp ON cp.id = s.course_package_id
        JOIN students st ON st.id = cp.student_id
        JOIN teachers t ON t.id = cp.teacher_id
        WHERE s.id = ?
        """,
        (schedule_id,),
    ).fetchone()
    if not lesson:
        return
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    body = (
        f"课程时间已更新\n\n"
        f"学生：{lesson['student_name']}\n"
        f"课程：{lesson['course_name']}\n"
        f"老师：{lesson['teacher_name']}\n"
        f"固定时间：{weekdays[lesson['weekday']]} {lesson['planned_time']}\n"
    )
    send_email(conn, lesson["student_email"], "student", "课程时间更新", body, "schedule", schedule_id)
    send_email(conn, lesson["teacher_email"], "teacher", "课程时间更新", body, "schedule", schedule_id)


def notify_lesson_completed(conn, lesson_instance_id: int, balance_after: int):
    lesson = conn.execute(
        """
        SELECT st.name AS student_name, st.email AS student_email,
               t.name AS teacher_name, t.email AS teacher_email, cp.course_name, li.confirmed_at
        FROM lesson_instances li
        JOIN schedules s ON s.id = li.schedule_id
        JOIN course_packages cp ON cp.id = s.course_package_id
        JOIN students st ON st.id = cp.student_id
        JOIN teachers t ON t.id = cp.teacher_id
        WHERE li.id = ?
        """,
        (lesson_instance_id,),
    ).fetchone()
    if not lesson:
        return
    body = (
        f"课程已完成\n\n"
        f"学生：{lesson['student_name']}\n"
        f"课程：{lesson['course_name']}\n"
        f"老师：{lesson['teacher_name']}\n"
        f"确认时间：{lesson['confirmed_at']}\n"
        f"剩余课时：{balance_after} 节\n"
    )
    send_email(conn, lesson["student_email"], "student", "课程完成通知", body, "lesson_instance", lesson_instance_id)
    send_email(conn, lesson["teacher_email"], "teacher", "课程完成通知", body, "lesson_instance", lesson_instance_id)


def notify_low_balance_if_needed(conn, course_package_id: int, balance_after: int):
    raw = conn.execute("SELECT value FROM settings WHERE key = 'low_balance_thresholds'").fetchone()
    thresholds = parse_thresholds(raw["value"] if raw else "10,7,5,3,1")
    if balance_after not in thresholds:
        return
    package = conn.execute(
        """
        SELECT st.name AS student_name, st.email AS student_email,
               t.name AS teacher_name, t.email AS teacher_email, cp.course_name
        FROM course_packages cp
        JOIN students st ON st.id = cp.student_id
        JOIN teachers t ON t.id = cp.teacher_id
        WHERE cp.id = ?
        """,
        (course_package_id,),
    ).fetchone()
    if not package:
        return
    body = (
        f"课时余额提醒\n\n"
        f"学生：{package['student_name']}\n"
        f"课程：{package['course_name']}\n"
        f"老师：{package['teacher_name']}\n"
        f"当前剩余：{balance_after} 节\n"
    )
    send_email(conn, package["student_email"], "student", "课时余额提醒", body, "course_package", course_package_id)
    send_email(conn, package["teacher_email"], "teacher", "课时余额提醒", body, "course_package", course_package_id)


def parse_thresholds(value: str):
    thresholds = set()
    for part in value.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            thresholds.add(int(part))
    return thresholds


def create_reminder_logs(conn, target_weekday: int):
    rows = conn.execute(
        """
        SELECT s.id, s.weekday, s.planned_time, st.name AS student_name, st.email AS student_email,
               t.name AS teacher_name, t.email AS teacher_email, cp.course_name
        FROM schedules s
        JOIN course_packages cp ON cp.id = s.course_package_id
        JOIN students st ON st.id = cp.student_id
        JOIN teachers t ON t.id = cp.teacher_id
        WHERE s.active = 1 AND s.reminder_enabled = 1 AND s.weekday = ?
        """,
        (target_weekday,),
    ).fetchall()
    for row in rows:
        body = (
            f"明天课程提醒\n\n"
            f"学生：{row['student_name']}\n"
            f"课程：{row['course_name']}\n"
            f"老师：{row['teacher_name']}\n"
            f"时间：{row['planned_time']}\n"
        )
        send_email(conn, row["student_email"], "student", "明天课程提醒", body, "schedule", row["id"])
        send_email(conn, row["teacher_email"], "teacher", "明天课程提醒", body, "schedule", row["id"])
