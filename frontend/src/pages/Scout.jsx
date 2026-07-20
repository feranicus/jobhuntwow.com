import { useState } from "react";
import { postJSON } from "../api.js";

export default function Scout() {
  const [q, setQ] = useState("data engineer");
  const [loc, setLoc] = useState("Berlin");
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [applyLog, setApplyLog] = useState(null);

  const run = async () => { setBusy(true); try { setRes(await postJSON("/api/scout", { query: q, location: loc })); } finally { setBusy(false); } };
  const apply = async (job) => {
    const first = await postJSON("/api/apply", { job_id: job.id, confirm: false });
    if (first.status === "needs_confirmation" && confirm(`Confirm & submit application to ${job.company} — ${job.role}?`)) {
      setApplyLog(await postJSON("/api/apply", { job_id: job.id, confirm: true }));
    }
  };

  return (
    <>
      <h1 className="page-h">Job Scout</h1>
      <p className="page-sub">Find roles behind real ATS (Workday, SuccessFactors, Personio…) — not just Easy Apply.</p>
      <div className="row" style={{marginBottom:16}}>
        <input className="input" style={{maxWidth:260}} value={q} onChange={e => setQ(e.target.value)} placeholder="role / keywords" />
        <input className="input" style={{maxWidth:200}} value={loc} onChange={e => setLoc(e.target.value)} placeholder="location" />
        <button className="btn" onClick={run} disabled={busy}>{busy ? "Scouting…" : "Scout jobs"}</button>
      </div>
      {res && <p className="page-sub">{res.count} roles · <span className="tag">{res.note}</span></p>}
      {res?.jobs?.map(j => (
        <div key={j.id} className="jobrow">
          <div>
            <b>{j.role}</b> <span className="tag">{j.ats}</span><br/>
            <small style={{color:"var(--muted)"}}>{j.company} · {j.location}</small>
          </div>
          <div className="row">
            <span className="fit">fit {j.fit}</span>
            <button className="btn sm ghost" onClick={() => apply(j)}>Apply →</button>
          </div>
        </div>
      ))}
      {applyLog && (
        <div className="card" style={{marginTop:14}}>
          <h3>✅ {applyLog.status} — {applyLog.job_id}</h3>
          <ol style={{margin:"8px 0 0 18px",color:"var(--muted)",fontSize:13.5}}>
            {applyLog.steps?.map((s,i) => <li key={i} style={{margin:"3px 0"}}>{s}</li>)}
          </ol>
          <p style={{marginTop:8}}>{applyLog.note}</p>
        </div>
      )}
    </>
  );
}
