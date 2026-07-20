"""Electronic - authentication: open self-signup + password + 6-digit OTP (2FA every login).

REUSED FROM colt_auth.py (proven in production):
  * 6-digit OTP via secrets.choice, hashed at rest, single-use, TTL, max-tries
  * failure counter -> lockout window
  * the Gmail API send path (service account + domain-wide delegation, HTTPS/443),
    because SMTP is blocked outbound on the droplet
DELIBERATELY NOT REUSED:
  * colt_auth.email_allowed() / the ALLOWLIST - Electronic is open self-signup
  * COLT_BOT_PASSWORD / the SHARED password - each customer sets their own
  * colt_auth's SMTP fallback - SMTP is blocked on the droplet, so a fallback that
    cannot work is dead code. Local dev uses AUTH_DEV_ECHO_OTP instead.
  * colt_auth.Auth's `uid` model (telegram user id) - here the email IS the identity.
colt_auth is NOT imported: it lives in the OTHER repo and importing it would couple this
project to Colt code, which the project rules forbid.

Sessions: signed with itsdangerous (URLSafeTimedSerializer), same as the cybergod webapp,
with a stdlib HMAC fallback so a missing dependency cannot take auth down.
"""
import os
import json
import time
import hmac
import base64
import hashlib
import secrets
import threading

from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel

from . import users

# ------------------------------------------------ config (env only, no secrets in code)
SESSION_SECRET  = os.environ.get("SESSION_SECRET", "")
SESSION_COOKIE  = os.environ.get("SESSION_COOKIE", "e_session")
SESSION_MAX_AGE = int(os.environ.get("SESSION_MAX_AGE", str(7 * 24 * 3600)))   # 7 days
COOKIE_SECURE   = os.environ.get("COOKIE_SECURE", "")     # "1"/"0" forces; empty = auto by host

OTP_TTL      = int(os.environ.get("OTP_TTL_SECS", "600"))       # 10 minutes
OTP_MAX      = int(os.environ.get("OTP_MAX_TRIES", "5"))
OTP_RESEND_S = int(os.environ.get("OTP_RESEND_SECS", "30"))     # min gap between codes
MAX_FAILS    = int(os.environ.get("AUTH_MAX_FAILS", "5"))
LOCK_SECS    = int(os.environ.get("AUTH_LOCK_SECS", "900"))     # 15 min lockout

GMAIL_SENDER   = os.environ.get("GMAIL_SENDER", "")
GMAIL_SA_B64   = os.environ.get("GMAIL_SA_B64", "")
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Electronic")

# Local dev only: return the code in the API response when no mailer is configured.
DEV_ECHO_OTP = os.environ.get("AUTH_DEV_ECHO_OTP", "").lower() in ("1", "true", "yes")

EVENTS_LOG = os.environ.get("EVENTS_LOG", "")
SERVICE    = os.environ.get("SERVICE", "electronic-backend")

if not SESSION_SECRET:
    # Never hard-fail (that takes the whole API down), but sessions must stay unforgeable:
    # use a random per-process secret. Cookies then die on restart - loud on purpose.
    SESSION_SECRET = secrets.token_urlsafe(48)
    print(json.dumps({"evt": "auth_config", "level": "warn", "service": SERVICE,
                      "msg": "SESSION_SECRET not set - ephemeral per-process secret in use; "
                             "sessions are invalidated on every restart"}), flush=True)


def _now():
    return time.time()


