import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import db_session, get_db_path
from app.models import SCHEMA_SQL
from app.services import init_schema


with db_session() as conn:
    init_schema(conn, SCHEMA_SQL)

print(f"数据库已初始化：{get_db_path()}")
