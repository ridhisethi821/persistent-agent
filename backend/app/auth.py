import base64
import binascii
import hmac
import os

from fastapi import Request
from fastapi.responses import JSONResponse


USERNAME = "admin"


async def reviewer_auth(request: Request, call_next):
    # Keep Azure health checks publicly accessible.
    if request.url.path == "/health":
        return await call_next(request)

    expected_password = os.getenv("PASSWORD")

    # Fail closed if authentication hasn't been configured.
    if not expected_password:
        return JSONResponse(
            status_code=503,
            content={"detail": "Reviewer authentication is not configured."},
        )

    authorization = request.headers.get("Authorization", "")

    try:
        scheme, token = authorization.split(" ", 1)

        if scheme.lower() != "basic":
            raise ValueError("Invalid authentication scheme")

        decoded = base64.b64decode(token, validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)

        valid_username = hmac.compare_digest(username, USERNAME)
        valid_password = hmac.compare_digest(
            password.encode("utf-8"),
            expected_password.encode("utf-8"),
        )

        if valid_username and valid_password:
            return await call_next(request)

    except (ValueError, UnicodeDecodeError, binascii.Error):
        pass

    return JSONResponse(
        status_code=401,
        content={"detail": "Invalid reviewer credentials."},
        headers={
            "WWW-Authenticate": 'Basic realm="Persistent Agent Reviewer", charset="UTF-8"'
        },
    )
