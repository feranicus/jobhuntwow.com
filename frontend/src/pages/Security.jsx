import { useCallback, useEffect, useState } from "react";
import { getJSON } from "../api.js";

/* Security — the operator's console. "You cannot protect something you cannot see."

   Every number here is read back out of the same event record the alert rules, the digest and
   Grafana consume. Nothing on this page acts: no block, no unblock, no threshold. A console that
   can enforce is a second enforcement path with none of the guardrails of the first.

   THE ONE RENDERING RULE: null is NOT zero. A source that could not be read paints an em dash and
   a reason, never a confident 0. "I could not look" and "there was nothing" are the same number
   and completely different facts, and a status page that cannot say "I do not know" will invent an
   answer — and the invented answer is always the reassuring one.

   Every value is coerced before it renders: a backend shape change must never white-screen the
   cabinet. */

const txt = (v) => (v === null || v === undefined ? "" : typeof v === "string" ? v : String(v));
const n = (v) => (v === null || v === undefined ? "—" : String(v));
const pct = (a, b) => (!b || a === null || a === undefined ? "" : Math.round((a / b) * 100) + "%");

/* Tone by state WORD, and every enum has an unknown. Unknown is amber, never green. */
const TONE = {
  active: "#16a34a", armed: "#16a34a", measured: "#16a34a",
  empty: "#64748b", none: "#b45309", off: "#b45309", stale: "#b45309",
  unknown: "#b45309", unverifiable: "#b45309", "not installed": "#dc2626",
};
const tone = (w) => TONE[String(w || "unknown")] || "#b45309";

const WINDOWS = [["1h", 1], ["24h", 24], ["7d", 168]];

function Chip({ label, word, why }) {
  return (
    <div title={txt(why)} style={{
      border: "1px solid var(--line, #26314a)", borderRadius: 10, padding: "8px 12px",
      minWidth: 150, background: "var(--panel, #0f1626)",
    }}>
      <div style={{ fontSize: 11, color: "var(--muted)", letterSpacing: .3 }}>{label}</div>
      <div style={{ fontWeight: 700, color: tone(word), textTransform: "lowercase" }}>
        {txt(word) || "unknown"}
      </div>
    </div>
  );
}

function Stat({ label, value, sub }) {
  return (
    <div style={{ minWidth: 118 }}>
      <div style={{ fontSize: 11, color: "var(--muted)" }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 800 }}>{n(value)}</div>
      {sub ? <div style={{ fontSize: 11, color: "var(--muted)" }}>{txt(sub)}</div> : null}
    </div>
  );
}

const th = { textAlign: "left", fontSize: 11, color: "var(--muted)", padding: "6px 8px",
             borderBottom: "1px solid var(--line, #26314a)", whiteSpace: "nowrap" };
