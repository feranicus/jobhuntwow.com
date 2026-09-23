import { useEffect, useRef, useState } from "react";
import { deleteJSON, getJSON, patchJSON, postJSON } from "../api.js";

/* Pipeline — the CRM view of the SAME rows the Tailor page writes and the apply engine updates.

   DRAG A CARD TO MOVE IT. He asked for exactly that: "I need to be able to move myself the jobs in
   pipeline but just moving them with my mouse". Native HTML5 drag-and-drop — no library, so there is
   nothing to keep up to date and nothing extra in the bundle.

   THE MOVE IS OPTIMISTIC AND REVERSIBLE: the card lands in the new column immediately (a board that
   waits for a round trip feels broken), the PATCH follows, and if the server refuses the card goes
   BACK where it was and the reason is printed. A UI that shows a state the database does not hold is
   the same defect as a log that claims a submit the site never confirmed.

   AND THE ONE-CLICK `rejected` LINK IS GONE. It sat next to `→ applied` on every card, and one
   mis-click is how a live application ended up in Rejected. Dragging is deliberate; a link is not.
   The `<select>` stays as the keyboard and touch path — dragging is not reachable without a mouse.

   Every value is coerced to text before it renders: a backend shape change must never white-screen
   the cabinet. */
/* THE LIFECYCLE, as he actually lives it (2026-09-18).
   `Applied` and `Submitted` were one event in two colours — the engine's "submitted" means the SITE
   confirmed the send, which is EVIDENCE about that event (a tick on the card), not a second step.
   And "Interview" is a season, not a stage: a board that cannot say which round you are in cannot
   tell you what to prepare tonight. */
const COLS = [
  ["tailored", "Tailored", "documents ready, not sent"],
  ["applied", "Applied", "sent to the employer"],
  ["hr_screen", "HR screen", "recruiter call"],
  ["tech", "Technical", "technical interview"],
  ["task", "Task / presentation", "take-home or panel presentation"],
  ["manager", "Hiring manager", "the manager you would report to"],
  ["final", "Final panel", "final round"],
  ["offer", "Offer", "offer on the table"],
  ["negotiation", "Contract negotiation", "money, start date, notice period, the paperwork"],
  ["signed", "Signed", "contract signed — the job is yours"],
  ["rejected", "Rejected", "closed"],
];
const STAGES = COLS.map(([k]) => k);
const txt = (v) => (v === null || v === undefined ? "" : typeof v === "string" ? v : String(v));
const day = (ts) => (ts ? new Date(Number(ts) * 1000).toLocaleDateString() : "");
/* EXACTLY when, to the minute, with the timezone — he asked for "when exactly It was created time
   and full date". A relative "2 days ago" is not an answer to that. */
const stamp = (ts) =>
  ts
    ? new Date(Number(ts) * 1000).toLocaleString(undefined, {
        weekday: "short", year: "numeric", month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit", timeZoneName: "short",
      })
    : "—";
const dl = (jobId, f) =>
  `/api/electronic/artifacts/${encodeURIComponent(jobId)}/${encodeURIComponent(f)}`;

/* The posting's host, when the JD never carried an employer name. `(employer not recorded)` tells
   you nothing; `app.civi.co.il` at least tells you where it came from. */
function whoFrom(row) {
  const e = txt(row.employer).trim();
  if (e) return e;
  try {
    const h = new URL(txt(row.jd_url)).hostname.replace(/^www\./, "");
    return h || "(employer not recorded)";
  } catch {
    return "(employer not recorded)";
  }
}

