"""Auth REST (P8): register/login -> JWT. Replaces the Chainlit mock password_auth_callback.

The frontend POSTs here on login, stores the returned access_token, then presents it on every
/sessions REST call (Authorization: Bearer) and the /ws connect (?token=). Login failures return a
generic message (no user enumeration). Generic infrastructure -> committed.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.api.deps import get_app
from src.auth.security import create_access_token, hash_password, verify_password

logger = logging.getLogger("estimate.api.auth")
router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    uid: str
    username: str


def _cfg():
    return get_app()["settings"].auth


def _token(user: dict) -> TokenOut:
    cfg = _cfg()
    access = create_access_token(user["id"], cfg.jwt_secret, cfg.jwt_algorithm, cfg.jwt_expire_minutes)
    return TokenOut(access_token=access, uid=user["id"], username=user["username"])


@router.post("/register", response_model=TokenOut)
async def register(body: Credentials) -> TokenOut:
    """Create a user (only when allow_registration is on) -> JWT. Otherwise 403 (use the CLI)."""
    cfg = _cfg()
    if not cfg.allow_registration:
        raise HTTPException(status_code=403, detail="registration disabled; create an account via the CLI")
    if not body.username or not body.password:
        raise HTTPException(status_code=400, detail="username and password are required")
    users = get_app()["users"]
    if await users.get_by_username(body.username):
        raise HTTPException(status_code=409, detail="username already exists")
    user = await users.create_user(body.username, hash_password(body.password))
    logger.info("registered user id=%s", user["id"])
    return _token(user)


@router.post("/login", response_model=TokenOut)
async def login(body: Credentials) -> TokenOut:
    """Verify username/password -> JWT. Generic 401 on any mismatch (avoids user enumeration)."""
    users = get_app()["users"]
    user = await users.get_by_username(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid username or password")
    return _token(user)
