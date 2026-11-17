"""The one login for users and clients: a bearer token from ``.env``."""

import hmac

from fastapi import HTTPException, Request


def token_checker(token: str):
    expected = token.encode()

    def require_token(request: Request) -> None:
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        # Bytes, because compare_digest raises on non-ASCII text, and a client picks the text.
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), expected):
            raise HTTPException(
                status_code=401,
                detail="a bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token
