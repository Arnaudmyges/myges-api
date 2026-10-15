"""Client Python pour l'API myGES / Kordis (non officielle)."""

from . import models
from ._version import __version__
from .auth import FileTokenStore, MemoryTokenStore, Token, TokenStore
from .client import AsyncMyGES, MyGES
from .errors import (
    APIError,
    AuthenticationError,
    BadRequestError,
    BlockedRequestError,
    InvalidCredentialsError,
    MyGESError,
    NotFoundError,
    TokenExpiredError,
    WriteNotAllowedError,
)
from .ics import to_ics

__all__ = [
    "APIError",
    "AsyncMyGES",
    "AuthenticationError",
    "BadRequestError",
    "BlockedRequestError",
    "FileTokenStore",
    "InvalidCredentialsError",
    "MemoryTokenStore",
    "MyGES",
    "MyGESError",
    "NotFoundError",
    "Token",
    "TokenExpiredError",
    "TokenStore",
    "WriteNotAllowedError",
    "__version__",
    "models",
    "to_ics",
]
