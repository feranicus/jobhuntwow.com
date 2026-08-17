#!/usr/bin/env python3
"""ONE owner for ATS account credentials. out/ats_accounts.json is the single store.

WHY THIS MODULE EXISTS (measured defect, 2026-08-16):
`agent.py::apply_context()` minted a BRAND-NEW random password on EVERY run
    pw = "Jhw!" + secrets.token_urlsafe(10)... + "9"
and wrote it to out/credentials.json. So an account created last week could never be
signed into again -- the password that opened it was overwritten by the next run. At the
same time workday_wd.py owned a PERSISTENT per-tenant store (out/ats_accounts.json) that
apply_context() never consulted. One credential, two homes, and the ephemeral home won.

THE MODEL, per the candidate's instruction:
  * A password is INVENTED ONCE PER ATS VENDOR and remembered forever, so every Workday
    tenant (accenture.wd103, siemens.wd3, ...) is opened with the SAME password.
  * An ACCOUNT is still PER TENANT -- Workday accounts do not span employers. So the
    store keys accounts by tenant and keeps the vendor password under a reserved key.
  * Nothing is ever regenerated. `password_for()` is idempotent by construction.

SECURITY
  * The file lives under out/ (gitignored via data/ + *.env rules; see assert_ignored()).
  * chmod 600 on every write.
  * Never logged: use redact() for any human-readable output.
  * Never passed in argv (CLAUDE.md rule) -- callers read it in-process.
"""
import json
import os
import re
import secrets
import stat

