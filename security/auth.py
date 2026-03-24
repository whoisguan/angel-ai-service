"""Service-to-service authentication middleware.

Validates Service Token from calling systems (KPI, Odoo, etc.).
Parses X-User-Context header to extract user identity and permissions.
"""

import base64
import json

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer

from config import settings
from models.schemas import UserContext

security_scheme = HTTPBearer()


async def verify_service_token(
    request: Request,
    credentials=Depends(security_scheme),
) -> str:
    """Verify the Bearer token matches our service secret.

    In production, this should be HMAC-based with expiry.
    MVP: simple shared secret comparison.
    """
    import hmac
    if not hmac.compare_digest(credentials.credentials, settings.SERVICE_TOKEN_SECRET):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token",
        )
    return credentials.credentials


def parse_user_context(request: Request) -> UserContext:
    """Parse X-User-Context header into structured UserContext.

    The header value is a base64-encoded JSON string, set by the
    calling system (e.g. KPI backend) after authenticating the end user.
    """
    header_value = request.headers.get("X-User-Context")
    if not header_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-User-Context header",
        )

    try:
        decoded = base64.b64decode(header_value).decode("utf-8")
        data = json.loads(decoded)
        return UserContext(**data)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Invalid X-User-Context: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-User-Context header",
        )


async def get_authenticated_context(
    request: Request,
    _token: str = Depends(verify_service_token),
) -> UserContext:
    """Combined dependency: verify service token + parse user context."""
    return parse_user_context(request)
