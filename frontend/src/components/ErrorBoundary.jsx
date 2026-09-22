import { Component } from "react";

/* A RENDER ERROR MUST NOT BLANK THE CABINET.

   On 2026-09-22 one undefined identifier inside the new run-log card threw
   `ReferenceError: txt is not defined` the moment the first log line arrived. React does what React
   does with an uncaught render error: it unmounts the entire tree. The operator pressed "Generate"
   and got a WHITE PAGE — no sidebar, no navigation, no message, nothing to act on. The bug was one
   line; the blast radius was the whole product.

   This boundary makes that failure survivable: the page that threw is replaced by the error itself,
   in words, with a way back. Everything around it keeps working. It is not a substitute for the
   check that catches the identifier before it ships (tests/test_frontend_symbols.py) — it is the
   admission that the next one will get through something, and that a white screen is the worst
   possible way to find out. */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { err: null };
  }

  static getDerivedStateFromError(err) {
    return { err };
  }

  componentDidCatch(err, info) {
    // The console is where a developer looks; the screen is where HE looks. Both get it.
    try {
      console.error("render error:", err, info && info.componentStack);
    } catch { /* never throw from the thing that handles throws */ }
  }

  render() {
    if (!this.state.err) return this.props.children;
    const msg = String((this.state.err && this.state.err.message) || this.state.err);
    return (
      <div className="card" style={{ margin: 16, borderColor: "#dc2626" }}>
        <h3 style={{ marginTop: 0, color: "#dc2626" }}>This page hit an error and stopped</h3>
        <p style={{ fontSize: 13 }}>
          Nothing you typed was lost on the server — the documents, the pipeline and the portfolio
          are stored there, not here. This is the page failing to draw itself.
        </p>
        <pre style={{
          background: "#0b1020", color: "#ffd7d7", padding: 10, borderRadius: 8,
          fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word",
        }}>{msg}</pre>
        <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
          <button className="btn" type="button" onClick={() => window.location.reload()}>
            Reload the page
          </button>
          <button className="btn ghost" type="button"
                  onClick={() => this.setState({ err: null })}>
            Try to carry on
          </button>
        </div>
      </div>
    );
  }
}
