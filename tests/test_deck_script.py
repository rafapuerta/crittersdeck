"""The plugin's MCP server for messages between sessions (plugin/scripts/deck.py), run as Claude Code runs it."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "plugin/scripts/deck.py"
VERSION = json.loads((SCRIPT.parent.parent / ".claude-plugin/plugin.json").read_text())["version"]
PEERS = {"peers": [{"id": "s1", "machine": "laptop", "title": "Fix", "folder": "/r", "status": "idle", "you": True}],
         "machines": ["laptop"]}


def board(status=200, body=None, raw=None):
    """A fake board answering every POST with `status` and `body` (JSON) or `raw` bytes."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen.append((self.path, self.headers["authorization"], self.headers["user-agent"], payload))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(raw if raw is not None else json.dumps(body or {}).encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}", seen


def call_tool(tmp_path, url, name, arguments=None):
    """Starts the MCP server, calls one tool, returns (text, isError) and the server's stderr."""
    env = {"DECK_TOKEN": "cd_secret", "DECK_ON": "true", "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"),
           "CLAUDE_CODE_SESSION_ID": "s1", "PATH": "/usr/bin:/bin",
           **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ else {})}
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}},
    ]
    p = subprocess.run([sys.executable, "-I", str(SCRIPT), "mcp", url], input="\n".join(map(json.dumps, requests)),
                       env=env, capture_output=True, text=True, timeout=30)
    init, answer = [json.loads(line) for line in p.stdout.splitlines()]
    assert init["result"]["serverInfo"] == {"name": "crittersdeck", "version": VERSION}
    result = answer["result"]
    return result["content"][0]["text"], result["isError"], p.stderr


@pytest.fixture
def stop():
    servers = []
    yield servers.append
    for s in servers:
        s.shutdown()


def test_peers_names_the_plugin_as_user_agent(tmp_path, stop):
    server, url, seen = board(body=PEERS)
    stop(server)
    text, error, _ = call_tool(tmp_path, url, "deck_peers")
    assert not error and "s1 · laptop · Fix · /r · idle (this session)" in text
    # Cloudflare in front of a board blocks urllib's default User-Agent (error 1010)
    assert seen == [("/ingest", "Bearer cd_secret", f"crittersdeck-plugin/{VERSION}",
                     {"hook_event_name": "DeckPeers", "session_id": "s1"})]


def test_http_error_without_json_says_the_status(tmp_path, stop):
    server, url, _ = board(status=403, raw=b"error code: 1010")
    stop(server)
    text, error, stderr = call_tool(tmp_path, url, "deck_peers")
    assert error and text == "The board answered HTTP 403."
    assert "cd_secret" not in text + stderr


def test_board_error_is_passed_on(tmp_path, stop):
    server, url, _ = board(status=401, body={"error": "unknown source"})
    stop(server)
    for name, args in (("deck_peers", {}), ("deck_inbox", {}), ("deck_send", {"to": "all", "text": "hi"})):
        assert call_tool(tmp_path, url, name, args)[:2] == ("unknown source", True)


def test_unreachable_board_is_a_network_error(tmp_path):
    text, error, _ = call_tool(tmp_path, "http://127.0.0.1:9", "deck_send", {"to": "all", "text": "hi"})
    assert error and text == "The board cannot be reached (network error)."
