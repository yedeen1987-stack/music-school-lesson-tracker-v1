from __future__ import annotations

import os

from itsdangerous import BadSignature, URLSafeSerializer

# 生产环境务必通过 SESSION_SECRET 覆盖，否则任何人都能用仓库里的默认值伪造登录 Cookie。
SECRET_KEY = os.getenv("SESSION_SECRET", "local-dev-change-me")
serializer = URLSafeSerializer(SECRET_KEY, salt="music-school-session")


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
