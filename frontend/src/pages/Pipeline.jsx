import { useEffect, useRef, useState } from "react";
import { getJSON, patchJSON } from "../api.js";

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
const COLS = [
  ["tailored", "Tailored"],
  ["applied", "Applied"],
  ["submitted", "Submitted"],
  ["interview", "Interview"],
  ["offer", "Offer"],
  ["rejected", "Rejected"],
];
const STAGES = COLS.map(([k]) => k);
const txt = (v) => (v === null || v === undefined ? "" : typeof v === "string" ? v : String(v));
const day = (ts) => (ts ? new Date(Number(ts) * 1000).toLocaleDateString() : "");

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
  const dragged = useRef(null);                  // {job_id, from}

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
        {COLS.map(([key, label]) => {
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
              <h4>{label} {cards.length ? `(${cards.length})` : ""}</h4>
              {cards.map((r) => {
                const id = txt(r.job_id);
                return (
                  <div
                    className={"kcard" + (busy === id ? " ksaving" : "")}
                    key={id}
                    draggable
                    title="drag me to another column"
                    onDragStart={(ev) => {
                      dragged.current = { job_id: id, from: key };
                      try {
                        ev.dataTransfer.setData("text/plain", id);
                        ev.dataTransfer.effectAllowed = "move";
                      } catch { /* older browsers: the ref above still carries it */ }
                    }}
                    onDragEnd={() => { dragged.current = null; setOver(""); }}
                  >
                    <b>{whoFrom(r)}</b>
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
    </>
  );
}
