"""deck.py wait (wakes an idle session when messages arrive) and the Stop hook that asks for it."""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "plugin/scripts/deck.py"
MSG = {"id": 1, "from": "peer", "from_id": "src:ab12", "to_kind": "session", "text": "PR ready", "ref": "", "from_board": False}


@pytest.fixture
def board():
    """A fake board: DeckWait answers `waiting` (or {} as an old board), holding the request while it is 0 like
    the real one; DeckInbox answers the inbox."""
    state = {"inbox": [], "waiting": 0, "old": False, "calls": [], "in_room": True}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            state["calls"].append((self.headers["authorization"], body))
            name = body["hook_event_name"]
            if name == "DeckWait":
                end = time.monotonic() + body.get("wait", 0)
                while not state["waiting"] and not state["old"] and state["in_room"] and time.monotonic() < end:
                    time.sleep(0.02)
                reply = {} if state["old"] else {"waiting": state["waiting"], "in_room": state["in_room"]}
            else:
                reply, state["inbox"] = {"messages": state["inbox"], "wake": state.get("wake", state["in_room"])}, []
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
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


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_wait_ends_before_claude_codes_background_limit():
    sys.path.insert(0, str(SCRIPT.parent))
    import deck
    assert deck.WAIT_FOR + deck.WAIT_POLL + 10 < deck.BASH_TIMEOUT / 1000 == 7200


def test_wait_times_out_with_a_restart_notice_and_refuses_old_boards(board, tmp_path):
    url, state = board
    (tmp_path / "token").write_text("cd_saved")
    p = start_wait(url, tmp_path, tmp_path, DECK_WAIT_FOR="0.4")
    out, _ = p.communicate(timeout=10)
    assert p.returncode == 0 and "Start the same command again" in out and not (tmp_path / "wait-s1.pid").exists()
    (tmp_path / "wait-s1.pid").write_text(str(dead_pid()))  # left by a killed wait, heartbeat still fresh
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
    (tmp_path / "wait-s1.pid").write_text(str(os.getpid()))
    assert hook(url, tmp_path, STOP) is None
    os.utime(tmp_path / "wait-s1.pid", (0, 0))  # its heartbeat stopped: the wait is gone
    assert hook(url, tmp_path, STOP)["decision"] == "block"


@pytest.mark.skipif(os.name != "posix", reason="Windows relies on the heartbeat alone")
def test_stop_asks_again_when_the_wait_was_killed(board, tmp_path):
    url, _ = board
    (tmp_path / "wait-s1.pid").write_text(str(dead_pid()))  # SIGKILL skipped its cleanup; heartbeat fresh
    out = hook(url, tmp_path, STOP)
    assert out["decision"] == "block" and "timeout: 7200000" in out["reason"]


def wait_for_calls(state, n=1):
    for _ in range(200):
        if sum(b["hook_event_name"] == "DeckWait" for _, b in state["calls"]) >= n:
            return
        time.sleep(0.05)
    raise AssertionError("no DeckWait call")


def standby(url, state, tmp_path):
    (tmp_path / "token").write_text("t")
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    (tmp_path / "wait-s1.pid").write_text(str(owner.pid))
    p = start_wait(url, tmp_path, tmp_path)
    time.sleep(0.5)
    assert p.poll() is None and state["calls"] == []  # no exit (no wake), no polling while the owner lives
    owner.kill()  # killed: the stale file is taken over
    owner.wait()
    wait_for_calls(state)
    return p


@pytest.mark.skipif(os.name != "posix", reason="a killed owner is seen at once only on POSIX")
def test_a_second_wait_takes_over_and_ignores_the_messages_that_ended_the_first(board, tmp_path):
    url, state = board
    state["waiting"] = 2
    p = standby(url, state, tmp_path)
    assert state["calls"][0][1]["wait"] == 0  # the baseline is asked without holding
    time.sleep(0.4)
    assert p.poll() is None and (tmp_path / "wait-s1.pid").read_text() == str(p.pid)  # the 2 are not news
    state["waiting"] = 0  # the woken session read them
    time.sleep(0.4)
    state["waiting"] = 1
    out, _ = p.communicate(timeout=10)
    assert p.returncode == 0 and out.startswith("1 new crittersdeck message") and not (tmp_path / "wait-s1.pid").exists()


@pytest.mark.skipif(os.name != "posix", reason="a killed owner is seen at once only on POSIX")
def test_a_second_wait_that_took_over_with_nothing_waiting_wakes_on_a_new_message(board, tmp_path):
    url, state = board
    p = standby(url, state, tmp_path)
    time.sleep(0.4)
    assert p.poll() is None
    state["waiting"] = 1  # arrives during the held long-poll
    out, _ = p.communicate(timeout=10)
    assert p.returncode == 0 and out.startswith("1 new crittersdeck message")


def test_a_second_wait_leaves_the_owners_file_alone(board, tmp_path):
    url, state = board
    (tmp_path / "token").write_text("t")
    pid = tmp_path / "wait-s1.pid"
    pid.write_text(str(os.getpid()))
    p = start_wait(url, tmp_path, tmp_path, DECK_WAIT_FOR="0.4")
    out, _ = p.communicate(timeout=10)
    assert pid.read_text() == str(os.getpid()) and state["calls"] == [] and "another wait still serves" in out


@pytest.mark.parametrize("content", [b"", b"0", b"-5", b"abc", "\u00b2".encode(), b"\xff", b"99999999999999999999"])
def test_a_corrupt_pid_file_counts_as_no_wait(board, tmp_path, content):
    url, _ = board
    (tmp_path / "wait-s1.pid").write_bytes(content)
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


def test_stop_of_a_quiet_session_does_not_ask_for_wait(board, tmp_path):
    url, state = board
    state["wake"] = False  # in no room: its messages wait for its next prompt, it never listens
    assert hook(url, tmp_path, STOP) is None
    assert not (tmp_path / "asked-s1").exists()


def test_wait_stops_for_good_when_the_session_leaves_its_rooms(board, tmp_path):
    url, state = board
    (tmp_path / "token").write_text("cd_saved")
    p = start_wait(url, tmp_path, tmp_path)
    time.sleep(0.5)
    assert p.poll() is None  # in a room: listening
    state["in_room"] = False  # taken out of the room, or the room closed
    out, _ = p.communicate(timeout=10)
    assert p.returncode == 0 and "in no room" in out
    assert hook(url, tmp_path, STOP) is None  # and the Stop hook does not ask again while it is in no room
