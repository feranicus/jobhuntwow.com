import { useState, useRef, useEffect } from "react";
import { chatStream, getJSON } from "../api.js";

export default function Hermes() {
  const [model, setModel] = useState("");
  const [msgs, setMsgs] = useState([{ role: "assistant", content: "Hi, I'm Hermes. Tell me what roles you want and I'll get to work. What are you looking for?" }]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const bodyRef = useRef(null);
  useEffect(() => { getJSON("/api/models").then(d => setModel(d.default || "")); }, []);
  useEffect(() => { if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight; }, [msgs]);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    const next = [...msgs, { role: "user", content: text }];
    setMsgs([...next, { role: "assistant", content: "" }]);
    setInput(""); setBusy(true);
    try {
      await chatStream(next, model, (chunk) => {
        setMsgs((cur) => {
          const copy = cur.slice();
          copy[copy.length - 1] = { role: "assistant", content: copy[copy.length - 1].content + chunk };
          return copy;
        });
      });
    } finally { setBusy(false); }
  }
  return (
    <>
      <h1 className="page-h">Hermes {model ? <span className="tag">{model}</span> : null}</h1>
      <p className="page-sub">Live chat on your DigitalOcean Qwen. Ask it to find roles, tailor your CV, or explain a step.</p>
      <div className="chat">
        <div className="chat-body" ref={bodyRef}>
          {msgs.map((m, i) => (
            <div key={i} className={"msg " + (m.role === "user" ? "me" : "bot")}>{m.content || (busy && i === msgs.length - 1 ? "…" : "")}</div>
          ))}
        </div>
        <div className="chat-input">
          <input className="input" placeholder="Message Hermes…" value={input}
            onChange={e => setInput(e.target.value)} onKeyDown={e => e.key === "Enter" && send()} />
          <button className="btn" onClick={send} disabled={busy}>{busy ? "…" : "Send"}</button>
        </div>
      </div>
    </>
  );
}