OUT = os.getenv("JHW_OUT", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out"))
STORE = os.getenv("JHW_ATS_ACCOUNTS", os.path.join(OUT, "ats_accounts.json"))

# Reserved key inside the store. A tenant host can never collide with it: hosts always
# contain a dot, this never does.
_VENDOR_KEY = "_vendor_passwords"

# Symbols deliberately restricted. Excluded: quotes, backslash, pipe, backtick, angle
# brackets, semicolon -- they break shell/JSON round-trips and several ATS reject them,
# which would look like a wrong password rather than a rejected character.
_SYMBOLS = "!#$%&*+-=?@^_"
_LOWER = "abcdefghijkmnopqrstuvwxyz"      # no l
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"       # no I, O
_DIGIT = "23456789"                       # no 0, 1


def vendor_of(url: str) -> str:
    """ATS VENDOR (not employer) from a URL. The vendor owns the password."""
    u = (url or "").lower()
    for pat, name in (
        (r"myworkdayjobs\.com|myworkdaysite\.com|\.wd\d+\.", "workday"),
        (r"successfactors\.|sapsf\.|jobs\.sap\.com", "successfactors"),
        (r"oraclecloud\.com|taleo\.net", "oracle_hcm"),
        (r"icims\.com", "icims"),
        (r"greenhouse\.io", "greenhouse"),
        (r"lever\.co", "lever"),
        (r"myworkday\.com", "workday"),
        (r"phenom|\.phenompeople\.com", "phenom"),
        (r"workforcenow\.adp\.com|adp\.com", "adp"),
        (r"ultipro\.com|ukg\.", "ukg"),
        (r"smartrecruiters\.com", "smartrecruiters"),
        (r"eightfold\.ai", "eightfold"),
    ):
        if re.search(pat, u):
            return name
    # WHITE-LABELLED ATS CANNOT BE IDENTIFIED FROM A URL. Phenom, Eightfold and
    # SmartRecruiters are served from the EMPLOYER's own domain -- NTT's Phenom site is
    # careers.nttdata.com, which contains no vendor token at all. The same trap is already
    # recorded for phenom.is_phenom() being over-broad. So: never guess. Fall back to the
    # host, which FAILS SAFE (that employer simply gets its own password instead of sharing
    # the vendor's), and let a caller that has the DOM assert the vendor explicitly via
    # account(url, email, vendor=...).
    m = re.search(r"https?://([^/]+)", u)
    return (m.group(1) if m else "unknown").replace("www.", "")


def tenant_of(url: str) -> str:
    """The ACCOUNT boundary. accenture.wd103.myworkdayjobs.com -> accenture.wd103

    A Workday tenant serves several hostnames (the recruiting host and the candidate-site
    host), so the tenant -- not the hostname -- is the key, or the same account would be
    created twice for one employer."""
    m = re.search(r"https?://([^/]+)", (url or "").lower())
    host = m.group(1) if m else (url or "").lower()
    wd = re.match(r"^([a-z0-9\-]+)\.(wd\d+)\.", host)
    if wd:
        return f"{wd.group(1)}.{wd.group(2)}"
    return host


def generate_password(length: int = 20) -> str:
    """A password that satisfies every ATS complexity rule we have met: >=1 upper, >=1
    lower, >=1 digit, >=1 symbol, no ambiguous glyphs. Shuffled with secrets, never random."""
    if length < 12:
        length = 12
    pool = _LOWER + _UPPER + _DIGIT + _SYMBOLS
    while True:
        chars = [secrets.choice(_LOWER), secrets.choice(_UPPER),
                 secrets.choice(_DIGIT), secrets.choice(_SYMBOLS)]
        chars += [secrets.choice(pool) for _ in range(length - 4)]
        secrets.SystemRandom().shuffle(chars)
        pw = "".join(chars)
        # Re-assert the contract on the FINAL string rather than trusting the seeding.
        if (any(c in _LOWER for c in pw) and any(c in _UPPER for c in pw)
                and any(c in _DIGIT for c in pw) and any(c in _SYMBOLS for c in pw)):
            return pw


def load() -> dict:
    try:
        with open(STORE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save(d: dict) -> None:
    os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
    # In-place write: never mv/rename. A bind-mounted FILE pins its inode, and replacing it
    # leaves any container reading the stale one forever (the 2026-08-07 Caddy lesson).
    with open(STORE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2, sort_keys=True)
    try:
        os.chmod(STORE, stat.S_IRUSR | stat.S_IWUSR)   # 600
    except OSError:
        pass


def password_for(vendor: str) -> str:
    """Mint ONCE per vendor, then remember forever. Idempotent: calling this a hundred
    times returns the same string, which is the whole point of the fix."""
    d = load()
    bag = d.get(_VENDOR_KEY)
    if not isinstance(bag, dict):
        bag = {}
    pw = bag.get(vendor)
    if isinstance(pw, str) and pw.strip():
        return pw
    pw = generate_password()
    bag[vendor] = pw
    d[_VENDOR_KEY] = bag
    save(d)
    return pw


def find(url: str) -> tuple:
    """(key, record) for this URL, whichever key style it was stored under. ('', {}) if unknown.

    THE OPEN ITEM THIS CLOSES, measured 2026-08-17. `out/ats_accounts.json` held BOTH key styles:
    this module writes the canonical `accenture.wd103` (vendor tenant), while `workday.py` was writing
    the HOSTNAME `kyndryl.wd5.myworkdayjobs.com`. One fact, one file, two keys -- so `account()` looked
    up the tenant, found nothing, reported `exists: False` and handed back a NEWLY GENERATED password
    for a tenant that already had a working one. That is the "one value, several homes, the stale one
    wins" defect this project has paid for over and over, and it fails SILENTLY: the log says
    "no account recorded yet" while a good credential sits three lines away in the same file.
    Resolution order: the canonical tenant key, then the exact hostname, then any stored key whose own
    tenant resolves to the same tenant (which is how a legacy hostname entry is found)."""
    d = load()
    tenant = tenant_of(url)
    for k in (tenant, _host(url)):
        rec = d.get(k)
        if isinstance(rec, dict) and rec.get("password"):
            return k, rec
    for k, rec in d.items():
        if str(k).startswith("_") or not isinstance(rec, dict) or not rec.get("password"):
            continue
        if tenant_of("https://" + str(k)) == tenant:
            return k, rec
    return "", {}


def _host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", (url or "").lower())
    return (m.group(1) if m else (url or "").lower()).split("/")[0]


def host_view() -> dict:
    """`{hostname: record}` — the shape `workday.py` has always consumed.

    It exists so the six call sites in that adapter keep working unchanged while this module becomes
    the single owner. A record stored under a tenant key is exposed under every hostname alias we have
    seen for it, so a lookup by host can never miss a credential stored by tenant."""
    d, out = load(), {}
    for k, rec in d.items():
        if str(k).startswith("_") or not isinstance(rec, dict):
            continue
        out[k] = rec
        for h in (rec.get("aliases") or []):
            out.setdefault(h, rec)
    return out


def account(url: str, email: str, vendor: str = "") -> dict:
    """The credentials to use for this URL. Never regenerates.

    Returns {email, password, vendor, tenant, exists}. `exists` False means no account has
    been CONFIRMED created for this tenant yet, so the flow should try Sign In and fall
    back to Create Account -- it is a hint, never an assertion about the remote site."""
    # `vendor` is an explicit override for a white-labelled ATS the caller identified from
    # the DOM (see vendor_of). Unset -> derive from the URL.
    vendor, tenant = (vendor or vendor_of(url)), tenant_of(url)
    _k, rec = find(url)                     # ONE resolver, both key styles
    if rec and rec.get("password"):
        return {"email": rec.get("email") or email, "password": rec["password"],
                "vendor": vendor, "tenant": tenant, "exists": bool(rec.get("created"))}
    return {"email": email, "password": password_for(vendor),
            "vendor": vendor, "tenant": tenant, "exists": False}


def remember(url: str, email: str, password: str, created: bool = True) -> None:
    """Record this tenant's account under the CANONICAL key, and migrate a legacy hostname entry.

    Nothing is ever deleted: a hostname a caller has used before is kept in `aliases` so
    `host_view()` still finds it, and a legacy record's own fields are carried over rather than
    dropped. Destroying a real password to tidy a key is the worse trade -- already recorded in this
    project when a stale row was quarantined rather than removed."""
    d = load()
    tenant, host = tenant_of(url), _host(url)
    old_key, old = find(url)
    rec = dict(old) if old else {}
    rec.update({"email": email, "password": password, "created": bool(created),
                "vendor": vendor_of(url)})
    aliases = set(rec.get("aliases") or [])
    if host and host != tenant:
        aliases.add(host)
    if old_key and old_key not in (tenant,) and old_key != host:
        aliases.add(old_key)
    rec["aliases"] = sorted(aliases)
    if old_key and old_key != tenant:
        d.pop(old_key, None)                # migrated, not lost: it is in `aliases`
    d[tenant] = rec
    save(d)


def redact(s: str) -> str:
    """Every human-readable path (console, Telegram, events.log) goes through this."""
    if not s:
        return ""
    d = load()
    secretsl = [v for v in (d.get(_VENDOR_KEY) or {}).values() if isinstance(v, str) and v]
    secretsl += [r.get("password") for k, r in d.items()
                 if k != _VENDOR_KEY and isinstance(r, dict) and r.get("password")]
    out = s
    for p in sorted({p for p in secretsl if p}, key=len, reverse=True):
        out = out.replace(p, "***")
    return out


def assert_ignored() -> bool:
    """The store must never be committable. Proven, not assumed."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        with open(os.path.join(root, ".gitignore"), encoding="utf-8") as fh:
            pats = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
    except Exception:
        return False
    rel = os.path.relpath(STORE, root).replace(os.sep, "/")
    return any(p.rstrip("/") in rel.split("/") or rel.startswith(p.rstrip("/")) for p in pats)
