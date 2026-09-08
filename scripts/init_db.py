import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import db_session
from app.models import SCHEMA_SQL
from app.services import init_schema, seed_data


with db_session() as conn:
    init_schema(conn, SCHEMA_SQL)
    seed_data(conn)
print("数据库已初始化：data/music_school.sqlite3")
