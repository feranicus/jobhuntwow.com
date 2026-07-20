"""Electronic - user accounts (open self-signup, multi-tenant).

Differs from the Colt/cybergod model on purpose: there is NO allowlist and NO shared
password. Any email may sign up and sets its OWN password. A 6-digit OTP (see auth.py)
is still required, so knowing the password alone is not enough.

Storage: one sqlite file in DATA_DIR (same pattern as store.py).
Password hashing: stdlib hashlib.scrypt - no extra dependency, memory-hard, and the
parameters travel with the hash so they can be raised later without breaking old rows.

Stored form:  scrypt$<n>$<r>$<p>$<b64salt>$<b64hash>

NOTHING in this module ever logs a password or a hash.
"""
import os
import re
import time
import base64
import hmac
import hashlib
import sqlite3
import threading

from .settings import DATA_DIR

_LOCK = threading.Lock()
_PATH = os.path.join(str(DATA_DIR), "users.sqlite")

# scrypt parameters. n must be a power of 2. n=2**14, r=8, p=1 -> ~16 MB, ~50-100ms.
_N = 2 ** 14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16

MIN_PASSWORD_LEN = int(os.environ.get("MIN_PASSWORD_LEN", "10"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class UserError(Exception):
    """Raised for anything the caller may safely show to the user."""


# ---------------------------------------------------------------- helpers
def norm_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(norm_email(email)))


def password_problem(pw: str):
    """Return a human message if the password is unacceptable, else None."""
    if not isinstance(pw, str) or not pw:
        return "Password is required."
    if len(pw) < MIN_PASSWORD_LEN:
        return "Password must be at least %d characters." % MIN_PASSWORD_LEN
    if len(pw) > 1024:
        return "Password is too long."
    if pw.strip() == "":
        return "Password cannot be only whitespace."
    return None


def hash_password(pw: str) -> str:
    """scrypt$<n>$<r>$<p>$<b64salt>$<b64hash>"""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
                        dklen=_DKLEN, maxmem=_N * _R * 256 * 2)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _N, _R, _P,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(pw: str, stored: str) -> bool:
    """Constant-time compare against a stored scrypt string. Never raises."""
    try:
        parts = (stored or "").split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        expect = base64.b64decode(parts[5])
        dk = hashlib.scrypt((pw or "").encode("utf-8"), salt=salt, n=n, r=r, p=p,
                            dklen=len(expect), maxmem=n * r * 256 * 2)
        return hmac.compare_digest(dk, expect)
    except Exception:
        return False


# ---------------------------------------------------------------- storage
def _conn():
    os.makedirs(str(DATA_DIR), exist_ok=True)
    c = sqlite3.connect(_PATH)
    c.row_factory = sqlite3.Row
    return c


def _init():
    with _LOCK, _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at    INTEGER NOT NULL,
                verified_at   INTEGER,
                last_login    INTEGER
            )"""
        )
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")


_init()


def _row_to_user(r):
    if not r:
        return None
    return {
        "id": r["id"],
        "email": r["email"],
        "password_hash": r["password_hash"],
        "created_at": r["created_at"],
        "verified_at": r["verified_at"],
        "last_login": r["last_login"],
    }


def get_user(email: str):
    """Full row (includes password_hash - never return this to a client)."""
    e = norm_email(email)
    if not e:
        return None
    with _LOCK, _conn() as c:
        r = c.execute("SELECT * FROM users WHERE email = ?", (e,)).fetchone()
    return _row_to_user(r)


def public_user(email: str):
    """Safe subset for API responses."""
    u = get_user(email)
    if not u:
        return None
    return {"id": u["id"], "email": u["email"], "created_at": u["created_at"],
            "verified": bool(u["verified_at"]), "last_login": u["last_login"]}


def create_user(email: str, password: str):
    """Create an UNVERIFIED user. Raises UserError on bad input / duplicate."""
    e = norm_email(email)
    if not valid_email(e):
        raise UserError("Enter a valid email address.")
    problem = password_problem(password)
    if problem:
        raise UserError(problem)
    ph = hash_password(password)
    now = int(time.time())
    with _LOCK, _conn() as c:
        try:
            c.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (e, ph, now),
            )
        except sqlite3.IntegrityError:
            raise UserError("account_exists")
    return public_user(e)


def verify_login(email: str, password: str) -> bool:
    """True only if the user exists AND the password matches.
    Runs a dummy scrypt when the user is missing so timing does not leak existence."""
    u = get_user(email)
    if not u:
        verify_password(password or "", hash_password("timing-equaliser-not-a-secret"))
        return False
    return verify_password(password or "", u["password_hash"])


def mark_verified(email: str):
    e = norm_email(email)
    with _LOCK, _conn() as c:
        c.execute("UPDATE users SET verified_at = COALESCE(verified_at, ?) WHERE email = ?",
                  (int(time.time()), e))
    return public_user(e)


def touch_login(email: str):
    e = norm_email(email)
    with _LOCK, _conn() as c:
        c.execute("UPDATE users SET last_login = ? WHERE email = ?", (int(time.time()), e))
    return public_user(e)


def set_password(email: str, password: str):
    """Used by a future reset flow; hashing lives in exactly one place."""
    problem = password_problem(password)
    if problem:
        raise UserError(problem)
    e = norm_email(email)
    with _LOCK, _conn() as c:
        c.execute("UPDATE users SET password_hash = ? WHERE email = ?", (hash_password(password), e))
    return public_user(e)
