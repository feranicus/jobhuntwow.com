import { useEffect, useState } from "react";
import { getJSON, postJSON } from "../api.js";

export default function Connections() {
  const [c, setC] = useState({});
  const [models, setModels] = useState([]);
  const [model, setModel] = useState("");
  const [tg, setTg] = useState("");
  const [wa, setWa] = useState("");
  const [li, setLi] = useState("");
  const load = () => getJSON("/api/connections").then(setC);
  useEffect(() => { load(); getJSON("/api/models").then(d => { setModels(d.models || []); setModel(d.default || ""); }); }, []);

  const save = async (section, patch) => setC(await postJSON("/api/connections", { section, patch }));
  const on = (k) => c?.[k]?.connected;
  const Pill = ({ k }) => <span className={"pill " + (on(k) ? "on" : "off")}>{on(k) ? "connected" : "not set"}</span>;

  return (
    <>
      <h1 className="page-h">Connections</h1>
      <p className="page-sub">Wire up the agent. Secrets are stored on the server, never shown back to the browser.</p>
      <div className="cards" style={{gridTemplateColumns:"repeat(auto-fit,minmax(320px,1fr))"}}>

        <div className="card">
          <h3>🧠 Qwen 3.x (DigitalOcean) <Pill k="qwen" /></h3>
          <p>Serverless Inference. The server holds your model access key; pick the model to use.</p>
          <div className="label">Model</div>
          <select value={model} onChange={e => setModel(e.target.value)}>
            <option value="">{models.length ? "— select —" : "no models (set key on server)"}</option>
            {models.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
          <div style={{marginTop:10}}><button className="btn sm" onClick={() => save("qwen", { model })} disabled={!model}>Use this model</button></div>
        </div>

        <div className="card">
          <h3>🪽 Hermes <Pill k="hermes" /></h3>
          <p>The orchestration agent that runs your search end-to-end.</p>
          <div style={{marginTop:10}}><button className="btn sm" onClick={() => save("hermes", { connected: !on("hermes") })}>{on("hermes") ? "Turn off" : "Enable Hermes"}</button></div>
        </div>

        <div className="card">
          <h3>💬 Telegram <Pill k="telegram" /></h3>
          <p>Paste a bot token from @BotFather to talk to Hermes from Telegram.</p>
          <div className="label">Bot token</div>
          <input className="input" type="password" placeholder="123456:ABC…" value={tg} onChange={e => setTg(e.target.value)} />
          <div style={{marginTop:10}}><button className="btn sm" onClick={() => save("telegram", { bot_token: tg })} disabled={!tg}>Connect Telegram</button></div>
        </div>

        <div className="card">
          <h3>🟢 WhatsApp <Pill k="whatsapp" /></h3>
          <p>Connect a WhatsApp number (Cloud API / provider) for the same agent.</p>
          <div className="label">Phone / number id</div>
          <input className="input" placeholder="+49…" value={wa} onChange={e => setWa(e.target.value)} />
          <div style={{marginTop:10}}><button className="btn sm" onClick={() => save("whatsapp", { phone: wa, connected: !!wa })} disabled={!wa}>Connect WhatsApp</button></div>
        </div>

        <div className="card">
          <h3>🔗 LinkedIn <Pill k="linkedin" /></h3>
          <p>Paste your exported LinkedIn cookies (JSON) so the scout runs in <i>your</i> session. Stored server-side, never echoed back.</p>
          <div className="label">cookies.json</div>
          <textarea rows={3} placeholder='[{"name":"li_at",...}]' value={li} onChange={e => setLi(e.target.value)} />
          <div style={{marginTop:10}}><button className="btn sm" onClick={() => { save("linkedin", { cookies: li }); setLi(""); }} disabled={!li}>Connect LinkedIn</button></div>
        </div>

      </div>
    </>
  );
}
