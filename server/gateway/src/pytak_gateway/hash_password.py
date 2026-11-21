"""The admin password: what every gateway call but GET /health is gated by.

It is a password the operator sets before deploy. Only its Argon2id hash (random salt, in the hash
string) goes into the gateway's environment as SITE_ADMIN_HASH, never the password itself:

Run "pytak-gw hash-password" to generate a hash.

With no SITE_ADMIN_HASH set, nobody can create, rotate or delete a site.
"""

import getpass
import os
import sys

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()  # Argon2id, the library's recommended cost, a fresh random salt per hash


def hash_password(password: str, hasher: PasswordHasher = _hasher) -> str:
    return hasher.hash(password)


def verify_password(encoded_hash: str | None, password: str) -> bool:
    """Slow on purpose (tens of ms): call it off the event loop. False for any failure, never raises."""
    if not encoded_hash:
        return False
    try:
        return _hasher.verify(encoded_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def load_admin_hash() -> str | None:
    return os.environ.get("SITE_ADMIN_HASH") or None


def prompt_and_print_hash() -> int:
    """`pytak-gw hash-password`: ask for the password twice and print the hash for .env."""
    first = getpass.getpass("Admin password: ")
    if not first or first != getpass.getpass("Repeat: "):
        print("Empty or mismatched password.", file=sys.stderr)
        return 1
    print(f"SITE_ADMIN_HASH='{hash_password(first)}'")
    return 0
