import { useEffect, useRef, useState } from "react";
import { getJSON, postJSON, putJSON, me } from "../api.js";

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
  // THE PROJECT PORTFOLIO — written once, kept on the server, read by every run afterwards.
  const [port, setPort] = useState([]);          // [{id,title,org,role,period,url,stack,summary,achievements}]
  const [portMsg, setPortMsg] = useState("");
  const [portPick, setPortPick] = useState(null); // the preview: which projects THIS posting picks
  const [portImport, setPortImport] = useState(null); // what an uploaded PDF/Word file proposed
  const [usePort, setUsePort] = useState(true);
  // "letter" = the usual prose letter · "top5" = Top 5 reasons to hire me for this role.
  const [coverFormat, setCoverFormat] = useState("letter");
  // THE RUN LOG. The browser mints the id, sends it with the run, and polls for the lines while
  // the run is in flight — so the console shows what the platform IS DOING, not a percentage.
  const [logLines, setLogLines] = useState([]);
  const [logState, setLogState] = useState(null);
  const logRef = useRef(null);
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

  // Keep the console pinned to the newest line while it streams.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [logLines]);

  useEffect(() => { (async () => {
    try {
      const r = await getJSON("/api/electronic/portfolio");
      if (r && Array.isArray(r.items)) setPort(r.items);
    } catch { /* an empty portfolio is a normal state, not an error */ }
  })(); }, []);

  async function savePortfolio(next) {
    setPort(next);                                   // optimistic: the page must feel immediate
    setPortMsg("saving…");
    try {
      const r = await putJSON("/api/electronic/portfolio", { items: next });
      if (r && Array.isArray(r.items)) {
        setPort(r.items);                            // RENDER WHAT WAS STORED, not what we sent:
        setPortMsg(`saved ${r.count} project(s)`);   // the store bounds and drops, and the page
      } else {                                       // must show the truth after it did.
        setPortMsg("not saved — the server did not confirm");
      }
    } catch (e) { setPortMsg("not saved: " + String(e && e.message ? e.message : e)); }
  }

  const blankProject = () => ({
    id: "", title: "", org: "", role: "", period: "", url: "",
    stack: [], tags: [], summary: "", achievements: [],
  });
  const setProject = (i, patch) =>
    setPort(port.map((p, j) => (i === j ? { ...p, ...patch } : p)));
  const listToText = (v) => (Array.isArray(v) ? v.join(", ") : String(v || ""));
  const textToList = (v) => String(v || "").split(/[,;\n]/).map(x => x.trim()).filter(Boolean);

  async function importPortfolio(f) {
    if (!f) return;
    setPortImport(null); setPortMsg(`reading ${f.name}…`);
    const fd = new FormData(); fd.append("file", f);
    try {
      const r = await fetch("/api/electronic/portfolio/upload",
                            { method: "POST", credentials: "include", body: fd });
      const d = await r.json();
      if (d.detail) { setPortMsg(String(d.detail)); return; }
      // PROPOSED, NOT SAVED. They land in the editor for him to check; the store is only written
      // when he presses Save, because a parser guessing at a two-column PDF must not be able to
      // change stored facts on its own.
      setPort([...port, ...(d.proposed || [])]);
      setPortImport(d);
      setPortMsg(d.note || "");
    } catch (e) { setPortMsg(String(e && e.message ? e.message : e)); }
  }

  async function previewPortfolio() {
    setPortPick(null);
    if (!jdText.trim()) { setPortMsg("paste the job description first, then preview"); return; }
    try {
      const r = await postJSON("/api/electronic/portfolio/preview", { text: jdText });
      setPortPick(r); setPortMsg("");
    } catch (e) { setPortMsg(String(e && e.message ? e.message : e)); }
  }

  function newRunId() {
    try {
      if (window.crypto && window.crypto.randomUUID) return "run-" + window.crypto.randomUUID();
    } catch { /* older browsers fall through */ }
    return "run-" + Date.now() + "-" + Math.random().toString(16).slice(2, 10);
  }

  // POLLING, NOT A STREAM. This endpoint sits behind a proxy that buffers, and a run is 15-45s: a
  // poll that cannot half-work beats a stream that silently stalls. `after` makes it cheap —
  // each call returns only what is new.
  async function pollLog(runId, stop) {
    let after = 0;
    for (let i = 0; i < 400 && !stop.done; i++) {
      try {
        const r = await getJSON(`/api/electronic/runlog/${encodeURIComponent(runId)}?after=${after}`);
        if (r && Array.isArray(r.lines)) {
          if (r.lines.length) setLogLines(prev => prev.concat(r.lines));
          after = r.next || after;
          setLogState(r);
          if (r.done) return;
        }
      } catch { /* a log that fails must never affect the run it observes */ }
      await new Promise(res => setTimeout(res, 700));
    }
  }

  async function run() {
    setErr(""); setResult(null); setLogLines([]); setLogState(null);
    if (!jdText.trim() && !jdUrl.trim()) { setErr("Paste the job description (or give a URL)."); return; }
    if (!profile.trim()) { setErr("Add your profile/resume text — we never invent experience."); return; }
    try {
      setPct(8); setBusy("Reading the job description…");
      const jd = await postJSON("/api/electronic/jd", { text: jdText, url: jdUrl });
      if (jd.status === "needs_paste" || jd.status === "needs_local_fetch") {
        setBusy(""); setErr(jd.note || "That site blocks fetching — paste the JD text instead."); return;
      }
      setPct(35); setBusy("Writing your resume and cover letter…");
      const runId = newRunId();
      const stop = { done: false };
      pollLog(runId, stop);                       // fire and forget: it stops when the run does
      const r = await postJSON("/api/electronic/generate",
                               { email, jd, profile, answers, use_photo: usePhoto, run_id: runId,
                                 use_portfolio: usePort, cover_format: coverFormat,
                                 evidence: evidence.map(x => ({ name: x.name, text: x.text })),
                                 links: links.split(/[\s,]+/).filter(Boolean) });
      stop.done = true;
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

      <div className="card">
        <label className="lbl">PROJECT PORTFOLIO (kept for every future application)</label>
        <p className="muted" style={{ marginTop: 0 }}>
          Write a project once. For each posting we pick the ones it actually asks for — by matching
          the posting's own words against your stack, tags and titles — and hand those projects to
          the writer as facts. Nothing is invented, and a posting none of them fit adds nothing
          rather than padding.
        </p>

        <div style={{ margin: "8px 0 12px" }}>
          <label className="lbl" style={{ textTransform: "none", letterSpacing: 0 }}>
            Already have it as a file? Import a PDF or Word portfolio (also .txt / .md)
          </label>
          <input type="file" accept=".pdf,.docx,.txt,.md"
                 onChange={e => importPortfolio(e.target.files && e.target.files[0])} />
          {portImport && (
            <p className="muted" style={{ marginTop: 4 }}>
              {portImport.name} · {portImport.kind} · {portImport.chars} characters read ·
              {" "}{(portImport.proposed || []).length} project(s) proposed — <b>nothing is saved
              until you press Save portfolio</b>.
            </p>
          )}
        </div>

        {port.map((p, i) => (
          <div key={p.id || i} className="card" style={{ background: "#f8fafc", marginBottom: 10 }}>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <input className="input" style={{ flex: "2 1 220px" }} placeholder="Project title"
                     value={p.title || ""} onChange={e => setProject(i, { title: e.target.value })} />
              <input className="input" style={{ flex: "1 1 140px" }} placeholder="Client / employer"
                     value={p.org || ""} onChange={e => setProject(i, { org: e.target.value })} />
              <input className="input" style={{ flex: "1 1 140px" }} placeholder="Your role"
                     value={p.role || ""} onChange={e => setProject(i, { role: e.target.value })} />
              <input className="input" style={{ flex: "0 1 120px" }} placeholder="2023-2024"
                     value={p.period || ""} onChange={e => setProject(i, { period: e.target.value })} />
            </div>
            <input className="input" style={{ marginTop: 8 }} placeholder="Link (optional)"
                   value={p.url || ""} onChange={e => setProject(i, { url: e.target.value })} />
            <input className="input" style={{ marginTop: 8 }}
                   placeholder="Stack / methods, comma separated — these are what match a posting"
                   value={listToText(p.stack)}
                   onChange={e => setProject(i, { stack: textToList(e.target.value) })} />
            <textarea className="input" style={{ marginTop: 8 }} rows={2}
                      placeholder="What the project was, in one or two sentences."
                      value={p.summary || ""}
                      onChange={e => setProject(i, { summary: e.target.value })} />
            <textarea className="input" style={{ marginTop: 8 }} rows={3}
                      placeholder="Achievements, one per line. Numbers where you have them — these are what a cover letter uses."
                      value={(p.achievements || []).join("\n")}
                      onChange={e => setProject(i, { achievements: e.target.value.split("\n").map(x => x.trim()).filter(Boolean) })} />
            <div style={{ marginTop: 8, display: "flex", gap: 8, alignItems: "center" }}>
              <button className="btn ghost sm" type="button"
                      onClick={() => savePortfolio(port.filter((_, j) => j !== i))}>Remove</button>
              {p.source ? <span className="muted" style={{ fontSize: 12 }}>{p.source}</span> : null}
            </div>
          </div>
        ))}

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
          <button className="btn ghost sm" type="button"
                  onClick={() => setPort([...port, blankProject()])}>+ Add a project</button>
          <button className="btn sm" type="button" onClick={() => savePortfolio(port)}>Save portfolio</button>
          <button className="btn ghost sm" type="button" onClick={previewPortfolio}>
            Which of these fit this posting?
          </button>
          <label className="muted" style={{ marginLeft: "auto" }}>
            <input type="checkbox" checked={usePort}
                   onChange={e => setUsePort(e.target.checked)} /> use the portfolio for this run
          </label>
        </div>
        {portMsg && <p className="muted" style={{ marginTop: 6 }}>{portMsg}</p>}
        {portPick && (
          <div className="card" style={{ background: "#f8fafc", marginTop: 10 }}>
            {(portPick.selected || []).length ? (
              <>
                <b>This posting would use:</b>
                <ul>
                  {(portPick.selected || []).map(x => (
                    <li key={x.id}>
                      {x.title}{x.org ? ` · ${x.org}` : ""}
                      <span className="muted"> — matched: {(x.matched || []).join(", ")}</span>
                    </li>
                  ))}
                </ul>
              </>
            ) : <p className="muted" style={{ margin: 0 }}>{portPick.note}</p>}
          </div>
        )}
      </div>

      <div className="card">
        <label className="lbl">COVER LETTER FORMAT</label>
        <label style={{ display: "block", marginTop: 6 }}>
          <input type="radio" name="coverfmt" checked={coverFormat === "letter"}
                 onChange={() => setCoverFormat("letter")} />{" "}
          Classic letter — 3-4 short paragraphs, 250-350 words.
        </label>
        <label style={{ display: "block", marginTop: 6 }}>
          <input type="radio" name="coverfmt" checked={coverFormat === "top5"}
                 onChange={() => setCoverFormat("top5")} />{" "}
          <b>Top 5 reasons to hire me</b> — five ranked reasons, each answering a different
          requirement in the posting, each backed by a real achievement from your profile or
          portfolio.
        </label>
      </div>

      <button className="btn" onClick={run} disabled={!!busy}>
        {busy ? busy : "Generate resume + cover letter →"}
      </button>

      {(logLines.length > 0) && (
        <div className="card">
          <label className="lbl">RUN LOG — what the platform is doing, line by line</label>
          <pre ref={logRef} style={{
            background: "#0b1020", color: "#d7e3ff", padding: 12, borderRadius: 8,
            maxHeight: 340, overflow: "auto", fontSize: 12, lineHeight: 1.45,
            whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0,
          }}>{logLines.join("\n")}</pre>
          <p className="muted" style={{ marginTop: 6 }}>
            {logState && logState.done
              ? "Run finished. The same log is saved beside your documents as a .txt."
              : (logState ? `${logState.pct || 0}% · ${txt(logState.msg)}` : "waiting for the first line…")}
          </p>
        </div>
      )}

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
            {result.cover_format === "top5" ? " · cover letter: top 5 reasons" : ""}
            {(result.portfolio_used || []).length
              ? ` · projects used: ${(result.portfolio_used || []).map(x => x.title).join(", ")}`
              : ""}
            {result.portfolio_note ? ` · ${result.portfolio_note}` : ""}
            {result.photo_used ? " · photo included" : ""}
          </p>
          <div>
            {(result.files || []).map(f => (
              <a key={f} className="btn ghost" style={{ marginRight: 8 }}
                 href={dl(result.job_id, f)}>⬇ {f}</a>
            ))}
          </div>
          {/* THE CORRELATION, SAID OUT LOUD. One row now links this job description — the link you
              gave, or the text you pasted — to the exact documents built for it. The Pipeline and
              the end-of-day digest read that same row. */}
          <p className="muted" style={{ marginTop: 10 }}>
            Tracked:{" "}
            {result.jd?.url
              ? <a href={String(result.jd.url)} target="_blank" rel="noreferrer">this posting</a>
              : "the job description you pasted"}
            {" → "}
            <b>{(result.files || []).filter(f => /resume|cv/i.test(String(f))).join(", ")
                 || "the documents above"}</b>
            {" · "}<a href="/pipeline">see it in your pipeline</a>
            <br />
            <small>record {String(result.job_id || "")}</small>
          </p>
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
