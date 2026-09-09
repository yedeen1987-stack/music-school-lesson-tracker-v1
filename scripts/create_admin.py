import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import db_session, get_db_path
from app.models import SCHEMA_SQL
from app.security import hash_password
from app.services import ensure_default_settings, init_schema


def main():
    with db_session() as conn:
        init_schema(conn, SCHEMA_SQL)
        ensure_default_settings(conn)

        existing_admin = conn.execute(
            "SELECT username FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone()

        if existing_admin:
            print(f"管理员已存在：{existing_admin['username']}")
            print("未做任何修改。")
            return 1

        username = input("管理员用户名: ").strip()
        if not username:
            print("错误：用户名不能为空。")
            return 1

        existing_user = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if existing_user:
            print("错误：该用户名已经存在。")
            return 1

        password = getpass.getpass("管理员密码（至少 8 位）: ")
        if len(password) < 8:
            print("错误：密码至少需要 8 位。")
            return 1

        confirmation = getpass.getpass("再次输入管理员密码: ")
        if password != confirmation:
            print("错误：两次输入的密码不一致。")
            return 1

        conn.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, 'admin')",
            (username, hash_password(password)),
        )

    print(f"管理员创建成功：{username}")
    print(f"数据库：{get_db_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
