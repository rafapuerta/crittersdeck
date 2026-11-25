"""The plugin's title and usage step (plugin/scripts/titles.py), run as Claude Code would run it."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "plugin/scripts/titles.py"
VERSION = json.loads((SCRIPT.parent.parent / ".claude-plugin/plugin.json").read_text())["version"]


class Received(list):
    agents = None  # the User-Agent of each request, beside it


@pytest.fixture
def board():
    received, agents = Received(), []
    received.agents = agents

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


def run(tmp_path, url, event, api=None):
    env = {
        "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"),
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
        "CLAUDE_PLUGIN_OPTION_TOKEN": "cd_secret",
        **({"CRITTERSDECK_TEST_API": api} if api else {}),
        "PATH": "/usr/bin:/bin",
        **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ else {}),  # Windows sockets
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
    [(path, auth, sent)] = received
    assert (path, auth, sent["hook_event_name"], sent["session_id"], sent["title"]) == (
        "/ingest", "Bearer cd_secret", "SessionInfo", "abc", "Fix the login")
    assert sent["info"]["tokens_in"] == 0  # counts, never prompt text
    # Cloudflare in front of a board blocks urllib's default User-Agent (error 1010)
    assert received.agents == [f"crittersdeck-plugin/{VERSION}"]


def test_linked_session_gets_title_from_claude_ai(tmp_path, board):
    url, received = board
    api_seen = []

    class Api(BaseHTTPRequestHandler):
        def do_GET(self):
            api_seen.append((self.path, self.headers["authorization"], self.headers["anthropic-beta"],
                             self.headers["user-agent"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"id": "session_01X", "title": "Agentes temáticos"}).encode())

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
    assert api_seen == [("/v1/sessions/session_01X", "Bearer oauth-secret", "ccr-byoc-2025-07-29",
                         f"crittersdeck-plugin/{VERSION}")]
    # the board gets the title and its own token, never the Claude credential
    [(path, auth, sent)] = received
    assert (path, auth, sent["title"]) == ("/ingest", "Bearer cd_secret", "Agentes temáticos")
    assert "oauth-secret" not in json.dumps(sent)


def test_malformed_claude_ai_id_is_not_used(tmp_path, board):
    url, received = board
    sessions = tmp_path / "cfg/sessions"
    sessions.mkdir(parents=True)
    (sessions / "1.json").write_text(json.dumps({"sessionId": "abc", "bridgeSessionId": "session_01X\n"}))
    (tmp_path / "cfg/.credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 4_000_000_000_000}}))
    # falls back to the transcript, without any request to claude.ai
    run(tmp_path, url, {"session_id": "abc", "transcript_path": transcript(tmp_path, "T")}, api="http://127.0.0.1:9/")
    assert [r[2]["title"] for r in received] == ["T"]


def test_linked_session_without_valid_credential_keeps_the_transcript(tmp_path, board):
    url, received = board
    sessions = tmp_path / "cfg/sessions"
    sessions.mkdir(parents=True)
    (sessions / "123.json").write_text(json.dumps({"sessionId": "abc", "bridgeSessionId": "session_01X"}))
    (tmp_path / "cfg/.credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 0}}))
    # an expired token is not used (no request to claude.ai): what the transcript has is sent
    run(tmp_path, url, {"session_id": "abc", "transcript_path": transcript(tmp_path, "T")}, api="http://127.0.0.1:9/")
    assert [r[2]["title"] for r in received] == ["T"]


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