export default function Pipeline() {
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("loading…");
  const [over, setOver] = useState("");          // the column the pointer is currently above
  const [busy, setBusy] = useState("");          // the job_id being saved
  const [open, setOpen] = useState(null);        // the row shown in the details panel
  const [draft, setDraft] = useState({ employer: "", title: "" });
  const [panelNote, setPanelNote] = useState("");
  const [noteDraft, setNoteDraft] = useState({ body: "", kind: "note", when: "" });
  const [noteMsg, setNoteMsg] = useState("");
  const dragged = useRef(null);                  // {job_id, from}
  const didDrag = useRef(false);                 // a drag must never also count as a click

  async function load() {
    try {
      const r = await getJSON("/api/applications?days=365");
      const list = Array.isArray(r && r.applications) ? r.applications : [];
      setRows(list);
      setNote(
        list.length
          ? `${list.length} application${list.length === 1 ? "" : "s"} · drag a card to move it` +
            (r.incomplete ? ` · ${r.incomplete} record(s) cannot prove which resume went where` : "")
          : "Nothing tracked yet — tailor a resume on the Tailor page and it lands here."
      );
    } catch (e) {
      setErr(txt((e && e.message) || e));
      setNote("");
    }
  }
  useEffect(() => { load(); }, []);

  /* ONE move, whatever started it (drop or select). Optimistic, then verified, then reverted on
     failure — never left showing something the server did not accept. */
  async function move(jobId, to, from) {
    if (!jobId || !to || to === from || !STAGES.includes(to)) return;
    setErr("");
    setBusy(jobId);
    setRows((prev) => prev.map((r) => (txt(r.job_id) === jobId ? { ...r, stage: to } : r)));
    try {
      const r = await patchJSON(`/api/applications/${encodeURIComponent(jobId)}`, { stage: to });
      if (!r || txt(r.stage) !== to) throw new Error(txt(r && r.detail) || "the server did not accept the move");
      setRows((prev) => prev.map((x) => (txt(x.job_id) === jobId ? { ...x, ...r } : x)));
    } catch (e) {
      setRows((prev) => prev.map((r) => (txt(r.job_id) === jobId ? { ...r, stage: from } : r)));
      setErr("could not move that card — " + txt((e && e.message) || e) + " (it went back)");
    } finally {
      setBusy("");
    }
  }

  /* CLICK A CARD, SEE THE JOB. The list deliberately does not carry the job description (a board
     with fifty postings in it would ship megabytes); the single-row read does, so the panel asks
     for it. */
  async function openCard(id) {
    setPanelNote("loading…");
    setOpen({ job_id: id });
    try {
      const r = await getJSON(`/api/applications/${encodeURIComponent(id)}`);
      setOpen(r);
      setDraft({ employer: txt(r.employer), title: txt(r.title) });
      setNoteDraft({ body: "", kind: "note", when: "" });
      setNoteMsg("");
      setPanelNote("");
    } catch (e) {
      setPanelNote("could not load it — " + txt((e && e.message) || e));
    }
  }

  /* HIS WORD BEATS OUR GUESS. The employer and the role are the only two fields a person can know
     better than the record does; everything else on a row is evidence and stays read-only. */
  async function saveDraft() {
    if (!open || !open.job_id) return;
    setPanelNote("saving…");
    try {
      const r = await patchJSON(`/api/applications/${encodeURIComponent(open.job_id)}`, {
        employer: draft.employer, title: draft.title,
      });
      setOpen({ ...open, ...r });
      setRows((prev) => prev.map((x) => (txt(x.job_id) === txt(open.job_id) ? { ...x, ...r } : x)));
      setPanelNote("saved");
    } catch (e) {
      setPanelNote("not saved — " + txt((e && e.message) || e));
    }
  }

  /* READ THE POSTING AGAIN. For the cards written before the sniff understood how postings are
     worded: same ladder, same guard — a name the posting does not contain is refused. */
  async function reread() {
    if (!open || !open.job_id) return;
    setPanelNote("reading the job description…");
    try {
      const r = await postJSON(`/api/applications/${encodeURIComponent(open.job_id)}/reread`, {});
      setOpen({ ...open, ...r });
      setDraft({ employer: txt(r.employer), title: txt(r.title) });
      setRows((prev) => prev.map((x) => (txt(x.job_id) === txt(open.job_id) ? { ...x, ...r } : x)));
      const info = r.reread || {};
      setPanelNote(info.found
        ? `read from the ${info.source === "text" ? "job description" : info.source === "llm"
            ? "job description (model, checked against the text)" : "posting address"}`
        : "the posting does not name an employer anywhere — type it yourself above");
    } catch (e) {
      setPanelNote("could not re-read it — " + txt((e && e.message) || e));
    }
  }

  /* HIS UPDATES ON THIS APPLICATION. A note is TESTIMONY — what he was told, when the next
     interview is — and it is kept apart from the correlated email below, which is EVIDENCE the
     mailbox found. The panel never merges the two, and neither one moves the card. */
  async function addNote() {
    if (!open || !open.job_id) return;
    const body = txt(noteDraft.body).trim();
    if (!body) { setNoteMsg("type the update first"); return; }
    setNoteMsg("saving…");
    try {
      const r = await postJSON(`/api/applications/${encodeURIComponent(open.job_id)}/notes`, {
        body, kind: noteDraft.kind, when: noteDraft.when,
      });
      setOpen({ ...open, ...r });
      setRows((prev) => prev.map((x) => (txt(x.job_id) === txt(open.job_id) ? { ...x, ...r } : x)));
      setNoteDraft({ body: "", kind: "note", when: "" });
      // A DATE WE COULD NOT READ IS SAID OUT LOUD. The note is kept either way; it is not silently
      // given today's date, because an invented date on a record is worse than no date.
      setNoteMsg(r.when_read === false
        ? "saved — but the date could not be read, so it was left off"
        : "saved");
    } catch (e) {
      setNoteMsg("not saved — " + txt((e && e.message) || e));
    }
  }

  async function removeNote(noteId) {
    if (!open || !open.job_id) return;
    setNoteMsg("removing…");
    try {
      const r = await deleteJSON(
        `/api/applications/${encodeURIComponent(open.job_id)}/notes/${encodeURIComponent(noteId)}`);
      setOpen({ ...open, ...r });
      setNoteMsg("removed");
    } catch (e) {
      setNoteMsg("not removed — " + txt((e && e.message) || e));
    }
  }

  function onDrop(ev, to) {
    ev.preventDefault();
    setOver("");
    const d = dragged.current || {};
    const id = d.job_id || txt(ev.dataTransfer && ev.dataTransfer.getData("text/plain"));
    dragged.current = null;
    move(id, to, d.from);
  }

  return (
    <>
      <h1 className="page-h">Pipeline</h1>
      <p className="page-sub">
        Every application, from the job description to the documents we sent. This is the same
        record the end-of-day digest reports. <b>Drag a card into another column to move it.</b>
      </p>
      {err && <div className="err">{err}</div>}
      {note && <p className="muted">{note}</p>}
      <div className="kan">
        {COLS.map(([key, label, hint]) => {
          const cards = rows.filter((r) => txt(r.stage) === key);
          return (
            <div
              className={"kcol" + (over === key ? " kdrop" : "")}
              key={key}
              onDragOver={(ev) => { ev.preventDefault(); if (over !== key) setOver(key); }}
              onDragEnter={(ev) => ev.preventDefault()}
              onDragLeave={() => setOver((o) => (o === key ? "" : o))}
              onDrop={(ev) => onDrop(ev, key)}
            >
              <h4 title={hint}>{label} {cards.length ? `(${cards.length})` : ""}</h4>
              {cards.map((r) => {
                const id = txt(r.job_id);
                return (
                  <div
                    className={"kcard" + (busy === id ? " ksaving" : "")}
                    key={id}
                    draggable
                    title="drag me to another column"
                    onClick={() => { if (!didDrag.current) openCard(id); }}
                    onDragStart={(ev) => {
                      didDrag.current = true;
                      dragged.current = { job_id: id, from: key };
                      try {
                        ev.dataTransfer.setData("text/plain", id);
                        ev.dataTransfer.effectAllowed = "move";
                      } catch { /* older browsers: the ref above still carries it */ }
                    }}
                    onDragEnd={() => {
                      dragged.current = null; setOver("");
                      // let the click that ends a drag pass by before re-arming
                      setTimeout(() => { didDrag.current = false; }, 0);
                    }}
                  >
                    <b>{whoFrom(r)}{r.confirmed ? <span title="the site confirmed this submission"
                        style={{ color: "var(--green)", marginLeft: 6 }}>✓</span> : null}</b>
                    <small>{txt(r.title) || "(role not recorded)"}</small>
                    <br />
                    <small>
                      {r.jd_url ? (
                        <a href={txt(r.jd_url)} target="_blank" rel="noreferrer"
                           draggable={false} onClick={(e) => e.stopPropagation()}>the posting</a>
                      ) : r.jd_chars ? (
                        `pasted JD · ${txt(r.jd_chars)} chars`
                      ) : (
                        <span style={{ color: "#b91c1c" }}>no job description on record</span>
                      )}
                      {r.ats ? ` · ${txt(r.ats)}` : ""}
                      {r.sent_ts ? ` · sent ${day(r.sent_ts)}` : ""}
                    </small>
                    <br />
                    <small style={{ color: "var(--green)" }}>
                      {txt(r.resume_file) || (Array.isArray(r.files) && r.files.length
                        ? r.files.map(txt).join(", ")
                        : "no document on record")}
                    </small>
                    <br />
                    {/* keyboard + touch path. Dragging is a mouse gesture; this is the same move. */}
                    <select
                      className="kmove"
                      value={key}
                      disabled={busy === id}
                      onChange={(ev) => move(id, ev.target.value, key)}
                      aria-label={"move " + whoFrom(r) + " to another stage"}
                    >
                      {COLS.map(([k, lab]) => (
                        <option key={k} value={k}>{k === key ? `— ${lab} —` : `move to ${lab}`}</option>
                      ))}
                    </select>
                    {!r.correlated && (
                      <small style={{ color: "#b91c1c", display: "block", marginTop: 4 }}>
                        incomplete: cannot prove which resume went to this posting
                      </small>
                    )}
                  </div>
                );
              })}
              {!cards.length && <small className="muted">drop here</small>}
            </div>
          );
        })}
      </div>

      {open && (
        <div className="jdrawer" onClick={(e) => { if (e.target === e.currentTarget) setOpen(null); }}>
          <div className="jpanel">
            <button className="jclose" onClick={() => setOpen(null)} aria-label="close">×</button>
            <h3>{txt(open.employer) || whoFrom(open) || "this application"}</h3>
            <p className="muted">{txt(open.title) || "(role not recorded)"}</p>

            <div className="jgrid">
              <span>created</span><b>{stamp(open.created_ts)}</b>
              <span>sent</span><b>{open.sent_ts ? stamp(open.sent_ts) : "not sent yet"}</b>
              <span>last change</span><b>{stamp(open.updated_ts)}</b>
              <span>stage</span><b>{txt(open.stage)}</b>
              <span>ATS</span><b>{txt(open.ats) || "—"}</b>
              <span>employer from</span><b>{txt(open.employer_source) || "—"}</b>
              <span>record</span><b><code>{txt(open.job_id)}</code></b>
            </div>

            <h4>Correct the details</h4>
            <div className="jedit">
              <input value={draft.employer} placeholder="employer"
                     onChange={(e) => setDraft({ ...draft, employer: e.target.value })} />
              <input value={draft.title} placeholder="role"
                     onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
              <button className="btn" onClick={saveDraft}>Save</button>
              <button className="btn ghost" onClick={reread}>Re-read the posting</button>
            </div>
            {panelNote && <p className="muted">{panelNote}</p>}

            <h4>Documents</h4>
            <div>
              {(Array.isArray(open.files) ? open.files : []).map((f) => (
                <a key={txt(f)} className="btn ghost" style={{ marginRight: 8, marginBottom: 6 }}
                   href={dl(txt(open.job_id), txt(f))}>⬇ {txt(f)}</a>
              ))}
              {!(open.files || []).length && <small className="muted">no document on record</small>}
            </div>

            {/* HIS OWN UPDATES. Testimony, not evidence: what the recruiter said, when the next
                interview is. Kept in its own table and shown under its own heading so that what he
                SAID can never be mistaken for what we FOUND. Nothing here moves the card. */}
            <h4>Updates</h4>
            <div className="jedit" style={{ gridTemplateColumns: "1fr" }}>
              <textarea rows={3} value={noteDraft.body}
                        placeholder="what happened — e.g. second interview with the hiring manager, they asked for references"
                        onChange={(e) => setNoteDraft({ ...noteDraft, body: e.target.value })} />
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center",
                          marginTop: 6 }}>
              <select value={noteDraft.kind}
                      onChange={(e) => setNoteDraft({ ...noteDraft, kind: e.target.value })}>
                <option value="note">note</option>
                <option value="interview">interview</option>
                <option value="call">call</option>
                <option value="task">task</option>
                <option value="followup">follow-up</option>
                <option value="offer">offer</option>
              </select>
              <input type="date" value={noteDraft.when} title="the date this is about (optional)"
                     onChange={(e) => setNoteDraft({ ...noteDraft, when: e.target.value })} />
              <button className="btn" onClick={addNote}>Add update</button>
              {noteMsg && <small className="muted">{noteMsg}</small>}
            </div>
            <div style={{ marginTop: 10 }}>
              {(open.notes || []).map((n) => (
                <div key={txt(n.note_id)} style={{
                  borderLeft: "3px solid var(--accent, #4c8dff)", paddingLeft: 10, marginBottom: 10,
                }}>
                  <div style={{ fontSize: 12, color: "var(--muted)" }}>
                    {stamp(n.ts)}{n.kind && n.kind !== "note" ? ` · ${txt(n.kind)}` : ""}
                    {n.when_ts ? ` · for ${day(n.when_ts)}` : ""}
                    {" · "}
                    <a role="button" tabIndex={0} style={{ cursor: "pointer" }}
                       onClick={() => removeNote(txt(n.note_id))}>remove</a>
                  </div>
                  <div style={{ whiteSpace: "pre-wrap" }}>{txt(n.body)}</div>
                </div>
              ))}
              {!(open.notes || []).length && (
                <small className="muted">no updates yet — add the first one above</small>
              )}
            </div>

            {/* WHAT THIS EMPLOYER HAS SAID. Correlated from the mailbox, with the evidence that
                linked each message, and a link straight into Gmail. The label (rejection /
                interview / offer) is a reading of the subject line and is shown, never acted on —
                nothing here moves the card.

                THE HEADING IS ALWAYS HERE, and an empty list is never left to speak for itself:
                "nothing has arrived" and "the mailbox is not connected" look identical on screen
                and mean opposite things, so the panel says which one it is. */}
            <h4>Emails about this application</h4>
            {!(open.mails || []).length && (
              <p className="muted" style={{ fontSize: 13 }}>
                {open.mail_status && open.mail_status.configured
                  ? "no email has matched this application yet — a message is filed here when the employer, the ATS domain or the posting id matches."
                  : txt((open.mail_status || {}).why)
                    || "the mailbox is not connected yet, so nothing has been read."}
              </p>
            )}
            {(open.mails || []).length > 0 && (
              <>
                <div>
                  {(open.mails || []).map((m) => (
                    <div key={txt(m.msg_id)} style={{
                      borderLeft: "3px solid var(--line, #26314a)", paddingLeft: 10,
                      marginBottom: 10,
                    }}>
                      <div style={{ fontSize: 12, color: "var(--muted)" }}>
                        {m.ts ? new Date(Number(m.ts) * 1000).toLocaleString() : ""}
                        {m.kind && m.kind !== "mail" ? ` · ${txt(m.kind)}` : ""}
                      </div>
                      <div><b>{txt(m.subject) || "(no subject)"}</b></div>
                      <div style={{ fontSize: 12 }}>{txt(m.from)}</div>
                      <div style={{ fontSize: 12, color: "var(--muted)" }}>{txt(m.snippet)}</div>
                      <div style={{ fontSize: 11, color: "var(--muted)" }}>
                        matched because: {(m.evidence || []).map(txt).join("; ") || "—"}
                      </div>
                      {m.url && (
                        <a href={txt(m.url)} target="_blank" rel="noreferrer"
                           style={{ fontSize: 12 }}>open in Gmail →</a>
                      )}
                    </div>
                  ))}
                </div>
              </>
            )}

            <h4>Job description</h4>
            {open.jd_url && (
              <p><a href={txt(open.jd_url)} target="_blank" rel="noreferrer">{txt(open.jd_url)}</a></p>
            )}
            <pre className="jdtext">
              {txt(open.jd_text) || (open.jd_url ? "(a link only — nothing was pasted)"
                                                 : "(no job description on record)")}
            </pre>
          </div>
        </div>
      )}
    </>
  );
}
