/* probe.js — what THIS browser could actually do, reported once per session.

   He asked for the WebRTC + HTTP/3 method by name. Two honest limits, which is why this file is
   small and why nothing it returns ever blocks anyone:

     * IT NEVER RUNS FOR A SCRAPER. curl, python-requests and every scanner execute no JavaScript,
       so this cannot see the bots that actually arrive — the server-side signals do that work.
       What it CAN separate is a real browser from a headless/cloud one driving the cabinet.
     * IT ACCUSES REAL PEOPLE IF YOU LET IT. A corporate firewall blocks UDP: no HTTP/3, no ICE
       candidates. Safari and privacy extensions restrict candidates. So "no UDP" is reported as
       NOT DETERMINABLE, never as a bot.

   PRIVACY: the address WebRTC reveals is sent once, compared on the server, and dropped there —
   only the boolean survives. Nothing here is stored in the browser either. */

const TIMEOUT_MS = 1500;

function h3Used() {
  // The page's own navigation entry says which protocol carried it. "h3" means UDP worked end to
  // end, with no TCP-only proxy in the path — measured, not asked.
  try {
    const e = performance.getEntriesByType("navigation")[0];
    return !!e && /^h3/i.test(e.nextHopProtocol || "");
  } catch { return false; }
}

function iceFacts() {
  // Resolves with {ice, srflx, publicIp} — or {ice:false} when UDP is blocked, which is normal.
  return new Promise((resolve) => {
    let done = false;
    const out = { ice: false, srflx: false, publicIp: "" };
    const finish = () => { if (!done) { done = true; try { pc.close(); } catch {} resolve(out); } };
    let pc;
    try {
      pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
    } catch { return resolve(out); }
    pc.onicecandidate = (ev) => {
      const c = ev && ev.candidate && ev.candidate.candidate;
      if (!c) return finish();
      out.ice = true;
      if (/ typ srflx /.test(c)) {
        out.srflx = true;
        const m = c.match(/ ([0-9]{1,3}(?:\.[0-9]{1,3}){3}) /);
        if (m) out.publicIp = m[1];
        finish();
      }
    };
    try {
      pc.createDataChannel("p");
      pc.createOffer().then((o) => pc.setLocalDescription(o)).catch(finish);
    } catch { finish(); }
    setTimeout(finish, TIMEOUT_MS);
  });
}

// THE WEBRTC HALF IS OPT-IN, AND IT IS OFF. It reveals an address the user chose to hide, it
// cannot see a scripted client at all, and it flags corporate networks that block UDP - three
// reasons the sibling estate evaluated the same technique and declined it. Set
// VITE_JHW_PROBE_WEBRTC=1 at build time to turn it on; the server also drops the fields unless
// JHW_PROBE_WEBRTC=1, so BOTH ends must agree before anybody is unmasked.
const WEBRTC_ON = (() => {
  try { return import.meta.env && import.meta.env.VITE_JHW_PROBE_WEBRTC === "1"; }
  catch { return false; }
})();

export async function reportProbe() {
  try {
    if (sessionStorage.getItem("jhwProbe")) return;   // once per session; this is not telemetry
    sessionStorage.setItem("jhwProbe", "1");
  } catch { /* private mode: just run it once per load */ }
  try {
    const ice = WEBRTC_ON ? await iceFacts() : { ice: false, srflx: false, publicIp: "" };
    const ua = navigator.userAgent || "";
    await fetch("/api/probe", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        h3: h3Used(),
        ice: ice.ice,
        srflx: ice.srflx,
        publicIp: ice.publicIp,                       // compared server-side, then dropped
        webdriver: !!navigator.webdriver,
        headlessUa: /headless|phantomjs|electron\//i.test(ua),
        plugins: (navigator.plugins && navigator.plugins.length) || 0,
        langs: (navigator.languages && navigator.languages.length) || 0,
      }),
    });
  } catch { /* a probe that fails must cost the page nothing */ }
}
