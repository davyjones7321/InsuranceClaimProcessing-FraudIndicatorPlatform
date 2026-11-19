"""Password hashing and verification using Argon2id."""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.config import settings

# Initialize Argon2 password hasher (fast parameters during test)
if settings.ENV == "test":
    ph = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
else:
    ph = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a plaintext password using Argon2id after enforcing minimum length."""
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters")
    return ph.hash(password)


def verify_password(hash_str: str, password: str) -> bool:
    """Verify a password against an Argon2id hash."""
    try:
        return ph.verify(hash_str, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def needs_rehash(hash_str: str) -> bool:
    """Check if the hash requires rehashing due to parameter updates."""
    try:
        return ph.check_needs_rehash(hash_str)
    except Exception:
        return True