const td = { padding: "5px 8px", fontSize: 12, borderBottom: "1px solid rgba(255,255,255,.04)",
             whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" };

export default function Security() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [hours, setHours] = useState(24);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const r = await getJSON("/api/security/overview?hours=" + hours);
      if (r && r.detail) { setErr(txt(r.detail)); setD(null); }
      else { setErr(""); setD(r); }
    } catch (e) {
      setErr("the console could not be loaded: " + txt(e && e.message));
    }
    setBusy(false);
  }, [hours]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const t = setInterval(load, 30000);        // the feed is only useful if it is current
    return () => clearInterval(t);
  }, [load]);

  if (err) {
    return (
      <div className="card" style={{ padding: 16 }}>
        <h2 style={{ marginTop: 0 }}>Security</h2>
        <p style={{ color: "#b45309" }}>{err}</p>
        <p style={{ color: "var(--muted)", fontSize: 13 }}>
          This page is administrator-only, and the check runs on the server for every request —
          hiding the menu entry would be presentation, not a control.
        </p>
      </div>
    );
  }
  if (!d) return <div style={{ color: "var(--muted)", padding: 16 }}>Reading the event record…</div>;

  const src = d.source || {};
  const llm = d.llm || {};
  const blind = !src.readable;

  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <h2 style={{ margin: 0 }}>Security</h2>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          window: {WINDOWS.map(([lbl, h]) => (
            <button key={h} type="button" className="btn ghost sm"
                    style={{ marginLeft: 6, opacity: hours === h ? 1 : .55 }}
                    onClick={() => setHours(h)}>{lbl}</button>
          ))}
        </span>
        <button type="button" className="btn ghost sm" onClick={load} disabled={busy}>
          {busy ? "reading…" : "refresh"}
        </button>
        <span style={{ marginLeft: "auto", color: "var(--muted)", fontSize: 11 }}>
          service {txt(d.service)} · generated {new Date((d.generated || 0) * 1000).toLocaleString()}
        </span>
      </div>

      {blind && (
        <div className="card" style={{ padding: 12, borderColor: "#b45309" }}>
          <b style={{ color: "#b45309" }}>BLIND, NOT CLEAN.</b>{" "}
          <span style={{ fontSize: 13 }}>{txt(src.why)}</span>
          <div style={{ fontSize: 12, color: "var(--muted)", marginTop: 4 }}>
            Every count below is <b>not measured</b> rather than zero. Nothing here is a statement
            about traffic until the event file can be read.
          </div>
        </div>
      )}

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <Chip label="SIDECAR" word={d.sidecar} why={d.sidecar_why} />
        <Chip label="ENFORCEMENT" word={d.enforce} why={d.enforce_why} />
        <Chip label="ALERTING" word={d.alerting} why={d.alerting_why} />
        <Chip label="VISITOR SPLIT" word={d.visitor_split}
              why="measured = the three buckets below come from real evidence" />
        <Chip label="AI METER" word={llm.healthy ? "active" : "unknown"}
              why={llm.healthy ? ("cap $" + txt(llm.cap_usd) + "/day, per account $"
                   + txt(llm.user_cap_usd)) : "the spend meter could not be read"} />
      </div>

      <div className="card" style={{ padding: 14, display: "flex", gap: 22, flexWrap: "wrap" }}>
        <Stat label="REQUESTS" value={d.requests} sub={"last " + txt(d.window_h) + "h"} />
        <Stat label="ADDRESSES" value={d.addresses} />
        <Stat label="VISITORS" value={d.visitors} sub={pct(d.visitors, d.addresses) + " of addresses"} />
        <Stat label="CLIENTS" value={d.clients} sub="scripts, bots, crawlers" />
        <Stat label="NOT DETERMINABLE" value={d.unjudged} sub="never guessed" />
        <Stat label="ATTACK-SHAPED" value={d.attacks} />
        <Stat label="REFUSED" value={d.refused} sub="401 / 403 / 429" />
        <Stat label="ALERTS" value={d.alerts_fired} sub={n(d.alerts_suppressed) + " suppressed"} />
        <Stat label="AI TODAY" value={llm.today_usd === null || llm.today_usd === undefined
              ? null : "$" + Number(llm.today_usd).toFixed(4)}
              sub={"cap $" + txt(llm.cap_usd) + " · " + n(llm.budget_refusals_window) + " refused"} />
      </div>

      <div className="card" style={{ padding: 14 }}>
        <h3 style={{ marginTop: 0 }}>Who is knocking, by hostname</h3>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr>
            <th style={th}>host</th><th style={th}>requests</th><th style={th}>addresses</th>
            <th style={th}>attack-shaped</th><th style={th}>refused</th>
          </tr></thead>
          <tbody>
            {Object.entries(d.hosts || {}).map(([h, row]) => (
              <tr key={h}>
                <td style={td}><b>{txt(h)}</b></td>
                <td style={td}>{n(row.requests)}</td>
                <td style={td}>{n(row.addresses)}</td>
                <td style={td}>{n(row.attacks)}</td>
                <td style={td}>{n(row.refused)}</td>
              </tr>
            ))}
            {!Object.keys(d.hosts || {}).length && (
              <tr><td style={td} colSpan={5}>
                {blind ? "not measured" : "no requests in this window"}
              </td></tr>
            )}
          </tbody>
        </table>
        <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 6 }}>
          jobhw.org is proxied to this application rather than redirected by the proxy, so the
          people who type the short domain appear here instead of vanishing upstream.
        </div>
      </div>

      <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit,minmax(320px,1fr))" }}>
        <div className="card" style={{ padding: 14 }}>
          <h3 style={{ marginTop: 0 }}>What they asked for</h3>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr><th style={th}>attack class</th><th style={th}>hits</th></tr></thead>
            <tbody>
              {Object.entries(d.attack_classes || {}).map(([k, v]) => (
                <tr key={k}><td style={td}>{txt(k)}</td><td style={td}>{n(v)}</td></tr>
              ))}
              {!Object.keys(d.attack_classes || {}).length && (
                <tr><td style={td} colSpan={2}>
                  {blind ? "not measured" : "nothing attack-shaped in this window"}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="card" style={{ padding: 14 }}>
          <h3 style={{ marginTop: 0 }}>Worst offenders</h3>
          <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 6 }}>
            Ranked by DISTINCT paths, not volume: a real visitor misses the same few stale pages, a
            scanner misses hundreds of different ones.
          </div>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>
              <th style={th}>address</th><th style={th}>distinct</th><th style={th}>hits</th>
              <th style={th}>country</th>
            </tr></thead>
            <tbody>
              {(d.top_offenders || []).map((o) => (
                <tr key={txt(o.ip)} title={(o.sample || []).map(txt).join("  ")}>
                  <td style={td}>{txt(o.ip)}</td>
                  <td style={td}>{n(o.distinct_paths)}</td>
                  <td style={td}>{n(o.hits)}</td>
                  <td style={td}>{txt(o.country)}</td>
                </tr>
              ))}
              {!(d.top_offenders || []).length && (
                <tr><td style={td} colSpan={4}>
                  {blind ? "not measured" : "nobody is probing right now"}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="card" style={{ padding: 14 }}>
          <h3 style={{ marginTop: 0 }}>What we did about it</h3>
          <div style={{ display: "flex", gap: 18, flexWrap: "wrap" }}>
            <Stat label="BLOCKED" value={(d.shield || {}).blocks} />
            <Stat label="WOULD BLOCK" value={(d.shield || {}).would_block} sub="detection only" />
            <Stat label="TARPITTED" value={(d.shield || {}).tarpits} />
            <Stat label="429 SENT" value={(d.shield || {}).refused_429} />
          </div>
          <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 8 }}>
            Everything the shield does expires by itself, an authenticated session is never blocked
            or slowed, and the ACME challenge and /api/ are never refused — a defence that locks a
            real person out, or turns a scanner into a certificate outage, costs more than it saves.
          </div>
        </div>

        <div className="card" style={{ padding: 14 }}>
          <h3 style={{ marginTop: 0 }}>The wallet</h3>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr><th style={th}>account</th><th style={th}>spend</th><th style={th}>calls</th></tr></thead>
            <tbody>
              {(llm.per_user || []).slice(0, 8).map((u) => (
                <tr key={txt(u.user)}>
                  <td style={td}>{txt(u.user)}</td>
                  <td style={td}>${Number(u.usd || 0).toFixed(4)}</td>
                  <td style={td}>{n(u.calls)}</td>
                </tr>
              ))}
              {!(llm.per_user || []).length && (
                <tr><td style={td} colSpan={3}>
                  {llm.healthy ? "no model calls recorded yet" : "the meter could not be read"}
                </td></tr>
              )}
            </tbody>
          </table>
          {(llm.unknown_models || []).length > 0 && (
            <div style={{ marginTop: 8, color: "#dc2626", fontSize: 12 }}>
              MODEL(S) NOBODY CONFIGURED: {(llm.unknown_models || []).map(txt).join(", ")} — this is
              the exact shape of the 2026-09 incident. Check the allowlist.
            </div>
          )}
        </div>
      </div>

      <div className="card" style={{ padding: 14 }}>
        <h3 style={{ marginTop: 0 }}>Live feed — the last {(d.feed || []).length} requests</h3>
        <div style={{ maxHeight: 420, overflow: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>
              <th style={th}>time</th><th style={th}>host</th><th style={th}>address</th>
              <th style={th}>country</th><th style={th}>method</th><th style={th}>path</th>
              <th style={th}>status</th><th style={th}>who</th><th style={th}>flag</th>
            </tr></thead>
            <tbody>
              {(d.feed || []).map((r, i) => (
                <tr key={i} title={txt(r.ua)}
                    style={{ background: r.attack ? "rgba(220,38,38,.09)" : "transparent" }}>
                  <td style={td}>{new Date((r.ts || 0) * 1000).toLocaleTimeString()}</td>
                  <td style={td}>{txt(r.host) || "—"}</td>
                  <td style={td}>{txt(r.ip)}</td>
                  <td style={td}>{txt(r.country)}</td>
                  <td style={td}>{txt(r.method)}</td>
                  <td style={{ ...td, maxWidth: 320 }}>{txt(r.path)}</td>
                  <td style={{ ...td, color: Number(r.status) >= 400 ? "#b45309" : "inherit" }}>
                    {n(r.status)}
                  </td>
                  <td style={td}>{txt(r.user) || (r.bot ? txt(r.bot_name) || "client" : "—")}</td>
                  <td style={td}>{txt(r.attack)}</td>
                </tr>
              ))}
              {!(d.feed || []).length && (
                <tr><td style={td} colSpan={9}>
                  {blind ? "not measured — the event file could not be read" : "nothing yet"}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 6 }}>
          Read from {txt(src.path) || "no configured event file"} · {n(src.lines_read)} line(s)
          scanned, {n(src.ours)} ours. {txt(d.caveat)}
        </div>
      </div>
    </div>
  );
}
