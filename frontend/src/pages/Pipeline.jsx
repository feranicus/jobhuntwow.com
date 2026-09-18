import { useEffect, useState } from "react";
import { getJSON, patchJSON } from "../api.js";

/* Pipeline — the CRM view of the SAME rows the Tailor page writes and the apply engine updates.

   This page used to render a hardcoded board of invented companies ("Delivery Hero", "N26",
   "€104k"). A funnel that shows fiction is worse than an empty one: it cannot be acted on and it
   teaches you to distrust the screen. It now reads GET /api/applications, which is one row per
   application: the job description (link or the pasted text), the exact documents we produced,
   whether it was sent, by which ATS, and the stage.

   Every value is coerced to text before it renders — a backend shape change must never white-screen
   the cabinet. */
const COLS = [
  ["tailored", "Tailored"],
  ["applied", "Applied"],
  ["submitted", "Submitted"],
  ["interview", "Interview"],
  ["offer", "Offer"],
  ["rejected", "Rejected"],
];
const NEXT = { tailored: "applied", applied: "submitted", submitted: "interview", interview: "offer" };
const txt = (v) => (v === null || v === undefined ? "" : typeof v === "string" ? v : String(v));
const day = (ts) => (ts ? new Date(Number(ts) * 1000).toLocaleDateString() : "");

export default function Pipeline() {
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("loading…");

  async function load() {
    try {
      const r = await getJSON("/api/applications?days=365");
      const list = Array.isArray(r && r.applications) ? r.applications : [];
      setRows(list);
      setNote(
        list.length
          ? `${list.length} application${list.length === 1 ? "" : "s"}` +
            (r.incomplete ? ` · ${r.incomplete} record(s) cannot prove which resume went where` : "")
          : "Nothing tracked yet — tailor a resume on the Tailor page and it lands here."
      );
    } catch (e) {
      setErr(txt((e && e.message) || e));
      setNote("");
    }
  }
  useEffect(() => { load(); }, []);

  async function move(row, stage) {
    try {
      await patchJSON(`/api/applications/${encodeURIComponent(txt(row.job_id))}`, { stage });
      load();
    } catch (e) {
      setErr(txt((e && e.message) || e));
    }
  }

  return (
    <>
      <h1 className="page-h">Pipeline</h1>
      <p className="page-sub">
        Every application, from the job description to the documents we sent. This is the same
        record the end-of-day digest reports.
      </p>
      {err && <div className="err">{err}</div>}
      {note && <p className="muted">{note}</p>}
      <div className="kan">
        {COLS.map(([key, label]) => {
          const cards = rows.filter((r) => txt(r.stage) === key);
          return (
            <div className="kcol" key={key}>
              <h4>{label} {cards.length ? `(${cards.length})` : ""}</h4>
              {cards.map((r) => (
                <div className="kcard" key={txt(r.job_id)}>
                  <b>{txt(r.employer) || "(employer not recorded)"}</b>
                  <small>{txt(r.title) || "(role not recorded)"}</small>
                  <br />
                  <small>
                    {r.jd_url ? (
                      <a href={txt(r.jd_url)} target="_blank" rel="noreferrer">the posting</a>
                    ) : r.jd_chars ? (
                      `pasted JD · ${txt(r.jd_chars)} chars`
                    ) : (
                      <span style={{ color: "var(--red, #b91c1c)" }}>no job description on record</span>
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
                  {NEXT[key] && (
                    <>
                      <br />
                      <a href="#" onClick={(e) => { e.preventDefault(); move(r, NEXT[key]); }}>
                        → {NEXT[key]}
                      </a>
                      {" · "}
                      <a href="#" onClick={(e) => { e.preventDefault(); move(r, "rejected"); }}>
                        rejected
                      </a>
                    </>
                  )}
                </div>
              ))}
            </div>
          );
        })}
      </div>
    </>
  );
}
