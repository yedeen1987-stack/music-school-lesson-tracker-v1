from datetime import datetime, timedelta
from pathlib import Path
import os
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import db_session
from app.emailer import create_reminder_logs
from app.models import SCHEMA_SQL
from app.services import init_schema


def run_reminders(conn, now=None):
    """在配置的分钟入队；调用方负责提交，同日标记与邮件保持原子性。"""
    now = now or datetime.now()
    # 先取得写锁，防止两个任务同时检查到尚未执行。
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('last_reminder_run_date', '')")
    configured = conn.execute("SELECT value FROM settings WHERE key = 'reminder_time'").fetchone()
    if now.strftime('%H:%M') != (configured['value'] if configured else '19:00'):
        return False
    day = now.date().isoformat()
    claimed = conn.execute(
        "UPDATE settings SET value = ? WHERE key = 'last_reminder_run_date' AND value != ?",
        (day, day),
    )
    if not claimed.rowcount:
        return False
    create_reminder_logs(conn, (now.date() + timedelta(days=1)).weekday())
    return True


def main():
    if os.getenv('TZ') and hasattr(time, 'tzset'):
        time.tzset()
    with db_session() as conn:
        init_schema(conn, SCHEMA_SQL)
        queued = run_reminders(conn)
    print('提醒已入队。' if queued else '未到提醒时间或今天已执行，跳过。')


if __name__ == '__main__':
    main()
