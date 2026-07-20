import { useCallback, useEffect, useState } from "react";
import { Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import Sidebar from "./components/Sidebar.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import Connections from "./pages/Connections.jsx";
import Scout from "./pages/Scout.jsx";
import Pipeline from "./pages/Pipeline.jsx";
import Hermes from "./pages/Hermes.jsx";
import Login from "./pages/Login.jsx";
import Signup from "./pages/Signup.jsx";
import { me as fetchMe, logout as apiLogout } from "./api.js";

const splash = {
  minHeight: "100dvh", display: "grid", placeItems: "center",
  color: "var(--muted)", fontSize: 14, fontWeight: 600,
};
const topbar = {
  display: "flex", justifyContent: "flex-end", alignItems: "center",
  gap: 10, marginBottom: 14, fontSize: 13, color: "var(--muted)",
};

export default function App() {
  // undefined = still checking, null = anonymous, {email} = signed in
  const [user, setUser] = useState(undefined);
  const nav = useNavigate();
  const loc = useLocation();

  const check = useCallback(async () => {
    const u = await fetchMe();          // null on 401 / network error
    setUser(u && u.email ? u : null);
  }, []);

  // re-check on every navigation so a fresh session (after /login) is picked up
  useEffect(() => { check(); }, [check, loc.pathname]);

  async function doLogout() {
    try { await apiLogout(); } catch { /* clear locally regardless */ }
    setUser(null);
    nav("/login", { replace: true });
  }

  if (user === undefined) return <div style={splash}>Checking your session…</div>;

  const onAuthPage = loc.pathname === "/login" || loc.pathname === "/signup";

  // anonymous: only the auth screens are reachable
  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/signup" element={<Signup />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  // signed in: never sit on an auth screen
  if (onAuthPage) return <Navigate to="/" replace />;

  return (
    <div className="app">
      <Sidebar />
      <main className="main">
        <div style={topbar}>
          <span>{user.email}</span>
          <button className="btn ghost sm" type="button" onClick={doLogout}>Sign out</button>
        </div>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/scout" element={<Scout />} />
          <Route path="/pipeline" element={<Pipeline />} />
          <Route path="/hermes" element={<Hermes />} />
          <Route path="/connections" element={<Connections />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}