def log(**k):
    """One JSON event per line -> stdout AND EVENTS_LOG. Never a password, never a code."""
    k.setdefault("service", SERVICE)
    k.setdefault("ts", int(_now()))
    line = json.dumps(k)
    try:
        print(line, flush=True)
    except Exception:
        pass
    if EVENTS_LOG:
        try:
            with open(EVENTS_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ------------------------------------------------ OTP store + lockout (colt_auth's model)
# In-memory on purpose: a pending OTP is short-lived (10 min) and must NOT outlive a restart.
# Keyed by the normalised email, guarded by a lock (uvicorn runs a threadpool).
_PENDING = {}       # email -> {code, exp, tries, purpose, sent_at}
_FAILS = {}         # email -> [count, first_ts]
_OTP_LOCK = threading.Lock()


def _code_hash(code: str, email: str) -> str:
    """Codes are never stored in the clear. Salted with the email AND the session secret."""
    return hashlib.sha256(
        (str(code).strip() + "|" + email + "|" + SESSION_SECRET).encode("utf-8")
    ).hexdigest()


def locked_for(email: str) -> int:
    """Seconds remaining in the lockout window, 0 if not locked."""
    with _OTP_LOCK:
        c = _FAILS.get(email)
        if not c:
            return 0
        n, t0 = c
        if n >= MAX_FAILS and (_now() - t0) < LOCK_SECS:
            return int(LOCK_SECS - (_now() - t0))
        if (_now() - t0) >= LOCK_SECS:
            _FAILS.pop(email, None)
        return 0


def _fail(email: str):
    with _OTP_LOCK:
        c = _FAILS.get(email) or [0, _now()]
        c[0] += 1
        if c[0] == 1:
            c[1] = _now()
        _FAILS[email] = c


def _clear_fails(email: str):
    with _OTP_LOCK:
        _FAILS.pop(email, None)


def issue_otp(email: str, purpose: str):
    """Generate + store a 6-digit code. Returns (code, error); error is user-safe text."""
    with _OTP_LOCK:
        prev = _PENDING.get(email)
        if prev and (_now() - prev.get("sent_at", 0)) < OTP_RESEND_S:
            wait = int(OTP_RESEND_S - (_now() - prev["sent_at"]))
            return (None, "A code was just sent. Wait %ds before requesting another." % max(1, wait))
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        _PENDING[email] = {"code": _code_hash(code, email), "exp": _now() + OTP_TTL,
                           "tries": 0, "purpose": purpose, "sent_at": _now()}
    return (code, None)


def check_otp(email: str, code: str):
    """Returns (ok, message, purpose). Single-use, TTL-bounded, attempt-limited."""
    exhausted = False
    left = 0
    with _OTP_LOCK:
        p = _PENDING.get(email)
        if not p:
            return (False, "No verification in progress. Start again.", None)
        if _now() > p["exp"]:
            _PENDING.pop(email, None)
            return (False, "Code expired. Request a new one.", None)
        if p["tries"] >= OTP_MAX:
            _PENDING.pop(email, None)
            exhausted = True
        else:
            p["tries"] += 1
            if hmac.compare_digest(_code_hash(code, email), p["code"]):
                purpose = p["purpose"]
                _PENDING.pop(email, None)          # single use
                return (True, "verified", purpose)
            left = max(0, OTP_MAX - p["tries"])
    _fail(email)
    if exhausted:
        return (False, "Too many wrong codes. Start again.", None)
    return (False, "Incorrect code. %d attempt(s) left." % left, None)


# ------------------------------------------------ OTP delivery (Gmail API over HTTPS/443)
# SMTP is BLOCKED outbound on the droplet - this is the only path that works there.
# Service account with domain-wide delegation (scope gmail.send), impersonating GMAIL_SENDER.
# The SA JSON key is passed base64-encoded in the env: no secret file to mount, none in git.
def mailer_configured() -> bool:
    return bool(GMAIL_SENDER and GMAIL_SA_B64)


def _build_message(email: str, code: str) -> bytes:
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = "Your Electronic verification code"
    msg["From"] = "%s <%s>" % (MAIL_FROM_NAME, GMAIL_SENDER)
    msg["To"] = email
    msg.set_content(
        "Your one-time verification code is: %s\n\n"
        "It expires in %d minutes and can be used once.\n"
        "If you did not request this, ignore this email - nobody can sign in without it.\n\n"
        "- %s" % (code, OTP_TTL // 60, MAIL_FROM_NAME)
    )
    return msg.as_bytes()


def send_otp(email: str, code: str) -> bool:
    """True if the code was handed to Gmail. Never raises; never logs the code."""
    if not mailer_configured():
        log(evt="otp_send", result="not_configured", email=email)
        return False
    try:
        import httpx
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GRequest

        info = json.loads(base64.b64decode(GMAIL_SA_B64))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/gmail.send"], subject=GMAIL_SENDER)
        creds.refresh(GRequest())
        raw = base64.urlsafe_b64encode(_build_message(email, code)).decode()
        r = httpx.post(
            "https://gmail.googleapis.com/gmail/v1/users/%s/messages/send" % GMAIL_SENDER,
            headers={"Authorization": "Bearer " + creds.token,
                     "Content-Type": "application/json"},
            json={"raw": raw}, timeout=20.0)
        if r.status_code in (200, 202):
            log(evt="otp_send", result="ok", email=email)
            return True
        log(evt="otp_send", result="error", email=email, status=r.status_code, err=r.text[:200])
        return False
    except Exception as e:
        log(evt="otp_send", result="error", email=email, err=repr(e)[:200])
        return False


# ------------------------------------------------ signed session tokens
# Same construction as the cybergod webapp: itsdangerous URLSafeTimedSerializer, with a
# stdlib HMAC fallback so a missing wheel degrades instead of breaking authentication.
try:
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    _SER = URLSafeTimedSerializer(SESSION_SECRET, salt="electronic-session")

    def make_session(email: str) -> str:
        return _SER.dumps({"email": users.norm_email(email)})

    def read_session(token: str):
        try:
            data = _SER.loads(token, max_age=SESSION_MAX_AGE)
            return (data or {}).get("email")
        except (BadSignature, SignatureExpired):
            return None
        except Exception:
            return None

except Exception:      # itsdangerous missing - documented fallback, same guarantees
    def _sign(payload: str) -> str:
        sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + sig

    def make_session(email: str) -> str:
        body = base64.urlsafe_b64encode(json.dumps(
            {"email": users.norm_email(email), "ts": int(_now())}).encode()).decode()
        return _sign(body)

    def read_session(token: str):
        try:
            body, sig = (token or "").rsplit(".", 1)
            expect = hmac.new(SESSION_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expect, sig):
                return None
            data = json.loads(base64.urlsafe_b64decode(body.encode()))
            if int(_now()) - int(data.get("ts", 0)) > SESSION_MAX_AGE:
                return None
            return data.get("email")
        except Exception:
            return None


# ------------------------------------------------ cookie plumbing (credentials:'include' friendly)
def _is_secure(request: Request) -> bool:
    """Secure cookies everywhere except plain-http localhost dev (where they would be dropped)."""
    if COOKIE_SECURE in ("1", "true", "yes"):
        return True
    if COOKIE_SECURE in ("0", "false", "no"):
        return False
    host = (request.url.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def set_session_cookie(response: Response, request: Request, email: str):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=make_session(email),
        max_age=SESSION_MAX_AGE,
        httponly=True,          # JS can never read it -> XSS cannot steal the session
        samesite="lax",         # CSRF-safe for top-level navigation, works with fetch same-site
        secure=_is_secure(request),
        path="/",
    )


def clear_session_cookie(response: Response, request: Request):
    response.delete_cookie(key=SESSION_COOKIE, path="/", samesite="lax",
                           secure=_is_secure(request), httponly=True)


def current_user(request: Request):
    """The authenticated email, or None. THE single source of tenant identity."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    email = read_session(token)
    if not email:
        return None
    # A session for a deleted account must not keep working.
    if not users.get_user(email):
        return None
    return email


def require_user(request: Request) -> str:
    """FastAPI dependency: the email, or 401. Use this to scope EVERY tenant-owned route."""
    email = current_user(request)
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return email


def tenant_key(email: str) -> str:
    """Stable, filesystem-safe id for the tenant. Use for per-user dirs/table keys so the
    email itself never has to be pasted into a path."""
    return hashlib.sha256(users.norm_email(email).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------ the flow
GENERIC_LOGIN_ERR = "Invalid email or password."


def _deliver(email: str, purpose: str):
    """Issue + send an OTP. Returns a dict the endpoint can return verbatim.
    Raises HTTPException on lockout / rate-limit / mailer failure."""
    lk = locked_for(email)
    if lk:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in %ds." % lk)
    code, err = issue_otp(email, purpose)
    if err:
        raise HTTPException(status_code=429, detail=err)
    out = {"ok": True, "stage": "otp_sent", "email": email,
           "expires_in": OTP_TTL, "purpose": purpose}
    if send_otp(email, code):
        log(evt="auth", action=purpose, result="otp_sent", email=email)
        return out
    if DEV_ECHO_OTP:
        # Local dev without a mailer. NEVER enable in production - it exposes the 2nd factor.
        log(evt="auth", action=purpose, result="otp_dev_echo", email=email)
        out["dev_code"] = code
        out["note"] = "AUTH_DEV_ECHO_OTP is on - development only."
        return out
    log(evt="auth", action=purpose, result="otp_send_fail", email=email)
    raise HTTPException(status_code=502,
                        detail="Could not send the verification email. Try again shortly.")


def signup(email: str, password: str) -> dict:
    """Open self-signup: create an UNVERIFIED account, then email a 6-digit code."""
    e = users.norm_email(email)
    try:
        users.create_user(e, password)
    except users.UserError as ex:
        if str(ex) == "account_exists":
            existing = users.get_user(e)
            # A never-verified signup that repeats with the SAME password may finish the flow
            # instead of dead-ending. Any other case is told the account exists (unavoidable
            # in open self-signup: the UX cannot work without it).
            if existing and not existing["verified_at"] and \
                    users.verify_password(password, existing["password_hash"]):
                return _deliver(e, "signup")
            raise HTTPException(status_code=409,
                                detail="An account with that email already exists. Sign in instead.")
        raise HTTPException(status_code=400, detail=str(ex))
    log(evt="auth", action="signup", result="created", email=e)
    return _deliver(e, "signup")


def login(email: str, password: str) -> dict:
    """Password check, then a fresh OTP - 2FA on EVERY login."""
    e = users.norm_email(email)
    lk = locked_for(e)
    if lk:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in %ds." % lk)
    if not users.verify_login(e, password):
        _fail(e)
        log(evt="auth", action="login", result="bad_password", email=e)
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERR)
    return _deliver(e, "login")


def verify(email: str, code: str) -> str:
    """Second factor. Returns the email on success; raises HTTPException otherwise."""
    e = users.norm_email(email)
    lk = locked_for(e)
    if lk:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again in %ds." % lk)
    ok, msg, purpose = check_otp(e, code)
    if not ok:
        log(evt="auth", action="verify", result="bad_code", email=e)
        raise HTTPException(status_code=401, detail=msg)
    users.mark_verified(e)
    users.touch_login(e)
    _clear_fails(e)
    log(evt="auth", action="verify", result="ok", email=e, purpose=purpose)
    return e


# ------------------------------------------------ HTTP surface
router = APIRouter(prefix="/api", tags=["auth"])


class SignupReq(BaseModel):
    email: str
    password: str


class LoginReq(BaseModel):
    email: str
    password: str


class VerifyReq(BaseModel):
    email: str
    code: str


@router.post("/auth/signup")
def api_signup(req: SignupReq):
    """Open self-signup. Creates an unverified account and emails a 6-digit code."""
    return signup(req.email, req.password)


@router.post("/auth/login")
def api_login(req: LoginReq):
    """Password factor. On success emails a 6-digit code; no session yet."""
    return login(req.email, req.password)


@router.post("/auth/verify")
def api_verify(req: VerifyReq, request: Request, response: Response):
    """Second factor. On success the session cookie is set."""
    email = verify(req.email, req.code)
    set_session_cookie(response, request, email)
    return {"ok": True, "email": email, "user": users.public_user(email)}


@router.post("/auth/logout")
def api_logout(request: Request, response: Response):
    email = current_user(request)
    clear_session_cookie(response, request)
    if email:
        log(evt="auth", action="logout", result="ok", email=email)
    return {"ok": True}


@router.get("/me")
def api_me(request: Request):
    email = current_user(request)
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"email": email, "user": users.public_user(email)}
