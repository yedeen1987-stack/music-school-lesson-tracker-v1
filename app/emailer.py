from __future__ import annotations

import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from app.security import sign_student_token

# 第 n 次投递失败后，等待多久再重试。用完就标记为 failed。
RETRY_BACKOFF_MINUTES = (1, 5, 15)
MAX_ATTEMPTS = len(RETRY_BACKOFF_MINUTES) + 1


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def email_queue_summary(conn, now=None):
    now = now or datetime.now()
    bounds = ((now - timedelta(hours=24)).isoformat(timespec='seconds'), now.isoformat(timespec='seconds'))
    pending = conn.execute("SELECT COUNT(*) FROM email_logs WHERE status = 'pending'").fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM email_logs WHERE status = 'failed' AND last_attempt_at BETWEEN ? AND ?", bounds
    ).fetchone()[0]
    latest = conn.execute("SELECT MAX(last_attempt_at) FROM email_logs WHERE status = 'sent'").fetchone()[0]
    error = conn.execute(
        "SELECT error FROM email_logs WHERE status = 'failed' AND last_attempt_at BETWEEN ? AND ? ORDER BY last_attempt_at DESC, id DESC LIMIT 1", bounds
    ).fetchone()
    return {'pending': pending, 'failed': failed, 'last_success': latest, 'error': error[0] if error else None}


def smtp_config():
    host = os.getenv("SMTP_HOST")
    username = os.getenv("SMTP_USERNAME")
    password = os.getenv("SMTP_PASSWORD")
    if not (host and username and password):
        return None
    return {
        "host": host,
        "port": int(os.getenv("SMTP_PORT", "587")),
        "username": username,
        "password": password,
        "from_email": os.getenv("SMTP_FROM", username),
    }


def queue_email(conn, to_email: str, recipient_type: str, subject: str, body: str, related_type: str, related_id: int):
    """只入队，不发信。老师点“完成本节”时不会被 SMTP 阻塞。"""
    if not to_email:
        return
    conn.execute(
        """
        INSERT INTO email_logs
        (recipient_email, recipient_type, subject, body, status, related_type, related_id, attempts, next_attempt_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?, 0, ?)
        """,
        (to_email, recipient_type, subject, body, related_type, related_id, now_iso()),
    )


def deliver_email(config, to_email: str, subject: str, body: str):
    msg = EmailMessage()
    msg["From"] = config["from_email"]
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(config["host"], config["port"], timeout=15) as smtp:
        smtp.starttls()
        smtp.login(config["username"], config["password"])
        smtp.send_message(msg)


def process_email_queue(conn, limit: int = 20, sender=None):
    """把队列里到期的邮件发出去，失败按退避重试，返回 (成功数, 失败数)。"""
    config = smtp_config()
    now = now_iso()
    rows = conn.execute(
        """
        SELECT * FROM email_logs
        WHERE status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        ORDER BY id
        LIMIT ?
        """,
        (now, limit),
    ).fetchall()

    sent = 0
    failed = 0
    for row in rows:
        if not config:
            # 没配 SMTP：不真正发信，但保留记录，方便本地验证流程。
            conn.execute(
                "UPDATE email_logs SET status = 'disabled', last_attempt_at = ?, next_attempt_at = NULL WHERE id = ?",
                (now, row["id"]),
            )
            continue
        attempts = row["attempts"] + 1
        try:
            (sender or deliver_email)(config, row["recipient_email"], row["subject"], row["body"])
        except Exception as exc:
            if attempts >= MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE email_logs SET status = 'failed', error = ?, attempts = ?, last_attempt_at = ?, next_attempt_at = NULL WHERE id = ?",
                    (str(exc), attempts, now, row["id"]),
                )
                failed += 1
            else:
                delay = RETRY_BACKOFF_MINUTES[attempts - 1]
                retry_at = (datetime.now() + timedelta(minutes=delay)).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE email_logs SET error = ?, attempts = ?, last_attempt_at = ?, next_attempt_at = ? WHERE id = ?",
                    (str(exc), attempts, now, retry_at, row["id"]),
                )
        else:
            conn.execute(
                "UPDATE email_logs SET status = 'sent', error = NULL, attempts = ?, last_attempt_at = ?, next_attempt_at = NULL WHERE id = ?",
                (attempts, now, row["id"]),
            )
            sent += 1
    return sent, failed


def portal_url(conn, student_id: int) -> str:
    """学生自助查询链接。没设置 public_base_url 时返回空串，邮件里就不带链接。"""
    row = conn.execute("SELECT value FROM settings WHERE key = 'public_base_url'").fetchone()
    base = (row["value"] if row else "").strip().rstrip("/")
    return f"{base}/p/{sign_student_token(student_id)}" if base else ""


