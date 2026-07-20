import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { login, verifyOtp } from "../api.js";

/* Local layout for the auth screens. styles.css owns the cabinet look
   (.card/.btn/.input/.label) — these only place the card on the page. */
export const authStyles = {
  wrap: {
    minHeight: "100dvh",
    display: "grid",
    gridTemplateColumns: "minmax(0,1fr)",
    placeItems: "center",
    padding: "24px 16px",
    background:
      "radial-gradient(1100px 520px at 10% -10%, rgba(2,132,199,.14), transparent 60%)," +
      "radial-gradient(900px 480px at 100% 0%, rgba(124,58,237,.12), transparent 60%)," +
      "var(--bg)",
  },
  card: { width: "100%", maxWidth: 420, padding: 26 },
  brand: {
    display: "flex", alignItems: "center", gap: 9, fontWeight: 700,
    fontSize: 20, marginBottom: 16, fontFamily: "'Space Grotesk',sans-serif",
  },
  dot: {
    width: 12, height: 12, borderRadius: 3,
    background: "linear-gradient(135deg,var(--sky),var(--blue))",
  },
  h: { margin: "0 0 4px", fontSize: 22 },
  sub: { margin: "0 0 8px", color: "var(--muted)", fontSize: 13.5, lineHeight: 1.5 },
  input: { fontSize: 16 },                       // >=16px: iOS must not zoom on focus
  otp: { fontSize: 24, letterSpacing: ".38em", textAlign: "center", fontWeight: 700 },
  btn: { width: "100%", justifyContent: "center", marginTop: 16 },
  link: {
    display: "block", marginTop: 14, textAlign: "center",
    color: "var(--muted)", fontSize: 13, fontWeight: 600, cursor: "pointer",
  },
  err: {
    marginTop: 14, background: "rgba(225,29,72,.09)", color: "var(--rose)",
    border: "1px solid rgba(225,29,72,.22)", borderRadius: 10,
    padding: "9px 12px", fontSize: 13, lineHeight: 1.45,
  },
  ok: {
    marginTop: 14, background: "rgba(22,163,74,.09)", color: "var(--green)",
    border: "1px solid rgba(22,163,74,.22)", borderRadius: 10,
    padding: "9px 12px", fontSize: 13, lineHeight: 1.45,
  },
  steps: {
    display: "flex", alignItems: "center", gap: 8, marginBottom: 14,
    fontSize: 11, fontWeight: 700, textTransform: "uppercase", letterSpacing: ".05em",
  },
  bar: { flex: 1, height: 2, background: "var(--line)", borderRadius: 2 },
};

export function stepColor(on) {
  return { color: on ? "var(--blue)" : "var(--muted)" };
}

export const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

export default function Login() {
  const nav = useNavigate();
  const [stage, setStage] = useState("creds"); // creds | otp
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submitCreds(e) {
    e.preventDefault();
    setErr(""); setMsg("");
    const mail = email.trim().toLowerCase();
    if (!EMAIL_RE.test(mail)) { setErr("Enter a valid email address."); return; }
    if (!password) { setErr("Enter your password."); return; }
    setBusy(true);
    try {
      const { ok, data } = await login(mail, password);
      if (ok && data.ok !== false) {
        setStage("otp");
        setCode("");
        setMsg(data.message || "We emailed you a 6-digit code. It expires in about 10 minutes.");
      } else {
        setErr(data.message || data.detail || "Wrong email or password.");
      }
    } catch {
      setErr("Could not reach the server. Try again.");
    } finally { setBusy(false); }
  }

  async function submitOtp(e) {
    e.preventDefault();
    setErr("");
    if (code.length !== 6) { setErr("Enter the 6-digit code from your inbox."); return; }
    setBusy(true);
    try {
      const { ok, data } = await verifyOtp(email.trim().toLowerCase(), code);
      if (ok && data.ok !== false) nav("/", { replace: true });
      else setErr(data.message || data.detail || "That code is invalid or expired.");
    } catch {
      setErr("Could not reach the server. Try again.");
    } finally { setBusy(false); }
  }

  return (
    <div style={authStyles.wrap}>
      <div className="card" style={authStyles.card}>
        <div style={authStyles.brand}>
          <span style={authStyles.dot}></span>JobHunt<b>WOW</b>
        </div>

        <div style={authStyles.steps}>
          <span style={stepColor(true)}>1 · Sign in</span>
          <i style={authStyles.bar}></i>
          <span style={stepColor(stage === "otp")}>2 · Verify</span>
        </div>

        {stage === "creds" ? (
          <form onSubmit={submitCreds}>
            <h2 style={authStyles.h}>Welcome back</h2>
            <p style={authStyles.sub}>Sign in to your cabinet. We send a one-time code to your inbox on every login.</p>
            <div className="label">Email</div>
            <input className="input" style={authStyles.input} type="email" autoComplete="username"
              placeholder="you@example.com" value={email} autoFocus required
              onChange={(e) => setEmail(e.target.value)} />
            <div className="label">Password</div>
            <input className="input" style={authStyles.input} type="password" autoComplete="current-password"
              placeholder="Your password" value={password} required
              onChange={(e) => setPassword(e.target.value)} />
            <button className="btn" style={authStyles.btn} type="submit" disabled={busy}>
              {busy ? "Sending code…" : "Continue →"}
            </button>
          </form>
        ) : (
          <form onSubmit={submitOtp}>
            <h2 style={authStyles.h}>Enter your code</h2>
            <p style={authStyles.sub}>
              We emailed a 6-digit code to <b>{email.trim().toLowerCase()}</b>. It expires in about 10 minutes.
            </p>
            <div className="label">6-digit code</div>
            <input className="input" style={{ ...authStyles.input, ...authStyles.otp }}
              inputMode="numeric" pattern="[0-9]*" autoComplete="one-time-code"
              maxLength={6} placeholder="000000" value={code} autoFocus required
              onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} />
            <button className="btn" style={authStyles.btn} type="submit" disabled={busy}>
              {busy ? "Verifying…" : "Verify & enter"}
            </button>
            <span style={authStyles.link} role="button" tabIndex={0}
              onClick={() => { setStage("creds"); setErr(""); setMsg(""); setCode(""); }}
              onKeyDown={(e) => { if (e.key === "Enter") { setStage("creds"); setErr(""); setMsg(""); setCode(""); } }}>
              ← Use a different account
            </span>
          </form>
        )}

        {msg && <div style={authStyles.ok}>{msg}</div>}
        {err && <div style={authStyles.err}>{err}</div>}

        <Link style={authStyles.link} to="/signup">No account yet? Create one →</Link>
      </div>
    </div>
  );
}
