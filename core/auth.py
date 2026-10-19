"""Admin password hashing and JWT-style tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

import bcrypt

JWT_SECRET = os.getenv("ACCESS_PASS_SECRET", "access-pass-change-me")
TOKEN_HOURS = 12


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_token(username: str, role: str = "admin") -> str:
    payload = {
        "u": username,
        "r": role,
        "exp": int(time.time()) + TOKEN_HOURS * 3600,
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(JWT_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(token: str) -> str | None:
    try:
        body, sig = token.split(".", 1)
        expect = hmac.new(JWT_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=="))
        if int(payload.get("exp") or 0) < time.time():
            return None
        username = str(payload.get("u") or "")
        if not username:
            return None
        role = str(payload.get("r") or "admin")
        if role not in ("super", "admin", "leader"):
            role = "admin"
        return {"username": username, "role": role}
    except (ValueError, json.JSONDecodeError, TypeError):
        return None
