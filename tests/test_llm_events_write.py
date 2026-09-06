"""A failed append to the shared events.log must be LOUD, once, and never raise.

jhw-web runs as UID 10001; events.log on the shared volume is root 0644. For the life of the
project every append raised PermissionError and was swallowed, so jobhuntwow never wrote one line
to the file promtail tails and the spend investigation was blind to it for a week."""
import importlib, io, json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))


def _unwritable(tmp_path):
    f = tmp_path / "blocker"; f.write_text("x")
    return str(f / "events.log")          # parent is a FILE: cannot be created on any OS


def test_telemetry_says_so_once_and_keeps_going(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVENTS_LOG", _unwritable(tmp_path))
    from app import telemetry as T
    importlib.reload(T)
    T.emit(evt="http", path="/x"); T.emit(evt="http", path="/y")
    out = capsys.readouterr().out
    assert out.count('"events_log_unwritable"') == 1
    assert out.count('"path": "/x"') == 1 and out.count('"path": "/y"') == 1


def test_llm_events_says_so_once_and_never_raises(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVENTS_LOG", _unwritable(tmp_path))
    from app import llm_events as E
    importlib.reload(E)
    assert E.record("deepseek-3.2", {"prompt_tokens": 1, "completion_tokens": 1}) is not None
    assert E.record("deepseek-3.2", {"prompt_tokens": 1, "completion_tokens": 1}) is not None
    assert capsys.readouterr().out.count('"events_log_unwritable"') == 1


def test_deploy_makes_the_shared_file_writable_and_proves_it():
    src = io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "deploy_direct.py"), encoding="utf-8").read()
    i = src.index("up -d --force-recreate")
    seg = src[i:]
    assert "chmod a+w /var/log/colt/events.log" in seg
    assert "EVENTS_LOG_UNWRITABLE" in seg, "the deploy must FAIL LOUDLY if jhw still cannot append"


if __name__ == "__main__":
    from _mini import run_module
    raise SystemExit(1 if run_module(dict(globals())) else 0)
