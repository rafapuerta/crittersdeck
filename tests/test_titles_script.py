"""The plugin's title step (plugin/scripts/titles.py), run as Claude Code would run it."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

agents = []  # User-Agent of each request to the board
SCRIPT = Path(__file__).parent.parent / "plugin/scripts/titles.py"


@pytest.fixture
def board():
    received = []
    agents.clear()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            received.append((self.path, self.headers["authorization"], json.loads(body)))
            agents.append(self.headers["user-agent"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()


def run(tmp_path, url, event, api=None, **extra):
    env = {**extra,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"),
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
        "CLAUDE_PLUGIN_OPTION_TOKEN": "cd_secret",
        **({"CRITTERSDECK_TEST_API": api} if api else {}),
        "PATH": "/usr/bin:/bin",
    }
    p = subprocess.run([sys.executable, str(SCRIPT), url], input=json.dumps(event), env=env,
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""  # never adds text to the session


def transcript(tmp_path, *titles):
    path = tmp_path / "t.jsonl"
    lines = [{"type": "user", "message": "hi"}] + [{"type": "ai-title", "aiTitle": t} for t in titles]
    path.write_text("\n".join(map(json.dumps, lines)) + "\nnot json \"ai-title\"\n")
    return str(path)


def test_terminal_session_sends_latest_ai_title(tmp_path, board):
    url, received = board
    event = {"session_id": "abc", "transcript_path": transcript(tmp_path, "Old", "Fix  the login")}
    run(tmp_path, url, event)
    [(path, auth, body)] = received
    assert (path, auth, body["hook_event_name"], body["title"]) == ("/ingest", "Bearer cd_secret", "SessionInfo", "Fix the login")
    # Cloudflare in front of a board blocks urllib's default User-Agent (error 1010)
    assert agents[0].startswith("crittersdeck-plugin/") and not agents[0].endswith("unknown")


def test_linked_session_gets_title_from_claude_ai(tmp_path, board):
    url, received = board
    api_seen = []

    class Api(BaseHTTPRequestHandler):
        def do_GET(self):
            api_seen.append((self.path, self.headers["authorization"], self.headers["anthropic-beta"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"id": "session_01X", "title": "Agentes temáticos", "external_metadata": {
                "context_usage": {"used_tokens": 5000.0, "max_tokens": 1000000}, "usage": {"cost_usd": 1.5},
                "last_served_model": "claude-opus-5-5"}}).encode())

        def log_message(self, *a):
            pass

    api = HTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    sessions = tmp_path / "cfg/sessions"
    sessions.mkdir(parents=True)
    (sessions / "1.json").write_text(json.dumps({"sessionId": "other", "bridgeSessionId": "session_01Y"}))
    (sessions / "2.json").write_text(json.dumps({"sessionId": "abc", "bridgeSessionId": "session_01X"}))
    (sessions / "3.json").write_text("{broken")
    (tmp_path / "cfg/.credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "oauth-secret", "expiresAt": 4_000_000_000_000}}))
    try:
        run(tmp_path, url, {"session_id": "abc", "transcript_path": transcript(tmp_path, "From transcript")},
            api=f"http://127.0.0.1:{api.server_port}/v1/sessions/")
    finally:
        api.shutdown()
    assert api_seen == [("/v1/sessions/session_01X", "Bearer oauth-secret", "ccr-byoc-2025-07-29")]
    # the board gets the title and its own token, never the Claude credential
    [(path, auth, body)] = received
    assert (path, auth, body["title"]) == ("/ingest", "Bearer cd_secret", "Agentes temáticos")
    assert body["info"] == {"context_used": 5000, "context_max": 1000000, "cost_usd": 1.5, "model": "claude-opus-5-5",
                            "tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cache_write": 0, "turns": 0}
    assert "oauth-secret" not in json.dumps(received)


def test_malformed_claude_ai_id_is_not_used(tmp_path, board):
    url, received = board
    sessions = tmp_path / "cfg/sessions"
    sessions.mkdir(parents=True)
    (sessions / "1.json").write_text(json.dumps({"sessionId": "abc", "bridgeSessionId": "session_01X\n"}))
    (tmp_path / "cfg/.credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 4_000_000_000_000}}))
    # falls back to the transcript, without any request to claude.ai
    run(tmp_path, url, {"session_id": "abc", "transcript_path": transcript(tmp_path, "T")}, api="http://127.0.0.1:9/")
    assert [r[2].get("title") for r in received] == ["T"]


def test_linked_session_without_valid_credential_sends_nothing(tmp_path, board):
    url, received = board
    sessions = tmp_path / "cfg/sessions"
    sessions.mkdir(parents=True)
    (sessions / "123.json").write_text(json.dumps({"sessionId": "abc", "bridgeSessionId": "session_01X"}))
    (tmp_path / "cfg/.credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 0}}))
    # an expired token is not used: what the transcript has (its ai-title, its figures) still goes
    run(tmp_path, url, {"session_id": "abc", "transcript_path": transcript(tmp_path, "T")})
    [(_, _, body)] = received
    assert body["title"] == "T" and body["info"]["turns"] == 0 and "cost_usd" not in body["info"]


def test_usage_from_the_transcript(tmp_path, board):
    url, received = board
    lines = [
        {"type": "user", "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {
            "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}},
        {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {  # same reply, counted once
            "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}},  # not a prompt
        {"type": "user", "isMeta": True, "message": {"role": "user", "content": "meta"}},
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "subagent"}},
        {"type": "assistant", "message": {"id": "m2", "model": "claude-opus-5-5", "usage": {
            "input_tokens": 1, "output_tokens": 7, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 0}}},
        {"type": "assistant", "message": {"id": "m3", "model": "<synthetic>", "usage": {"input_tokens": 999}}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "second"}]}},
    ]
    path = tmp_path / "u.jsonl"
    path.write_text("\n".join(map(json.dumps, lines)) + "\n")
    run(tmp_path, url, {"session_id": "abc", "transcript_path": str(path)})
    [(_, _, body)] = received
    assert "title" not in body
    assert body["info"] == {"tokens_in": 11, "tokens_out": 12, "cache_read": 400, "cache_write": 20, "turns": 2,
                            "context_used": 301, "model": "claude-opus-5-5"}


def test_switched_off_sends_nothing(tmp_path, board):
    url, received = board
    for value in ("false", "0", "off"):
        run(tmp_path, url, {"session_id": "abc", "transcript_path": transcript(tmp_path, "T")}, CLAUDE_PLUGIN_OPTION_SEND_USAGE=value)
    assert received == []
    run(tmp_path, url, {"session_id": "abc", "transcript_path": transcript(tmp_path, "T")}, CLAUDE_PLUGIN_OPTION_SEND_USAGE="true")
    assert len(received) == 1


def test_bad_input_is_ignored(tmp_path, board):
    url, received = board
    run(tmp_path, url, {"session_id": "abc"})  # no transcript
    run(tmp_path, url, {"transcript_path": "/nonexistent"})
    run(tmp_path, "file:///etc/passwd", {"session_id": "abc", "transcript_path": transcript(tmp_path, "T")})
    assert received == []


def test_redirects_are_not_followed(tmp_path):
    """A redirect must not carry the Authorization header to another host."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # the "board": redirects elsewhere
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
            self.end_headers()

        def do_GET(self):  # where the redirect points
            seen.append(self.headers.get("authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        run(tmp_path, f"http://127.0.0.1:{server.server_port}",
            {"session_id": "abc", "transcript_path": transcript(tmp_path, "T")})
    finally:
        server.shutdown()
    assert seen == []
