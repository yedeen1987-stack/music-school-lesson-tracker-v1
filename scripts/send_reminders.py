from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import db_session
from app.emailer import create_reminder_logs
from app.models import SCHEMA_SQL
from app.services import init_schema


tomorrow = date.today() + timedelta(days=1)
with db_session() as conn:
    init_schema(conn, SCHEMA_SQL)
    create_reminder_logs(conn, tomorrow.weekday())
print("提醒任务已执行，结果请查看 email_logs。")
