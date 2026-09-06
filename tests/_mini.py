"""A ~40-line stdlib stand-in for pytest, because jobhuntwow's suites must run with no setup on
the operator's machine (five wasted ships were spent relearning that). Test functions may take
`monkeypatch`, `tmp_path` and `capsys` and get the same behaviour they would under pytest. Under
real pytest these files still collect normally; `run_module` is only used via __main__."""
import inspect, io, os, pathlib, sys, tempfile, traceback


class _MP:
    def __init__(self): self._u = []
    def setattr(self, obj, name, val):
        self._u.append((obj, name, getattr(obj, name, None), hasattr(obj, name))); setattr(obj, name, val)
    def setenv(self, k, v):
        self._u.append((os.environ, k, os.environ.get(k), k in os.environ)); os.environ[k] = v
    def undo(self):
        for obj, name, old, had in reversed(self._u):
            if obj is os.environ:
                (os.environ.__setitem__(name, old) if had else os.environ.pop(name, None))
            elif had: setattr(obj, name, old)
            else: delattr(obj, name)


class _Cap:
    def __init__(self): self.buf = io.StringIO(); self._old = sys.stdout; sys.stdout = self.buf
    def readouterr(self):
        v = self.buf.getvalue(); self.buf.seek(0); self.buf.truncate(0)
        return type("R", (), {"out": v, "err": ""})()
    def stop(self): sys.stdout = self._old


def run_module(g):
    fails = 0
    for name, fn in sorted(g.items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        mp, cap, kw = _MP(), None, {}
        for p in inspect.signature(fn).parameters:
            if p == "monkeypatch": kw[p] = mp
            elif p == "tmp_path": kw[p] = pathlib.Path(tempfile.mkdtemp())
            elif p == "capsys": cap = kw[p] = _Cap()
        try:
            fn(**kw); ok = True; err = ""
        except Exception:
            ok = False; err = traceback.format_exc()[-800:]
        finally:
            if cap: cap.stop()
            mp.undo()
        if ok: print("  ok    %s" % name)
        else: fails += 1; print("  FAIL  %s\n%s" % (name, err))
    print("%d failed" % fails); return fails
