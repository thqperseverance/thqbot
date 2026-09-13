"""登录 / 登出 / 当前用户。

单应用自带鉴权（用户确认项：网关自带极简登录）。
Cookie 为 HttpOnly + SameSite=Lax 的签名令牌；跨站 POST 不会携带该 Cookie，
因此不再需要单独的 CSRF 令牌。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from .. import repository
from ..config import get_settings
from ..db import get_db
from ..deps import current_user
from ..models import User
from ..schemas import LoginRequest, LoginResponse, UserOut
from ..security import sign_session_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_out(user: User) -> UserOut:
    return UserOut(user_id=user.id, username=user.username, display_name=user.display_name or user.username)


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, response: Response, session: Session = Depends(get_db)) -> LoginResponse:
    settings = get_settings()
    user = repository.get_user_by_username(session, payload.username)
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用")

    token = sign_session_token(settings.session_secret, user.id)
    response.set_cookie(
        key=settings.session_cookie,
        value=token,
        max_age=settings.session_max_age,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return LoginResponse(user=_user_out(user))


@router.post("/logout")
def logout(response: Response) -> dict[str, str]:
    settings = get_settings()
    response.delete_cookie(key=settings.session_cookie, path="/")
    return {"status": "ok"}


@router.get("/me", response_model=LoginResponse)
def me(user: User = Depends(current_user)) -> LoginResponse:
    return LoginResponse(user=_user_out(user))
