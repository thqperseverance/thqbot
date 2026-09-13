"""FastAPI 依赖：当前登录用户。"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from . import repository
from .config import get_settings
from .db import get_db
from .models import User
from .security import load_session_token


def current_user(request: Request, session: Session = Depends(get_db)) -> User:
    """从签名 Cookie 解析当前用户；失败一律 401。

    注意：这里没有任何"回退到请求头"的分支 —— 旧网关的 trust-proxy 绕过在此不成立。
    """
    settings = get_settings()
    token = request.cookies.get(settings.session_cookie)
    user_id = load_session_token(settings.session_secret, token or "", max_age=settings.session_max_age)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    user = repository.get_user(session, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
    return user
