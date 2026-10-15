from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_user
from ..models import User
from ..ratelimit import limit
from ..schemas import AccessToken, ClaimTokenOut, Credentials, LoginBody
from ..security import Principal, hash_secret, issue_token, verify_secret
from ..services import create_claim_token, iso

router = APIRouter()

_DUMMY = hash_secret("petmate-dummy-password")


@router.post("/auth/register", response_model=AccessToken, status_code=201,
             dependencies=[Depends(limit("auth", "rate_auth"))])
def register(body: Credentials, request: Request, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if db.scalar(select(User.id).where(func.lower(User.email) == email)) is not None:
        raise HTTPException(409, "email already registered")
    user = User(email=email, password_hash=hash_secret(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "email already registered")
    s = request.app.state.settings
    return AccessToken(access_token=issue_token(s.jwt_secret, "user", user.id, s.jwt_ttl_s))


@router.post("/auth/login", response_model=AccessToken,
             dependencies=[Depends(limit("auth", "rate_auth"))])
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    user = db.scalar(select(User).where(func.lower(User.email) == email))
    if user is None:
        verify_secret(body.password, _DUMMY)  # equalize timing
        raise HTTPException(401, "invalid credentials")
    if not verify_secret(body.password, user.password_hash):
        raise HTTPException(401, "invalid credentials")
    s = request.app.state.settings
    return AccessToken(access_token=issue_token(s.jwt_secret, "user", user.id, s.jwt_ttl_s))


@router.post("/claim-tokens", response_model=ClaimTokenOut, status_code=201)
def claim_token(request: Request, user: Principal = Depends(require_user), db: Session = Depends(get_db)):
    if db.get(User, user.user_id) is None:
        raise HTTPException(401, "unknown user")
    tok, exp = create_claim_token(db, user.user_id, request.app.state.settings.claim_ttl_s)
    return ClaimTokenOut(claim_token=tok, expires_at=iso(exp))
