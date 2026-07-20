export async function getJSON(path) {
  const r = await fetch(path, { credentials: "include" });
  return r.json();
}
export async function postJSON(path, body) {
  const r = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}
// stream Hermes chat: onChunk(text) called as tokens arrive
export async function chatStream(messages, model, onChunk) {
  const r = await fetch("/api/chat", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, model }),
  });
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    onChunk(dec.decode(value, { stream: true }));
  }
}

/* ---------------- auth ----------------
   POST /api/auth/signup {email,password} -> {ok:true} + OTP emailed
   POST /api/auth/login  {email,password} -> {ok:true} + OTP emailed
   POST /api/auth/verify {email,code}     -> httponly session cookie
   POST /api/auth/logout                  -> clears it
   GET  /api/me                           -> {email} or 401
   Every call carries the session cookie (credentials:'include').
   Auth helpers return {ok,status,data} so the UI can show real messages.  */
async function postAuth(path, body) {
  const r = await fetch(path, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch { data = {}; }
  return { ok: r.ok, status: r.status, data: data || {} };
}

export function signup(email, password) {
  return postAuth("/api/auth/signup", { email, password });
}
export function login(email, password) {
  return postAuth("/api/auth/login", { email, password });
}
export function verifyOtp(email, code) {
  return postAuth("/api/auth/verify", { email, code });
}
export function logout() {
  return postAuth("/api/auth/logout", {});
}
// resolves to {email} when signed in, or null on 401 / network error
export async function me() {
  try {
    const r = await fetch("/api/me", { credentials: "include" });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}
