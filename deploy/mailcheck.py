"""mailcheck.py — runs INSIDE the jhw-web container. Says exactly WHY Gmail refuses.

Never prints the private key or the OTP. Prints the service-account identity, whether the
token exchange (domain-wide delegation) works, and the raw Gmail API error if the send fails.
"""
import base64, json, os, sys

SENDER = os.environ.get("GMAIL_SENDER", "")
SA_B64 = os.environ.get("GMAIL_SA_B64", "")
TO = sys.argv[1] if len(sys.argv) > 1 else SENDER

print("  GMAIL_SENDER      :", SENDER or "(EMPTY - nothing will send)")
print("  GMAIL_SA_B64 set  :", "yes (%d chars)" % len(SA_B64) if SA_B64 else "NO")
if not (SENDER and SA_B64):
    print("  VERDICT: mailer not configured — set both in .env, then redeploy.")
    sys.exit(1)

try:
    info = json.loads(base64.b64decode(SA_B64))
except Exception as e:
    print("  VERDICT: GMAIL_SA_B64 is not valid base64 JSON:", repr(e)[:200])
    sys.exit(1)

print("  SA client_email   :", info.get("client_email", "?"))
print("  SA client_id      :", info.get("client_id", "?"))
print("  SA project_id     :", info.get("project_id", "?"))
print("  SA type           :", info.get("type", "?"))

from google.oauth2 import service_account
from google.auth.transport.requests import Request as GRequest

SCOPE = "https://www.googleapis.com/auth/gmail.send"
try:
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[SCOPE], subject=SENDER)
    creds.refresh(GRequest())
    print("  token exchange    : OK (domain-wide delegation is working for", SENDER + ")")
except Exception as e:
    msg = repr(e)
    print("  token exchange    : FAILED")
    print("  raw error         :", msg[:400])
    if "unauthorized_client" in msg:
        print("""
  VERDICT: Google refused the impersonation ("unauthorized_client").
  The service account exists, but it is NOT authorised to act as %s.
  FIX (Google Workspace admin, one time):
    admin.google.com -> Security -> Access and data control -> API controls
      -> Domain-wide delegation -> Add new
      Client ID: %s
      OAuth scopes: %s
  NOTE: domain-wide delegation only works for a GOOGLE WORKSPACE domain you administer.
  A plain @gmail.com sender can NEVER be impersonated this way — that is a hard Google limit,
  not a bug in this app.""" % (SENDER, info.get("client_id", "?"), SCOPE))
    elif "invalid_grant" in msg:
        print("""
  VERDICT: "invalid_grant" — usually one of:
    * %s does not exist in the Workspace the SA is delegated for,
    * the delegation was added for a DIFFERENT scope, or
    * the droplet clock is skewed (check `date -u`).""" % SENDER)
    sys.exit(1)

import httpx
from email.message import EmailMessage
m = EmailMessage()
m["Subject"] = "JobHuntWOW mail self-test"
m["From"] = SENDER
m["To"] = TO
m.set_content("If you can read this, OTP delivery works.")
raw = base64.urlsafe_b64encode(m.as_bytes()).decode()
r = httpx.post("https://gmail.googleapis.com/gmail/v1/users/%s/messages/send" % SENDER,
               headers={"Authorization": "Bearer " + creds.token,
                        "Content-Type": "application/json"},
               json={"raw": raw}, timeout=20.0)
print("  send status       :", r.status_code)
if r.status_code in (200, 202):
    print("  VERDICT: SENT to", TO, "- check that inbox (and spam).")
else:
    print("  raw API response  :", r.text[:400])
    print("  VERDICT: token was fine but the SEND was refused (see the error above).")
