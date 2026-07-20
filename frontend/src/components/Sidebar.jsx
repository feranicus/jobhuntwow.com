import { NavLink } from "react-router-dom";
const items = [
  ["/", "🏠 Dashboard", true],
  ["/scout", "🔎 Job Scout"],
  ["/pipeline", "📋 Pipeline"],
  ["/hermes", "🪽 Hermes"],
  ["/connections", "🔌 Connections"],
];
export default function Sidebar() {
  return (
    <aside className="side">
      <div className="brand"><span className="dot"></span>JobHunt<b>WOW</b></div>
      <nav className="nav">
        {items.map(([to, label, end]) => (
          <NavLink key={to} to={to} end={!!end}>{label}</NavLink>
        ))}
      </nav>
      <div style={{marginTop:24,fontSize:12,color:"var(--muted)"}}>v0.1 · agent preview</div>
    </aside>
  );
}
