from __future__ import annotations

from itsdangerous import BadSignature, URLSafeSerializer

SECRET_KEY = "local-dev-change-me"
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
