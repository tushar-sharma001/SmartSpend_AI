"""
Authentication.

Deliberately simple, matching the app's actual threat model (a single-user
local/demo deployment, not a multi-tenant production service): password
hashing via Werkzeug, a users table, and Streamlit's own session_state as
the session store. No JWTs, no OAuth, no server-side session database — see
the README's "Known Limitations" for exactly what that does and doesn't
give you, and what you'd add first for a real deployment.
"""

import re
import time
from dataclasses import dataclass
from typing import Optional

from werkzeug.security import check_password_hash, generate_password_hash

from src.config import get_logger
from src.database import DuplicateEmailError, create_user, get_user_by_email

logger = get_logger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8

# Lightweight, in-memory, single-process login throttle. Resets on app
# restart and doesn't survive multiple worker processes — a real deployment
# would move this to a shared store (e.g. Redis) behind a load balancer,
# but that's real infrastructure this project deliberately doesn't add.
_FAILED_ATTEMPTS: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 300  # 5 minutes


class AuthError(ValueError):
    """Raised for any user-facing auth failure — the message is always safe to display."""


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    name: str
    email: str


def _validate_registration(name: str, email: str, password: str, confirm_password: str) -> None:
    if not name.strip():
        raise AuthError("Name is required.")
    if not email.strip() or not _EMAIL_RE.match(email.strip()):
        raise AuthError("Please enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password != confirm_password:
        raise AuthError("Passwords do not match.")


def register(name: str, email: str, password: str, confirm_password: str) -> AuthenticatedUser:
    """Validate and create a new account. Raises AuthError with a message
    safe to show the user on any failure."""
    _validate_registration(name, email, password, confirm_password)
    try:
        user_id = create_user(name.strip(), email.strip(), generate_password_hash(password))
    except DuplicateEmailError:
        raise AuthError("That email is already registered.")
    logger.info("Registered new user id=%s", user_id)
    return AuthenticatedUser(id=user_id, name=name.strip(), email=email.strip().lower())


def _is_throttled(email: str) -> bool:
    now = time.time()
    attempts = [t for t in _FAILED_ATTEMPTS.get(email, []) if now - t < _WINDOW_SECONDS]
    _FAILED_ATTEMPTS[email] = attempts
    return len(attempts) >= _MAX_ATTEMPTS


def _record_failed_attempt(email: str) -> None:
    _FAILED_ATTEMPTS.setdefault(email, []).append(time.time())


def authenticate(email: str, password: str) -> AuthenticatedUser:
    """Verify credentials and return the authenticated user.

    Raises AuthError with a deliberately generic message on any failure —
    "Invalid email or password" whether the email doesn't exist or the
    password is wrong, so a login attempt can't be used to enumerate which
    emails are registered.
    """
    email = email.strip().lower()

    if _is_throttled(email):
        raise AuthError("Too many failed attempts. Please wait a few minutes and try again.")

    row = get_user_by_email(email)
    if row is None or not check_password_hash(row["password_hash"], password):
        _record_failed_attempt(email)
        raise AuthError("Invalid email or password.")

    _FAILED_ATTEMPTS.pop(email, None)
    return AuthenticatedUser(id=row["id"], name=row["name"], email=row["email"])
