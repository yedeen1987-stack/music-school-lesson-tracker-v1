"""发送待发邮件队列。可用 systemd timer 或 cron 定时执行，作为应用内后台线程的兜底。"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import db_session
from app.emailer import process_email_queue
from app.models import SCHEMA_SQL
from app.services import init_schema


with db_session() as conn:
    init_schema(conn, SCHEMA_SQL)
    sent, failed = process_email_queue(conn, limit=200)
print(f"邮件队列处理完成：成功 {sent} 封，最终失败 {failed} 封。")
