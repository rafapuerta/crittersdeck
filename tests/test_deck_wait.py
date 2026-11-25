"""deck.py wait (wakes an idle session when messages arrive) and the Stop hook that asks for it."""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "plugin/scripts/deck.py"
MSG = {"id": 1, "from": "peer", "from_id": "src:ab12", "to_kind": "session", "text": "PR ready", "ref": "", "from_board": False}


@pytest.fixture
def board():
    """A fake board: DeckWait answers `waiting` (or {} as an old board), DeckInbox the inbox."""
    state = {"inbox": [], "waiting": 0, "old": False, "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            state["calls"].append((self.headers["authorization"], body))
            name = body["hook_event_name"]
            if name == "DeckWait":
                reply = {} if state["old"] else {"waiting": state["waiting"]}
            else:
                reply, state["inbox"] = {"messages": state["inbox"]}, []
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()


def env(tmp_path, **kw):
    return {"PATH": "/usr/bin:/bin", "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"), "CLAUDE_CODE_SESSION_ID": "s1",
            "DECK_WAIT_RETRY": "0.1", **kw}


def start_wait(url, data, tmp_path, **kw):
    return subprocess.Popen([sys.executable, "-I", str(SCRIPT), "wait", url, str(data)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env(tmp_path, **kw))


def hook(url, data, event, **kw):
    p = subprocess.run([sys.executable, "-I", str(SCRIPT), "hook", url, str(data)], input=json.dumps(event),
                       capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "CLAUDE_PLUGIN_OPTION_TOKEN": "cd_t", **kw})
    assert p.returncode == 0 and p.stderr == ""
    return json.loads(p.stdout) if p.stdout else None


STOP = {"hook_event_name": "Stop", "session_id": "s1", "stop_hook_active": False}


def test_wait_exits_on_messages_with_a_notice_and_cleans_up(board, tmp_path):
    url, state = board
    (tmp_path / "token").write_text("cd_saved")
    p = start_wait(url, tmp_path, tmp_path)
    pid = tmp_path / "wait-s1.pid"
    for _ in range(100):
        if pid.exists():
            break
        time.sleep(0.05)
    assert pid.read_text() == str(p.pid)
    time.sleep(0.3)
    assert p.poll() is None  # nothing waiting: it keeps going
    state["waiting"] = 2
    out, err = p.communicate(timeout=10)
    assert p.returncode == 0 and err == ""
    assert out.strip() == "2 new crittersdeck messages; read them with deck_inbox."
    assert not pid.exists()
    auth, body = state["calls"][-1]
    assert auth == "Bearer cd_saved" and body == {"hook_event_name": "DeckWait", "session_id": "s1", "wait": 50}
    assert not any(b["hook_event_name"] == "DeckInbox" for _, b in state["calls"])  # never takes the messages


def test_wait_times_out_and_refuses_doubles_and_old_boards(board, tmp_path):
    url, state = board
    (tmp_path / "token").write_text("cd_saved")
    p = start_wait(url, tmp_path, tmp_path, DECK_WAIT_FOR="0.4")
    out, _ = p.communicate(timeout=10)
    assert p.returncode == 0 and "stopped waiting" in out and not (tmp_path / "wait-s1.pid").exists()
    (tmp_path / "wait-s1.pid").write_text("1")  # a live wait (fresh heartbeat)
    out, _ = start_wait(url, tmp_path, tmp_path).communicate(timeout=10)
    assert "already waiting" in out
    os.utime(tmp_path / "wait-s1.pid", (0, 0))  # stale: its process is gone
    state["old"] = True
    p = start_wait(url, tmp_path, tmp_path)
    out, _ = p.communicate(timeout=10)
    assert p.returncode == 1 and "too old" in out
    # its end wakes the session; the Stop hook must not ask again, or the session would loop
    assert hook(url, tmp_path, STOP) is None
    (tmp_path / "token").unlink()
    p = start_wait(url, tmp_path, tmp_path)
    assert p.communicate(timeout=10)[0].startswith("crittersdeck wait: no token") and p.returncode == 1


def test_stop_with_messages_delivers_them(board, tmp_path):
    url, state = board
    state["inbox"] = [MSG]
    out = hook(url, tmp_path, STOP)
    assert out["decision"] == "block" and "PR ready" in out["reason"]
    assert not (tmp_path / "asked-s1").exists()


def test_stop_without_messages_asks_for_wait_once(board, tmp_path):
    url, _ = board
    out = hook(url, tmp_path, STOP)
    assert out["decision"] == "block"
    assert "run_in_background" in out["reason"] and f"deck.py wait {url} {tmp_path}" in out["reason"]
    assert (tmp_path / "token").read_text() == "cd_t"  # for `wait`, which has no plugin options
    if os.name == "posix":
        assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600
    assert hook(url, tmp_path, STOP) is None  # asked already this turn: no loop
    assert hook(url, tmp_path, {**STOP, "stop_hook_active": True}) is None
    hook(url, tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "s1"})  # a new prompt...
    assert hook(url, tmp_path, STOP)["decision"] == "block"  # ...may ask again


def test_stop_with_a_live_wait_lets_the_session_stop(board, tmp_path):
    url, _ = board
    (tmp_path / "wait-s1.pid").write_text("123")
    assert hook(url, tmp_path, STOP) is None
    os.utime(tmp_path / "wait-s1.pid", (0, 0))  # its heartbeat stopped: the wait is gone
    assert hook(url, tmp_path, STOP)["decision"] == "block"


def test_stop_does_not_ask_when_off_down_or_without_data(board, tmp_path):
    url, _ = board
    assert hook(url, tmp_path, STOP, CLAUDE_PLUGIN_OPTION_AGENT_MESSAGES="false") is None
    assert hook("http://127.0.0.1:9", tmp_path, STOP) is None  # board down: not worth a turn
    assert hook(url, "", STOP) is None  # no data folder for the PID file
    assert not (tmp_path / "asked-s1").exists()


def test_wait_finds_the_session_through_claude_pid(board, tmp_path):
    url, state = board
    (tmp_path / "token").write_text("t")
    (tmp_path / "cfg" / "sessions").mkdir(parents=True)
    (tmp_path / "cfg" / "sessions" / "4242.json").write_text(json.dumps({"sessionId": "resumed"}))
    state["waiting"] = 1
    p = start_wait(url, tmp_path, tmp_path, CLAUDE_PID="4242")
    p.communicate(timeout=10)
    assert state["calls"][-1][1]["session_id"] == "resumed"


def test_wait_gives_up_on_a_session_the_board_forgot(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(409)
            self.end_headers()
            self.wfile.write(b'{"error": "this session is not on the board yet"}')

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (tmp_path / "token").write_text("t")
    p = start_wait(f"http://127.0.0.1:{server.server_port}", tmp_path, tmp_path)
    out, _ = p.communicate(timeout=10)
    server.shutdown()
    assert p.returncode == 1 and "not on the board" in out and (tmp_path / "nowait-s1").exists()
