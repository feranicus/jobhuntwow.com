import { useEffect, useState } from "react";
import { getJSON, postJSON, me } from "../api.js";

/* Tailor: the thing Electronic promises in chat, actually wired.
   JD (paste or URL) + your profile  ->  POST /api/electronic/generate  ->  DOCX + PDF.
   The backend never invents experience: it truth-checks the output against the profile and
   returns `warnings` + `gaps`, which we surface instead of hiding. */
export default function Tailor() {
  const [email, setEmail] = useState("");
  const [jdText, setJdText] = useState("");
  const [jdUrl, setJdUrl] = useState("");
  const [profile, setProfile] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [result, setResult] = useState(null);
  const [note, setNote] = useState("");
  const [pct, setPct] = useState(0);
  const [secs, setSecs] = useState(0);
  const [model, setModel] = useState("");
  const [photo, setPhoto] = useState("");
  const [usePhoto, setUsePhoto] = useState(false);
  const [answers, setAnswers] = useState({});     // gap question -> candidate's answer
  const [evidence, setEvidence] = useState([]);   // [{name, words, text}] portfolio / articles
  const [links, setLinks] = useState("");
  const [chat, setChat] = useState([]);          // [{role:'you'|'electronic', text}]
  const [msg, setMsg] = useState("");

  // Elapsed clock + eased progress. Generation is two LLM calls (~10-40s); a bare spinner makes
  // people refresh, and refreshing loses the run.
  useEffect(() => {
    if (!busy) return;
    const t0 = Date.now();
    const iv = setInterval(() => {
      setSecs(Math.round((Date.now() - t0) / 1000));
      setPct(p => (p < 90 ? p + (90 - p) * 0.04 : p));   // ease toward 90, never fake completion
    }, 300);
    return () => clearInterval(iv);
  }, [busy]);
  const [jobs, setJobs] = useState([]);

  useEffect(() => {
    me().then(u => { if (u && u.email) { setEmail(u.email); loadJobs(u.email); } });
    getJSON("/api/models").then(d => setModel(d.default || "")).catch(() => {});
  }, []);

  async function loadJobs(who) {
    try {
      const d = await getJSON(`/api/electronic/jobs?email=${encodeURIComponent(who)}`);
      setJobs(Array.isArray(d.jobs) ? d.jobs : []);
    } catch { /* listing is best-effort */ }
  }

  async function readFile(e) {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    setErr("");
    // A browser CANNOT read a PDF/DOCX -- FileReader returns raw bytes ("%PDF-1.4 ..."), which is
    // how an unreadable profile reached the generator. Always extract on the server.
    setBusy(`Reading ${f.name}…`);
    try {
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch("/api/electronic/profile/upload",
                            { method: "POST", credentials: "include", body: fd });
      const d = await r.json();
      if (!r.ok) { setErr(d.detail || "Could not read that file."); setBusy(""); return; }
      setProfile(d.text || "");
      setNote(`Read ${d.words} words from ${d.filename} (${d.kind}).`);
    } catch (ex) {
      setErr("Upload failed: " + String(ex && ex.message ? ex.message : ex));
    }
    setBusy("");
  }

  async function uploadPhoto(e) {
    const f = e.target.files && e.target.files[0];
    if (!f || !email) return;
    const fd = new FormData();
    fd.append("file", f);
    const r = await fetch(`/api/electronic/photo/upload?email=${encodeURIComponent(email)}`,
                          { method: "POST", credentials: "include", body: fd });
    const d = await r.json();
    if (!r.ok) { setErr(d.detail || "Could not read that image."); return; }
    setPhoto(d.photo); setUsePhoto(true); setErr("");
  }

  async function addEvidence(e) {
    const files = Array.from(e.target.files || []);
    if (!files.length || !email) return;
    setErr("");
    for (const f of files) {
      setBusy(`Reading ${f.name}…`);
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch(`/api/electronic/evidence/upload?email=${encodeURIComponent(email)}`,
                            { method: "POST", credentials: "include", body: fd });
      const d = await r.json();
      if (!r.ok) { setErr(`${f.name}: ${d.detail || "could not be read"}`); continue; }
      setEvidence(prev => [...prev.filter(x => x.name !== d.name), d]);
    }
    setBusy("");
  }

  // Iterate on documents that already exist: "put Canonical back", "one page", "less formal".
  async function revise() {
    const instruction = msg.trim();
    if (!instruction || !result || !result.job_id) return;
    setMsg("");
    setChat(c => [...c, { role: "you", text: instruction }]);
    setPct(20); setBusy("Applying your change…");
    try {
      const r = await postJSON("/api/electronic/revise",
                               { email, job_id: result.job_id, instruction, use_photo: usePhoto });
      if (r.detail) {
        setChat(c => [...c, { role: "electronic", text: "Could not do that: " + r.detail }]);
      } else {
        setResult({ ...result, ...r });
        setChat(c => [...c, { role: "electronic",
          text: "Done — rebuilt " + (r.files || []).join(", ") + ". Download again below." }]);
      }
    } catch (e) {
      setChat(c => [...c, { role: "electronic", text: "Failed: " + String(e) }]);
    }
    setPct(100); setBusy("");
  }

  async function run() {
    setErr(""); setResult(null);
    if (!jdText.trim() && !jdUrl.trim()) { setErr("Paste the job description (or give a URL)."); return; }
    if (!profile.trim()) { setErr("Add your profile/resume text — we never invent experience."); return; }
    try {
      setPct(8); setBusy("Reading the job description…");
      const jd = await postJSON("/api/electronic/jd", { text: jdText, url: jdUrl });
      if (jd.status === "needs_paste" || jd.status === "needs_local_fetch") {
        setBusy(""); setErr(jd.note || "That site blocks fetching — paste the JD text instead."); return;
      }
      setPct(35); setBusy("Writing your resume and cover letter…");
      const r = await postJSON("/api/electronic/generate",
                               { email, jd, profile, answers, use_photo: usePhoto,
                                 evidence: evidence.map(x => ({ name: x.name, text: x.text })),
                                 links: links.split(/[\s,]+/).filter(Boolean) });
      if (r.detail) { setBusy(""); setErr(String(r.detail)); return; }
      setPct(100); setResult(r); setBusy(""); loadJobs(email);
    } catch (e) {
      setBusy(""); setErr(String(e && e.message ? e.message : e));
    }
  }

  const dl = (jid, f) =>
    `/api/electronic/artifacts/${encodeURIComponent(jid)}/${encodeURIComponent(f)}?email=${encodeURIComponent(email)}`;

  return (
    <div>
      <h1>Tailor</h1>
      <p className="muted">
        Job description in, a truth-checked resume and cover letter out — as DOCX and PDF.
      </p>

      <div className="card">
        <label className="lbl">JOB DESCRIPTION (paste the full text)</label>
        <textarea className="input" rows={8} value={jdText}
                  onChange={e => setJdText(e.target.value)}
                  placeholder="Paste the whole posting here…" />
        <label className="lbl">…or a link (LinkedIn/Indeed/Glassdoor usually block fetching — paste instead)</label>
        <input className="input" value={jdUrl} onChange={e => setJdUrl(e.target.value)}
               placeholder="https://boards.greenhouse.io/…" />
      </div>

      <div className="card">
        <label className="lbl">YOUR PROFILE / CURRENT RESUME (PDF, DOCX, TXT or MD)</label>
        <input type="file" accept=".pdf,.docx,.md,.txt,.markdown" onChange={readFile} />
        {note && <p className="muted">{note}</p>}
      </div>

      <div className="card">
        <label className="lbl">PHOTO (optional)</label>
        <input type="file" accept=".jpg,.jpeg,.png" onChange={uploadPhoto} />
        {photo && (
          <label className="muted" style={{ display: "block", marginTop: 6 }}>
            <input type="checkbox" checked={usePhoto} onChange={e => setUsePhoto(e.target.checked)} />
            {" "}Put my photo on the resume ({photo})
          </label>
        )}
        <p className="muted">
          Customary on a German/Austrian/Swiss CV. For US/UK applications most recruiters advise
          against it and some ATS parsers mishandle images — leave it off for those.
        </p>
        <textarea className="input" rows={8} value={profile}
                  onChange={e => setProfile(e.target.value)}
                  placeholder="Upload a PDF/DOCX above, or paste your resume text here…" />
      </div>

      <button className="btn" onClick={run} disabled={!!busy}>
        {busy ? busy : "Generate resume + cover letter →"}
      </button>

      {busy && (
        <div className="card">
          <div className="bar"><div className="bar-fill" style={{ width: `${Math.round(pct)}%` }} /></div>
          <p className="muted">
            {Math.round(pct)}% · {busy} · {secs}s elapsed{model ? ` · model: ${model}` : ""} — takes ~15-45s. Leaving the page cancels it.
          </p>
        </div>
      )}
      {err && <div className="err">{err}</div>}

      {result && (
        <div className="card">
          <h3>{result.jd?.title || "Tailored"} {result.jd?.company ? `· ${result.jd.company}` : ""}</h3>
          <p className="muted">
            Built in {Math.round((result.elapsed_ms || 0) / 100) / 10}s
            {result.models ? ` · models: ${Object.values(result.models).filter(Boolean).join(", ")}` : ""}
            {result.answers_used ? ` · used ${result.answers_used} of your answers` : ""}
            {result.evidence_used ? ` · ${result.evidence_used} attachment(s)` : ""}
            {result.links_used ? ` · ${result.links_used} link(s) read` : ""}
            {result.photo_used ? " · photo included" : ""}
          </p>
          <div>
            {(result.files || []).map(f => (
              <a key={f} className="btn ghost" style={{ marginRight: 8 }}
                 href={dl(result.job_id, f)}>⬇ {f}</a>
            ))}
          </div>
          <div className="card" style={{ background: "#f8fafc" }}>
            <h3>Talk to Electronic about this draft</h3>
            <p className="muted">
              Ask for changes and it rebuilds the same tailored draft — e.g. “put Canonical, Verint
              and Red Hat back”, “make it one page”, “shorter bullets”, “more formal tone”,
              “move Key Achievements below the summary”.
            </p>
            {chat.map((m, i) => (
              <p key={i} className={m.role === "you" ? "" : "muted"}>
                <b>{m.role === "you" ? "You" : "Electronic"}:</b> {m.text}
              </p>
            ))}
            <input className="input" value={msg} onChange={e => setMsg(e.target.value)}
                   onKeyDown={e => { if (e.key === "Enter") revise(); }}
                   placeholder="Tell Electronic what to change…" />
            <button className="btn" onClick={revise} disabled={!!busy || !msg.trim()}>
              Apply change →
            </button>
          </div>

          {!!(result.employers_missing || []).length && (
            <div className="err">
              <b>These employers are missing from the resume:</b>{" "}
              {result.employers_missing.join(", ")} — ask Electronic above to put them back.
            </div>
          )}

          {!!(result.link_notes || []).length && (
            <div className="err">
              <b>Could not read:</b>
              <ul>{result.link_notes.map((w, i) => <li key={i}>{String(w)}</li>)}</ul>
            </div>
          )}
          {!!(result.warnings || []).length && (
            <div className="err">
              <b>Truth check flagged:</b>
              <ul>{result.warnings.map((w, i) => <li key={i}>{String(w)}</li>)}</ul>
            </div>
          )}
          {!!(result.gaps || []).length && (
            <div className="card" style={{ background: "#f8fafc" }}>
              <h3>Electronic has questions</h3>
              <p className="muted">
                These are the things the posting asks for that your CV does not evidence. If you
                have them and simply never wrote them down (a client, a project, a tool), answer
                here and Electronic will rebuild using your answers. Blank answers stay out — we
                never invent experience.
              </p>
              {result.gaps.map((g, i) => (
                <div key={i} style={{ marginBottom: 10 }}>
                  <label className="lbl" style={{ textTransform: "none", letterSpacing: 0 }}>
                    {String(g)}
                  </label>
                  <input className="input" value={answers[String(g)] || ""}
                         placeholder="e.g. Ran a 2-year programme for Aldi and AON — retail + insurance"
                         onChange={e => setAnswers({ ...answers, [String(g)]: e.target.value })} />
                </div>
              ))}
              <label className="lbl" style={{ textTransform: "none", letterSpacing: 0 }}>
                Or attach proof — project portfolio, case study, published article, reference
                letter (PDF, DOCX, TXT, MD)
              </label>
              <input type="file" multiple accept=".pdf,.docx,.md,.txt" onChange={addEvidence} />
              {!!evidence.length && (
                <p className="muted">
                  Attached: {evidence.map(x => `${x.name} (${x.words} words)`).join(" · ")}
                </p>
              )}
              <label className="lbl" style={{ textTransform: "none", letterSpacing: 0 }}>
                …or links to articles / portfolio pages (one per line)
              </label>
              <textarea className="input" rows={2} value={links}
                        onChange={e => setLinks(e.target.value)}
                        placeholder="https://feranicus.com/me&#10;https://medium.com/@you/article" />
              <button className="btn" onClick={run} disabled={!!busy}>
                Rebuild with my answers →
              </button>
            </div>
          )}
          {!!(result.keywords_matched || []).length && (
            <p className="muted"><b>Matched:</b> {result.keywords_matched.join(", ")}</p>
          )}
        </div>
      )}

      {!!jobs.length && (
        <div className="card">
          <h3>Earlier documents</h3>
          {jobs.map(j => (
            <div key={j.job_id || j}>
              <a href={`#`} onClick={e => { e.preventDefault(); setResult(j); }}>
                {j.title || j.job_id} {j.company ? `· ${j.company}` : ""}
              </a>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
