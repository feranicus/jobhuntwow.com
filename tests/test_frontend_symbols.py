"""EVERY IDENTIFIER A PAGE CALLS MUST EXIST. Parsing is not running.

WHY THIS FILE EXISTS, precisely. On 2026-09-22 the Tailor page called `txt(...)` inside the new
run-log card. `txt` was defined in Pipeline.jsx and Security.jsx and NOT in Tailor.jsx. esbuild
parsed the file without complaint -- an undefined identifier is perfectly legal JavaScript until it
executes -- and the build shipped. It then executed the moment the first run-log line arrived,
threw `ReferenceError: txt is not defined`, React unmounted the whole tree, and the operator got a
WHITE PAGE at the exact moment he pressed Generate:

    Uncaught ReferenceError: txt is not defined       index-C-wYg724.js:40

"it parses" was the only evidence anyone had, and parsing was never the question. This check asks
the question that was: is every function this file CALLS actually defined in it, imported into it,
or a browser/JS global? It is deliberately crude -- a regex over source, not a JS parser -- because
the defect it prevents is crude, and a check that needs a JavaScript engine would not run here.

It reports UNKNOWN names, and an unknown name is a FAILURE, not a warning: the last one cost a
white screen in production.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "frontend", "src")

# Things the browser and the language provide. Not "names we are tired of seeing" -- each of these
# genuinely exists at runtime with no import.
GLOBALS = {
    "fetch", "setTimeout", "clearTimeout", "setInterval", "clearInterval", "alert", "confirm",
    "prompt", "encodeURIComponent", "decodeURIComponent", "parseInt", "parseFloat", "isNaN",
    "String", "Number", "Boolean", "Object", "Array", "JSON", "Date", "Math", "Promise", "Error",
    "Map", "Set", "RegExp", "FormData", "Blob", "URL", "URLSearchParams", "AbortController",
    "requestAnimationFrame", "structuredClone", "queueMicrotask", "console", "window", "document",
    "navigator", "performance", "localStorage", "sessionStorage", "crypto", "btoa", "atob",
    # React hooks are imported explicitly, but these read as calls in JSX callbacks too often to
    # be worth special-casing one by one when the import line already proves them.
    "require", "import",
}
# Called on an object (`foo.bar(`) -- the property is not a free identifier, so it is not our
# business. Only BARE calls are.
CALL = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\s*\(")
# What the file itself provides.
DECL = re.compile(r"\b(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)")
DESTRUCT = re.compile(r"\b(?:const|let|var)\s*\{([^}]*)\}\s*=")
# `const [thing, setThing] = useState(...)` - the React idiom this whole app is built on. Missing
# it made the first run of this check report 60 "undefined" setters, which is the shape of a check
# nobody will read twice.
ARRAY_DESTRUCT = re.compile(r"\b(?:const|let|var)\s*\[([^\]]*)\]\s*=")
IMPORTED = re.compile(r"import\s+(?:\{([^}]*)\}|([A-Za-z_$][\w$]*))[^;]*from", re.S)
PARAMS = re.compile(r"(?:function\s*[\w$]*\s*\(([^)]*)\)|\(([^)]*)\)\s*=>|([A-Za-z_$][\w$]*)\s*=>)")
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "typeof", "new", "await", "yield",
            "function", "super", "this", "void", "delete", "in", "of", "do", "else", "case",
            # `async (` and `async function (` read as a call to `async`; they are not.
            "async", "instanceof", "throw",
            # class members read as bare calls when they are declared: `constructor(props) {`,
            # `render() {`. They are definitions, not calls to a free identifier.
            "constructor", "render", "get", "set", "static"}

fails = []


def check(cond, name, detail=""):
    print(("  ok    " if cond else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def blank_template_text(src: str) -> str:
    """Blank the TEXT of a template literal and KEEP the `${...}` expressions.

    THE FIRST VERSION BLANKED THE WHOLE LITERAL, and the defect this file exists for lived inside
    one: `${logState.pct || 0}% . ${txt(logState.msg)}`. So the check could not see the very call
    that white-screened the page -- a check that cannot see its subject, which is the defect class
    above all others here. Blanking only the literal halves keeps prose out and code in.
    """
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "`":
            out.append(c)
            i += 1
            depth = 0
            while i < n:
                ch = src[i]
                if ch == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if depth == 0 and ch == "`":
                    out.append(ch)
                    i += 1
                    break
                if depth == 0 and ch == "$" and i + 1 < n and src[i + 1] == "{":
                    out.append("${")
                    depth = 1
                    i += 2
                    continue
                if depth > 0:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    out.append(ch)          # INSIDE ${...} is real code: keep every character
                    i += 1
                    continue
                out.append(" " if not ch.isspace() else ch)
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def defined_names(src: str) -> set:
    names = set(DECL.findall(src))
    for blob in DESTRUCT.findall(src):
        names |= {n.strip().split(":")[-1].strip() for n in blob.split(",") if n.strip()}
    for blob in ARRAY_DESTRUCT.findall(src):
        names |= {n.strip() for n in blob.split(",") if n.strip()}
    for braced, plain in IMPORTED.findall(src):
        if plain:
            names.add(plain.strip())
        for n in (braced or "").split(","):
            n = n.strip()
            if n:
                names.add(n.split(" as ")[-1].strip())
    for a, b, c in PARAMS.findall(src):
        for blob in (a, b, c):
            for n in (blob or "").replace("{", " ").replace("}", " ").replace("(", " ").replace(")", " ").split(","):
                n = n.strip().split("=")[0].strip().split(":")[-1].strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*", n or ""):
                    names.add(n)
    return names


files = []
for base, _d, fs in os.walk(SRC):
    for f in fs:
        if f.endswith((".jsx", ".js")):
            files.append(os.path.join(base, f))
check(len(files) >= 8, "the frontend sources were found", "%d file(s)" % len(files))

for path in sorted(files):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    # Strip strings and comments: a word inside a template literal or a comment is not a call.
    body = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    body = re.sub(r"(?m)//.*$", " ", body)
    # JSX TEXT IS PROSE, NOT CODE. "· 3 link(s) read" and "12 line(s) scanned" are sentences a
    # human reads; treating them as calls to `link` and `line` is the kind of false positive that
    # gets a check switched off within a week.
    def _blank(m):
        return m.group(1) + re.sub(r"[^\s]", " ", m.group(2)) + m.group(3)

    # TEMPLATES FIRST. The JSX prose pass blanks everything between `}` and `{`, which in
    # `` `${a}% . ${txt(b)}` `` includes the `$` of the NEXT expression -- so the template scanner
    # then read `{txt(b)}` as literal text and blanked the one call this file exists to catch.
    # Measured: with the helper deleted, the check still passed. Order is the fix.
    body = blank_template_text(body)
    # Prose sits between any two JSX delimiters, not only between two tags: `{n(x)} line(s)
    # scanned, {n(y)} ours.` is three text runs bounded by }, { and <.
    for _pat in (r"(>)([^<>{}]*)(<)", r"(\})([^<>{}]*)(\{)", r"(\})([^<>{}]*)(<)",
                 r"(>)([^<>{}]*)(\{)"):
        body = re.sub(_pat, _blank, body)
    body = re.sub(r"'(?:[^'\\]|\\.)*'", "''", body)
    body = re.sub(r'"(?:[^"\\]|\\.)*"', '""', body)
    known = defined_names(src) | GLOBALS | KEYWORDS
    unknown = sorted({n for n in CALL.findall(body)
                      if n not in known and not n[0].isupper()})
    rel = os.path.relpath(path, ROOT).replace("\\", "/")
    check(not unknown, "%s calls nothing it has not got" % rel,
          ("UNDEFINED: %s" % ", ".join(unknown)) if unknown else "")

# NEGATIVE: the check must actually catch the defect it was written for.
probe = "const a = 1;\nfunction X(){ return zzzNotDefined(a); }\n"
known = defined_names(probe) | GLOBALS | KEYWORDS
missed = sorted({n for n in CALL.findall(probe) if n not in known and not n[0].isupper()})
check(missed == ["zzzNotDefined"],
      "NEGATIVE: an undefined call IS detected (the check can fail)", str(missed))
check("txt" in defined_names(open(os.path.join(SRC, "pages", "Tailor.jsx"),
                                  encoding="utf-8").read()),
      "and the identifier that white-screened the cabinet is now defined where it is used")

print("-" * 78)
print("%d check(s) failed" % len(fails))
if fails:
    print("FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
