import { useState } from "react";
const COLS = ["Sourced", "Applied", "HR screen", "Tech", "Final", "Offer"];
const SEED = {
  "Sourced": [["Delivery Hero","Sr Data Engineer","fit 86"],["N26","Data Platform Eng","fit 81"]],
  "Applied": [["Zalando","Analytics Engineer","Workday · #A21"]],
  "HR screen": [["Delivery Hero","Sr Data Engineer","🙂 3 Qs"]],
  "Tech": [["Personio","BI Engineer","Tue 14:00"]],
  "Final": [["HelloFresh","Data Engineer","panel Fri"]],
  "Offer": [["GetYourGuide","Senior DE","€104k"]],
};
export default function Pipeline() {
  const [board] = useState(SEED);
  return (
    <>
      <h1 className="page-h">Pipeline</h1>
      <p className="page-sub">Every role Hermes touches, as a card the agents move through the funnel. (v0.1 — live sync with the agent next.)</p>
      <div className="kan">
        {COLS.map(c => (
          <div className="kcol" key={c}>
            <h4>{c}</h4>
            {(board[c] || []).map((k, i) => (
              <div className="kcard" key={i}><b>{k[0]}</b><small>{k[1]}</small><br/><small style={{color:"var(--green)"}}>{k[2]}</small></div>
            ))}
          </div>
        ))}
      </div>
    </>
  );
}
