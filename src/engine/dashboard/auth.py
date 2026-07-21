"""HTTP Basic Auth for the dashboard. Single shared password, checked with
a constant-time comparison -- this guards a read-only reporting view, not
an account boundary, so one shared credential is proportionate. Refuses to
start rather than serving trade/prediction history unauthenticated."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from engine.config.settings import Settings, get_settings

_security = HTTPBasic()


def require_auth(
    credentials: HTTPBasicCredentials = Depends(_security),
    settings: Settings = Depends(get_settings),
) -> str:
    if not settings.dashboard_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DASHBOARD_PASSWORD is not set -- refusing to serve unauthenticated.",
        )
    correct = secrets.compare_digest(credentials.password, settings.dashboard_password)
    if not correct:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
