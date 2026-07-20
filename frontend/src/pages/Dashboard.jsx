import { useEffect, useState } from "react";
import { getJSON } from "../api.js";
import { Link } from "react-router-dom";

export default function Dashboard() {
  const [conn, setConn] = useState(null);
  const [health, setHealth] = useState(null);
  useEffect(() => {
    getJSON("/api/connections").then(setConn);
    getJSON("/api/health").then(setHealth);
  }, []);
  const c = conn || {};
  const status = (ok) => <span className={"pill " + (ok ? "on" : "off")}>{ok ? "connected" : "not set"}</span>;
  return (
    <>
      <h1 className="page-h">Dashboard</h1>
      <p className="page-sub">Your job hunt, run by Hermes. Connect your tools, scout roles, and let the agent apply — you approve every step.</p>
      <div className="banner">
        👋 This is v0.1. <b>Hermes chat runs on real Qwen</b> via your DigitalOcean key. LinkedIn scouting and the ATS Apply-Driver are wired as previews and will go live next.
      </div>
      <div className="cards">
        <div className="card"><h3>🧠 Qwen 3.x {status(c?.qwen?.connected)}</h3><p>Model: {c?.qwen?.model || "—"}. Inference: {health?.qwen_configured ? "key present on server" : "no key on server"}.</p></div>
        <div className="card"><h3>🪽 Hermes {status(c?.hermes?.connected)}</h3><p>The job-search agent. <Link to="/hermes">Open chat →</Link></p></div>
        <div className="card"><h3>💬 Telegram {status(c?.telegram?.connected)}</h3><p>Talk to Hermes from your phone. <Link to="/connections">Connect →</Link></p></div>
        <div className="card"><h3>🟢 WhatsApp {status(c?.whatsapp?.connected)}</h3><p>Same agent, on WhatsApp. <Link to="/connections">Connect →</Link></p></div>
        <div className="card"><h3>🔗 LinkedIn {status(c?.linkedin?.connected)}</h3><p>Scout roles from your network. <Link to="/connections">Connect →</Link></p></div>
        <div className="card"><h3>🚀 Quick start</h3><p><Link to="/scout">Scout jobs →</Link> · <Link to="/pipeline">Pipeline →</Link></p></div>
      </div>
    </>
  );
}
