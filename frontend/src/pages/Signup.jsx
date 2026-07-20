import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { signup, verifyOtp } from "../api.js";
import { authStyles as S, stepColor, EMAIL_RE } from "./Login.jsx";

const MIN_PW = 10;

export default function Signup() {
  const nav = useNavigate();
  const [stage, setStage] = useState("form"); // form | otp
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [code, setCode] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  function clearErr() { if (err) setErr(""); }

  async function submitForm(e) {
    e.preventDefault();
    setErr(""); setMsg("");
    const mail = email.trim().toLowerCase();
    if (!EMAIL_RE.test(mail)) { setErr("Enter a valid email address."); return; }
    if (password.length < MIN_PW) { setErr(`Password must be at least ${MIN_PW} characters.`); return; }
    if (password !== confirm) { setErr("The two passwords do not match."); return; }
    setBusy(true);
    try {
      const { ok, data } = await signup(mail, password);
      if (ok && data.ok !== false) {
        setStage("otp");
        setCode("");
        setMsg(data.message || "Account created. We emailed you a 6-digit code — it expires in about 10 minutes.");
      } else {
        setErr(data.message || data.detail || "Could not create that account. It may already exist — try signing in.");
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

  const pwShort = password.length > 0 && password.length < MIN_PW;
  const pwMismatch = confirm.length > 0 && password !== confirm;

  return (
    <div style={S.wrap}>
      <div className="card" style={S.card}>
        <div style={S.brand}>
          <span style={S.dot}></span>JobHunt<b>WOW</b>
        </div>

        <div style={S.steps}>
          <span style={stepColor(true)}>1 · Create account</span>
          <i style={S.bar}></i>
          <span style={stepColor(stage === "otp")}>2 · Verify</span>
        </div>

        {stage === "form" ? (
          <form onSubmit={submitForm}>
            <h2 style={S.h}>Create your account</h2>
            <p style={S.sub}>One email, one password. We confirm it with a 6-digit code so nobody else can use your inbox.</p>

            <div className="label">Email</div>
            <input className="input" style={S.input} type="email" autoComplete="username"
              placeholder="you@example.com" value={email} autoFocus required
              onChange={(e) => { setEmail(e.target.value); clearErr(); }} />

            <div className="label">Password</div>
            <input className="input" style={S.input} type="password" autoComplete="new-password"
              placeholder={`At least ${MIN_PW} characters`} value={password} required
              onChange={(e) => { setPassword(e.target.value); clearErr(); }} />
            <div style={{ ...S.sub, margin: "6px 0 0", color: pwShort ? "var(--rose)" : "var(--muted)" }}>
              {pwShort ? `${MIN_PW - password.length} more character(s) needed.` : `Minimum ${MIN_PW} characters.`}
            </div>

            <div className="label">Confirm password</div>
            <input className="input" style={S.input} type="password" autoComplete="new-password"
              placeholder="Repeat your password" value={confirm} required
              onChange={(e) => { setConfirm(e.target.value); clearErr(); }} />
            {pwMismatch && (
              <div style={{ ...S.sub, margin: "6px 0 0", color: "var(--rose)" }}>The passwords do not match.</div>
            )}

            <button className="btn" style={S.btn} type="submit" disabled={busy}>
              {busy ? "Creating…" : "Create account →"}
            </button>
          </form>
        ) : (
          <form onSubmit={submitOtp}>
            <h2 style={S.h}>Confirm your email</h2>
            <p style={S.sub}>
              We emailed a 6-digit code to <b>{email.trim().toLowerCase()}</b>. It expires in about 10 minutes.
            </p>
            <div className="label">6-digit code</div>
            <input className="input" style={{ ...S.input, ...S.otp }}
              inputMode="numeric" pattern="[0-9]*" autoComplete="one-time-code"
              maxLength={6} placeholder="000000" value={code} autoFocus required
              onChange={(e) => { setCode(e.target.value.replace(/\D/g, "").slice(0, 6)); clearErr(); }} />
            <button className="btn" style={S.btn} type="submit" disabled={busy}>
              {busy ? "Verifying…" : "Verify & enter"}
            </button>
            <span style={S.link} role="button" tabIndex={0}
              onClick={() => { setStage("form"); setErr(""); setMsg(""); setCode(""); }}
              onKeyDown={(e) => { if (e.key === "Enter") { setStage("form"); setErr(""); setMsg(""); setCode(""); } }}>
              ← Use a different email
            </span>
          </form>
        )}

        {msg && <div style={S.ok}>{msg}</div>}
        {err && <div style={S.err}>{err}</div>}

        <Link style={S.link} to="/login">Already have an account? Sign in →</Link>
      </div>
    </div>
  );
}
