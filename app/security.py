from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from itsdangerous import BadSignature, URLSafeSerializer

# 生产环境务必通过 SESSION_SECRET 覆盖，否则任何人都能用仓库里的默认值伪造登录 Cookie。
SECRET_KEY = os.getenv("SESSION_SECRET", "local-dev-change-me")
serializer = URLSafeSerializer(SECRET_KEY, salt="music-school-session")
student_serializer = URLSafeSerializer(SECRET_KEY, salt="music-school-student-portal")

PBKDF2_ROUNDS = 310_000


def sign_session(user_id: int) -> str:
    return serializer.dumps({"user_id": user_id})


def read_session(value: str | None) -> int | None:
    if not value:
        return None
    try:
        data = serializer.loads(value)
    except BadSignature:
        return None
    return data.get("user_id")


def sign_student_token(student_id: int) -> str:
    """学生查看自己课时的只读链接，不需要账号密码。"""
    return student_serializer.dumps({"student_id": student_id})


def read_student_token(value: str | None) -> int | None:
    if not value:
        return None
    try:
        data = student_serializer.loads(value)
    except BadSignature:
        return None
    return data.get("student_id")


def hash_password(password: str, salt: str | None = None, rounds: int = PBKDF2_ROUNDS) -> str:
    """用标准库的 PBKDF2-SHA256，不引入需要编译的依赖，自建服务器上装起来省事。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds).hex()
    return f"pbkdf2_sha256${rounds}${salt}${digest}"


def is_hashed(stored: str) -> bool:
    return stored.startswith("pbkdf2_sha256$")


def verify_password(password: str, stored: str) -> bool:
    if not is_hashed(stored):
        # 旧数据里的明文密码，登录时比对成功后由上层重新哈希写回。
        return hmac.compare_digest(password, stored)
    try:
        _, rounds, salt, _ = stored.split("$", 3)
        return hmac.compare_digest(hash_password(password, salt, int(rounds)), stored)
    except (ValueError, TypeError):
        return False
