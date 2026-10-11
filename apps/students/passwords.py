"""One-time passwords for student logins (shown once, stored only as a hash)."""

import secrets

# No 0/O, 1/I: easy to read off a printed sheet.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH = 10


def generate_one_time_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
