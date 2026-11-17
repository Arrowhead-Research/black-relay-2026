"""Static bearer token shared by all consumers; compared in constant time."""

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fusion_service.settings import Settings

bearer = HTTPBearer(auto_error=False)


class BearerTokenAuth:
    """A FastAPI dependency; install app-wide with `dependencies=[Depends(BearerTokenAuth(...))]`."""

    def __init__(self, expected: str):
        self._expected = expected.encode()

    @classmethod
    def from_settings(cls, settings: Settings) -> "BearerTokenAuth":
        return cls(settings.token().get_secret_value())

    def __call__(
        self, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> None:
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), self._expected
        ):
            raise HTTPException(
                401, "a bearer token is required", headers={"WWW-Authenticate": "Bearer"}
            )
