"""Deterministic dependency identities for immutable Python runtimes."""
import hashlib


IDENTITY_DIGEST_LENGTH = 12
PYTHON_VERSION = [3, 14]
PYTHON_SOABI = "cpython-314-x86_64-linux-gnu"
PYTHON_MACHINE = "x86_64"

GUNICORN_VERSION = "26.2.0"
GUNICORN_WHEEL_NAME = "gunicorn-26.2.0-py3-none-any.whl"
GUNICORN_WHEEL_SHA256 = "bd249d0b3f7972f7432f0a6b6ff3b3ee2d129f70cd1ff6c09a9dd9e29a2b88e3"
GUNICORN_LOCK_TEXT = (
    "--only-binary=:all:\n"
    "gunicorn==26.2.0 \\\n"
    "    --hash=sha256:" + GUNICORN_WHEEL_SHA256 + "\n")

PILLOW_VERSION = "12.3.0"
PILLOW_WHEEL_NAME = (
    "pillow-12.3.0-cp314-cp314-manylinux_2_27_x86_64."
    "manylinux_2_28_x86_64.whl")
PILLOW_WHEEL_SHA256 = "251bf95b67017e27b13d82f5b326234ca62d70f9cf4c2b9032de2358a3b12c7b"
PILLOW_LOCK_TEXT = (
    "--only-binary=:all:\n"
    "Pillow==12.3.0 \\\n"
    "    --hash=sha256:" + PILLOW_WHEEL_SHA256 + "\n")


def lock_digest(lock_text):
    return hashlib.sha256(lock_text.encode()).hexdigest()


def content_addressed_name(prefix, lock_sha256):
    if (len(lock_sha256) != 64
            or any(value not in "0123456789abcdef" for value in lock_sha256)):
        raise ValueError("runtime identity requires a full lowercase SHA-256")
    return f"{prefix}-{lock_sha256[:IDENTITY_DIGEST_LENGTH]}"


GUNICORN_LOCK_SHA256 = lock_digest(GUNICORN_LOCK_TEXT)
PILLOW_LOCK_SHA256 = lock_digest(PILLOW_LOCK_TEXT)

# The predecessor was built before the repository-owned v2 lock identity. Its
# original immutable record remains independently verifiable and must never be
# reinterpreted as the current lock.
LEGACY_GUNICORN_LOCK_SHA256 = (
    "d76b3a3df43fa99d034d8d28de2a4feef5eb61ca5f1be8baf55df9e1f5d476b7")

LEGACY_GUNICORN_RUNTIME_NAME = "python3.14-gunicorn-26.2.0"
GUNICORN_RUNTIME_NAME = content_addressed_name(
    LEGACY_GUNICORN_RUNTIME_NAME, GUNICORN_LOCK_SHA256)

PILLOW_RUNTIME_NAME = content_addressed_name(
    "emoji-python3.14-pillow-12.3.0", PILLOW_LOCK_SHA256)
