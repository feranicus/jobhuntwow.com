import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
const items = [
  ["/", "🏠 Dashboard", true],
  ["/scout", "🔎 Job Scout"],
  ["/pipeline", "📋 Pipeline"],
  ["/electronic", "🪽 Electronic"],
  ["/tailor", "📄 Tailor"],
  ["/connections", "🔌 Connections"],
];
// Shown only to an administrator - and that is PRESENTATION. The control is auth.require_admin on
// the route itself, which runs on the server on every request; anyone can type the URL.
const adminItems = [["/security", "🛡 Security"]];
export default function Sidebar({ admin = false }) {
  const [build, setBuild] = useState("");
  useEffect(() => {
    let gone = false;
    fetch("/api/health", { credentials: "include" })
      .then(r => r.json())
      .then(d => { if (!gone && d && d.build) setBuild(String(d.build)); })
      .catch(() => { /* the stamp is a convenience, never a reason to break the sidebar */ });
    return () => { gone = true; };
  }, []);
  return (
    <aside className="side">
      <div className="brand"><span className="dot"></span>JobHunt<b>WOW</b></div>
      <nav className="nav">
        {items.concat(admin ? adminItems : []).map(([to, label, end]) => (
          <NavLink key={to} to={to} end={!!end}>{label}</NavLink>
        ))}
      </nav>
      {/* WHICH BUILD AM I LOOKING AT. Written into the image by the deploy and read back from
          /api/health. He looked for two features that had been written but not yet shipped and had
          no way to tell — this is that way. "unknown" when the image carries no stamp; never a
          guess. */}
      <div style={{marginTop:24,fontSize:12,color:"var(--muted)"}} title={build || ""}>
        v0.1 · agent preview{build ? <><br/>build {build}</> : null}
      </div>
    </aside>
  );
}
