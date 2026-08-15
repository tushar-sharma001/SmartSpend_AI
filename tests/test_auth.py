"""
Tests for authentication: registration validation, password hashing,
login success/failure, and the login throttle.
"""

import importlib
import time

import pytest


@pytest.fixture
def auth_modules(tmp_path, monkeypatch):
    """Fresh database + auth module per test, including auth's in-memory
    throttle state, so tests can't leak into each other."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SMARTSPEND_DB_PATH", str(db_path))

    import src.config as config_module
    importlib.reload(config_module)
    import src.database as database_module
    importlib.reload(database_module)
    import src.auth as auth_module
    importlib.reload(auth_module)

    database_module.init_db()
    return auth_module


def test_register_success(auth_modules):
    auth = auth_modules
    user = auth.register("Tushar", "tushar@example.com", "securepass1", "securepass1")
    assert user.name == "Tushar"
    assert user.email == "tushar@example.com"


@pytest.mark.parametrize(
    "name,email,password,confirm,expected_fragment",
    [
        ("", "a@b.com", "password1", "password1", "Name is required"),
        ("A", "not-an-email", "password1", "password1", "valid email"),
        ("A", "a@b.com", "short", "short", "at least 8 characters"),
        ("A", "a@b.com", "password1", "password2", "do not match"),
    ],
)
def test_register_validation_errors(auth_modules, name, email, password, confirm, expected_fragment):
    auth = auth_modules
    with pytest.raises(auth.AuthError, match=expected_fragment):
        auth.register(name, email, password, confirm)


def test_register_rejects_duplicate_email(auth_modules):
    auth = auth_modules
    auth.register("Tushar", "tushar@example.com", "securepass1", "securepass1")
    with pytest.raises(auth.AuthError, match="already registered"):
        auth.register("Someone Else", "tushar@example.com", "otherpass1", "otherpass1")


def test_password_is_hashed_not_stored_plaintext(auth_modules):
    auth = auth_modules
    from src.database import get_user_by_email

    auth.register("Tushar", "tushar@example.com", "securepass1", "securepass1")
    row = get_user_by_email("tushar@example.com")
    assert row["password_hash"] != "securepass1"
    assert row["password_hash"].startswith("scrypt:") or ":" in row["password_hash"]


def test_login_with_correct_password_succeeds(auth_modules):
    auth = auth_modules
    registered = auth.register("Tushar", "tushar@example.com", "securepass1", "securepass1")
    logged_in = auth.authenticate("tushar@example.com", "securepass1")
    assert logged_in.id == registered.id


def test_login_with_wrong_password_fails_generically(auth_modules):
    auth = auth_modules
    auth.register("Tushar", "tushar@example.com", "securepass1", "securepass1")
    with pytest.raises(auth.AuthError, match="Invalid email or password"):
        auth.authenticate("tushar@example.com", "wrongpassword")


def test_login_with_nonexistent_email_fails_with_same_message(auth_modules):
    """The error message must not reveal whether the email exists at all."""
    auth = auth_modules
    with pytest.raises(auth.AuthError, match="^Invalid email or password\\.$"):
        auth.authenticate("nobody@example.com", "whatever123")


def test_login_email_is_case_insensitive(auth_modules):
    auth = auth_modules
    auth.register("Tushar", "Tushar@Example.com", "securepass1", "securepass1")
    logged_in = auth.authenticate("tushar@EXAMPLE.com", "securepass1")
    assert logged_in.email == "tushar@example.com"


def test_login_throttle_blocks_after_repeated_failures(auth_modules):
    auth = auth_modules
    auth.register("Tushar", "tushar@example.com", "securepass1", "securepass1")

    for _ in range(auth._MAX_ATTEMPTS):
        with pytest.raises(auth.AuthError):
            auth.authenticate("tushar@example.com", "wrongpassword")

    # even the CORRECT password should now be blocked by the throttle
    with pytest.raises(auth.AuthError, match="Too many failed attempts"):
        auth.authenticate("tushar@example.com", "securepass1")


def test_login_throttle_resets_after_success(auth_modules):
    auth = auth_modules
    auth.register("Tushar", "tushar@example.com", "securepass1", "securepass1")

    for _ in range(auth._MAX_ATTEMPTS - 1):
        with pytest.raises(auth.AuthError):
            auth.authenticate("tushar@example.com", "wrongpassword")

    # one attempt left before throttling — a correct login here should
    # succeed AND clear the failure count
    auth.authenticate("tushar@example.com", "securepass1")

    # so a subsequent single wrong attempt should fail with the normal
    # message, not the throttle message
    with pytest.raises(auth.AuthError, match="Invalid email or password"):
        auth.authenticate("tushar@example.com", "wrongpassword")
