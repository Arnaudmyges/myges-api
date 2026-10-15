"""Exceptions levées par le client."""

from __future__ import annotations


class MyGESError(Exception):
    """Base de toutes les erreurs du client."""


class AuthenticationError(MyGESError):
    """Échec de l'obtention d'un token."""


class InvalidCredentialsError(AuthenticationError):
    """Identifiant ou mot de passe refusé par Kordis."""


class BlockedRequestError(MyGESError):
    """Requête rejetée par le pare-feu applicatif de Kordis (403 HTML).

    Arrive notamment avec le User-Agent ``python-requests/*``.
    """


class TokenExpiredError(MyGESError):
    """Token refusé (401) et pas d'identifiants pour en obtenir un nouveau."""


class WriteNotAllowedError(MyGESError):
    """Opération en écriture appelée sans ``allow_writes=True``."""


class APIError(MyGESError):
    """Réponse d'erreur de l'API."""

    def __init__(self, status_code: int, path: str, message: str = "") -> None:
        self.status_code = status_code
        self.path = path
        self.message = message
        super().__init__(f"{status_code} sur {path}" + (f" : {message}" if message else ""))


class BadRequestError(APIError):
    """400 : paramètre manquant ou invalide."""


class NotFoundError(APIError):
    """404 : ressource ou route inconnue."""
