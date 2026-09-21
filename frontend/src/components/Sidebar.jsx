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
  return (
    <aside className="side">
      <div className="brand"><span className="dot"></span>JobHunt<b>WOW</b></div>
      <nav className="nav">
        {items.concat(admin ? adminItems : []).map(([to, label, end]) => (
          <NavLink key={to} to={to} end={!!end}>{label}</NavLink>
        ))}
      </nav>
      <div style={{marginTop:24,fontSize:12,color:"var(--muted)"}}>v0.1 · agent preview</div>
    </aside>
  );
}
