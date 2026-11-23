"""Send the title Claude gave this session to the crittersdeck board.

Runs as a Claude Code hook of the crittersdeck-titles plugin (installing it is the opt-in). Reads the hook event
on stdin and prints nothing, so it can never add text to the session.

Where the title comes from:
- Sessions linked to claude.ai (desktop app, web, Remote Control): Claude Code's session
  registry (<config>/sessions/<pid>.json) maps this session to its claude.ai id, and the
  claude.ai sessions API returns its title. That API is not documented: if it changes, no
  title is sent and the board keeps the one from the first prompt.
- Terminal sessions: the `ai-title` entry Claude Code writes to the transcript.

The Claude credential is read from <config>/.credentials.json, used only for that request to
api.anthropic.com, and never stored or sent anywhere else. The board gets the title only.
"""

import glob
import json
import os
import re
import sys
import time
import urllib.request

API = "https://api.anthropic.com/v1/sessions/"
# Tests point the script at a local fake API; nothing but loopback is accepted.
if os.environ.get("CRITTERSDECK_TEST_API", "").startswith("http://127.0.0.1:"):
    API = os.environ["CRITTERSDECK_TEST_API"]
TIMEOUT = 5
_BRIDGE_ID = re.compile(r"session_[A-Za-z0-9]{1,64}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # urllib would forward the Authorization header to wherever a redirect points


_open = urllib.request.build_opener(_NoRedirect).open


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def bridge_id(session_id):
    """The claude.ai id of this session, from Claude Code's registry of running sessions."""
    for path in glob.glob(os.path.join(config_dir(), "sessions", "*.json")):
        entry = read_json(path)
        if isinstance(entry, dict) and entry.get("sessionId") == session_id:
            bid = entry.get("bridgeSessionId")
            if isinstance(bid, str) and _BRIDGE_ID.fullmatch(bid):
                return bid
    return None


def title_from_claude_ai(bid):
    creds = read_json(os.path.join(config_dir(), ".credentials.json")) or {}
    oauth = creds.get("claudeAiOauth") or {}
    token, expires = oauth.get("accessToken"), oauth.get("expiresAt") or 0
    if not isinstance(token, str) or not token or expires / 1000 < time.time():
        return None  # Claude Code refreshes it on its next request; try again on the next event
    request = urllib.request.Request(API + bid, headers={
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "ccr-byoc-2025-07-29",
    })
    try:
        with _open(request, timeout=TIMEOUT) as response:
            title = json.load(response).get("title")
    except (OSError, ValueError, AttributeError):
        return None
    return title if isinstance(title, str) else None


def title_from_transcript(path):
    """The latest `ai-title` in a terminal session's transcript.

    shortcut: reads the whole transcript on each run (async, so it never delays the session);
    keep a byte offset if long transcripts make it costly.
    """
    title = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"ai-title"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") == "ai-title" and isinstance(entry.get("aiTitle"), str):
                    title = entry["aiTitle"]
    except (OSError, TypeError):
        return None
    return title


def send(ingest_url, token, session_id, title):
    body = json.dumps({"hook_event_name": "TitleUpdate", "session_id": session_id, "title": title}).encode()
    request = urllib.request.Request(ingest_url.rstrip("/") + "/ingest", data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
    })
    try:
        _open(request, timeout=TIMEOUT).close()
    except OSError:
        pass


def main(argv):
    token = os.environ.get("CLAUDE_PLUGIN_OPTION_TOKEN", "")
    if len(argv) < 2 or not argv[1].startswith(("http://", "https://")) or not token:
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    session_id = event.get("session_id") if isinstance(event, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return

    bid = bridge_id(session_id)
    title = title_from_claude_ai(bid) if bid else title_from_transcript(event.get("transcript_path"))
    title = " ".join(title.split()) if title else ""
    if not title:
        return

    # Sent every time: the board ignores an unchanged title, and a session it forgot gets it back.
    send(argv[1], token, session_id, title)

if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception:  # never disturb the session
        pass
