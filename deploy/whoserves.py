"""whoserves.py — runs INSIDE the droplet. Answers ONE question with facts:

    which Caddy route actually serves https://jobhuntwow.com/api/health, and what does it do?

Reads Caddy's ADAPTED JSON config (the real, expanded routing table — env placeholders already
resolved) instead of eyeballing the Caddyfile. Route order in that JSON is the order Caddy
evaluates, so the FIRST route whose host matcher matches is the winner.
"""
import json, sys

HOSTS = {"jobhuntwow.com", "www.jobhuntwow.com"}


def handler_summary(route):
    bits = []
    for h in route.get("handle", []):
        t = h.get("handler")
        if t == "reverse_proxy":
            ups = ",".join(u.get("dial", "?") for u in h.get("upstreams", []))
            bits.append(f"reverse_proxy -> {ups}")
        elif t == "file_server":
            bits.append(f"file_server (root set by vars)")
        elif t == "vars":
            if "root" in h:
                bits.append(f"root = {h['root']}")
        elif t == "static_response":
            bits.append(f"static_response {h.get('status_code','')} -> {h.get('headers',{}).get('Location',[''])[0] if h.get('headers') else ''}")
        elif t == "subroute":
            for sr in h.get("routes", []):
                bits.append("  [sub] " + handler_summary(sr))
        elif t == "headers":
            bits.append("headers")
        else:
            bits.append(t or "?")
    return "; ".join(b for b in bits if b)


def hosts_of(route):
    out = []
    for m in route.get("match", []) or []:
        out += m.get("host", [])
    return out


def main():
    cfg = json.load(sys.stdin)
    servers = cfg.get("apps", {}).get("http", {}).get("servers", {})
    print(f"servers: {list(servers)}")
    for name, srv in servers.items():
        print(f"\n=== server '{name}' listen={srv.get('listen')} ===")
        for i, route in enumerate(srv.get("routes", [])):
            hs = hosts_of(route)
            match_ours = (not hs) or bool(set(hs) & HOSTS)
            flag = "  <-- MATCHES jobhuntwow.com" if match_ours else ""
            label = ", ".join(hs) if hs else "(NO host matcher = CATCH-ALL, matches everything)"
            print(f"[{i}] {label}{flag}")
            if match_ours:
                print(f"      {handler_summary(route)}")
        # who wins
        for i, route in enumerate(srv.get("routes", [])):
            hs = hosts_of(route)
            if (not hs) or (set(hs) & HOSTS):
                print(f"\n>>> FIRST route matching jobhuntwow.com is [{i}] "
                      f"{', '.join(hs) if hs else 'CATCH-ALL'}")
                print(f">>> it does: {handler_summary(route)}")
                if not hs:
                    print(">>> VERDICT: a CATCH-ALL route sits BEFORE our host route and swallows the domain.")
                break


if __name__ == "__main__":
    main()