def portal_line(conn, student_id: int) -> str:
    url = portal_url(conn, student_id)
    return f"\n查看剩余课时和上课记录：{url}\n" if url else ""


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
    queue_email(conn, lesson["student_email"], "student", "课程时间更新", body, "schedule", schedule_id)
    queue_email(conn, lesson["teacher_email"], "teacher", "课程时间更新", body, "schedule", schedule_id)


def notify_lesson_completed(conn, lesson_instance_id: int, balance_after: int):
    lesson = conn.execute(
        """
        SELECT st.id AS student_id, st.name AS student_name, st.email AS student_email,
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
    queue_email(conn, lesson["student_email"], "student", "课程完成通知", body + portal_line(conn, lesson["student_id"]), "lesson_instance", lesson_instance_id)
    queue_email(conn, lesson["teacher_email"], "teacher", "课程完成通知", body, "lesson_instance", lesson_instance_id)


def read_thresholds(conn):
    raw = conn.execute("SELECT value FROM settings WHERE key = 'low_balance_thresholds'").fetchone()
    return parse_thresholds(raw["value"] if raw else "10,7,5,3,1")


def reset_balance_alerts(conn, course_package_id: int, balance: int):
    """余额上升（续费或撤销）时重置提醒状态。

    高于当前余额的阈值直接标记为“已处理”，避免刚买完 10 节就因为剩 9 节而报警；
    低于当前余额的阈值清空，等余额掉下去时可以重新提醒。
    """
    conn.execute("DELETE FROM balance_alerts WHERE course_package_id = ?", (course_package_id,))
    for threshold in sorted(read_thresholds(conn)):
        if threshold >= balance:
            conn.execute(
                "INSERT OR IGNORE INTO balance_alerts (course_package_id, threshold) VALUES (?, ?)",
                (course_package_id, threshold),
            )


def notify_low_balance_if_needed(conn, course_package_id: int, balance_after: int):
    """余额跌破任一阈值就提醒一次；跨过多个阈值只发一封，且不会因为错过精确数字而漏发。"""
    notified = {
        row["threshold"]
        for row in conn.execute(
            "SELECT threshold FROM balance_alerts WHERE course_package_id = ?", (course_package_id,)
        ).fetchall()
    }
    crossed = sorted(t for t in read_thresholds(conn) if balance_after <= t and t not in notified)
    if not crossed:
        return
    for threshold in crossed:
        conn.execute(
            "INSERT OR IGNORE INTO balance_alerts (course_package_id, threshold) VALUES (?, ?)",
            (course_package_id, threshold),
        )
    package = conn.execute(
        """
        SELECT st.id AS student_id, st.name AS student_name, st.email AS student_email,
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
    tail = "课时已用完，请及时续费。" if balance_after <= 0 else "建议及时续费，避免影响后续排课。"
    body = (
        f"课时余额提醒\n\n"
        f"学生：{package['student_name']}\n"
        f"课程：{package['course_name']}\n"
        f"老师：{package['teacher_name']}\n"
        f"当前剩余：{balance_after} 节\n\n"
        f"{tail}\n"
    )
    queue_email(conn, package["student_email"], "student", "课时余额提醒", body + portal_line(conn, package["student_id"]), "course_package", course_package_id)
    queue_email(conn, package["teacher_email"], "teacher", "课时余额提醒", body, "course_package", course_package_id)


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
        queue_email(conn, row["student_email"], "student", "明天课程提醒", body, "schedule", row["id"])
        queue_email(conn, row["teacher_email"], "teacher", "明天课程提醒", body, "schedule", row["id"])


def notify_renewal_requested(conn, renewal_id: int):
    """学生在自助页面点了“我要续费”，通知老师和管理员去跟进。"""
    row = conn.execute(
        """
        SELECT rr.balance_at_request, st.name AS student_name, st.phone, st.email AS student_email,
               t.name AS teacher_name, t.email AS teacher_email, cp.course_name
        FROM renewal_requests rr
        JOIN course_packages cp ON cp.id = rr.course_package_id
        JOIN students st ON st.id = cp.student_id
        JOIN teachers t ON t.id = cp.teacher_id
        WHERE rr.id = ?
        """,
        (renewal_id,),
    ).fetchone()
    if not row:
        return
    body = (
        f"学生提交了续费意向\n\n"
        f"学生：{row['student_name']}\n"
        f"课程：{row['course_name']}\n"
        f"老师：{row['teacher_name']}\n"
        f"电话：{row['phone'] or '未填写'}\n"
        f"邮箱：{row['student_email']}\n"
        f"提交时剩余：{row['balance_at_request']} 节\n\n"
        f"请在系统的“续费”页面跟进并登记新课时。\n"
    )
    queue_email(conn, row["teacher_email"], "teacher", "续费申请", body, "renewal_request", renewal_id)
