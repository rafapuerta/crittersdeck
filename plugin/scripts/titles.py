"""Send the title Claude gave this session, and its usage, to the crittersdeck board.

Runs as a Claude Code hook of the crittersdeck plugin, unless its send_usage option is off. Reads the hook event
on stdin and prints nothing, so it can never add text to the session.

Where it comes from:
- The session's transcript (on this machine): the model, tokens in and out, cache, context used,
  the number of prompts, the name given with /rename or by the desktop app (custom-title), and for
  terminal sessions the `ai-title` Claude Code writes there.
- Sessions linked to claude.ai (desktop app, web, Remote Control): Claude Code's session
  registry (<config>/sessions/<pid>.json) maps this session to its claude.ai id, and the
  claude.ai sessions API gives its title, context limit and cost. That API is not documented:
  if it changes, nothing from it is sent and the board keeps what it has.

Only counts and the model's name leave the machine, never prompts or replies. The Claude
credential is read from <config>/.credentials.json, used only for that request to
api.anthropic.com, and never stored or sent anywhere else.
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
LIMITS = (10**13, 10**7)  # what the board accepts: counts, cost (anything else would void the whole event)
_BRIDGE_ID = re.compile(r"session_[A-Za-z0-9]{1,64}")


def _version():
    """The plugin's version, from its plugin.json, for the User-Agent."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".claude-plugin", "plugin.json"),
                  encoding="utf-8") as f:
            version = json.load(f).get("version")
        return version if isinstance(version, str) and version else "unknown"
    except (OSError, ValueError, AttributeError):
        return "unknown"


VERSION = _version()
# Cloudflare in front of a board rejects urllib's default User-Agent (error 1010): name the plugin instead.
USER_AGENT = f"crittersdeck-plugin/{VERSION}"


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


def from_claude_ai(bid):
    """(title, figures) claude.ai has for the session, or (None, {})."""
    creds = read_json(os.path.join(config_dir(), ".credentials.json")) or {}
    oauth = creds.get("claudeAiOauth") or {}
    token, expires = oauth.get("accessToken"), oauth.get("expiresAt") or 0
    if not isinstance(token, str) or not token or expires / 1000 < time.time():
        return None, {}  # Claude Code refreshes it on its next request; try again on the next event
    request = urllib.request.Request(API + bid, headers={
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "ccr-byoc-2025-07-29",
        "User-Agent": USER_AGENT,
    })
    try:
        with _open(request, timeout=TIMEOUT) as response:
            session = json.load(response)
        meta = session.get("external_metadata") or {}
        context = meta.get("context_usage") or {}
        figures = {
            "context_used": context.get("used_tokens"),
            "context_max": context.get("max_tokens"),
            "cost_usd": (meta.get("usage") or {}).get("cost_usd"),
            "model": meta.get("last_served_model") or meta.get("model"),
        }
        title = session.get("title")
    except (OSError, ValueError, AttributeError):
        return None, {}
    clean = {}
    for k, v in figures.items():
        if k == "model" and isinstance(v, str) and v.strip():
            clean[k] = v.strip()
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < LIMITS[k == "cost_usd"]:
            clean[k] = float(v) if k == "cost_usd" else int(v)  # the board takes whole counts only
    return (title if isinstance(title, str) else None), clean


def from_transcript(path):
    """(ai-title, custom title, figures) from a session's transcript: usage of each reply counted once.

    shortcut: reads the whole transcript on each run (async, so it never delays the session);
    keep a byte offset if long transcripts make it costly.
    """
    title, custom, seen, last = None, None, set(), None
    totals = {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cache_write": 0, "turns": 0}
    model = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("isSidechain"):
                    continue
                kind = entry.get("type")
                if kind == "ai-title" and isinstance(entry.get("aiTitle"), str):
                    title = entry["aiTitle"]
                elif kind == "custom-title" and isinstance(entry.get("customTitle"), str):
                    custom = entry["customTitle"]  # /rename, and the desktop app's titles
                elif kind == "user" and not entry.get("isMeta") and is_prompt(entry.get("message")):
                    totals["turns"] += 1
                elif kind == "assistant":
                    message = entry.get("message") or {}
                    usage = message.get("usage")
                    if not isinstance(usage, dict) or message.get("model") == "<synthetic>":
                        continue
                    if message.get("id") in seen:  # one reply can span several entries
                        continue
                    seen.add(message.get("id"))
                    num = lambda k: usage.get(k) if type(usage.get(k)) is int else 0
                    totals["tokens_in"] += num("input_tokens")
                    totals["tokens_out"] += num("output_tokens")
                    totals["cache_read"] += num("cache_read_input_tokens")
                    totals["cache_write"] += num("cache_creation_input_tokens")
                    last = num("input_tokens") + num("cache_read_input_tokens") + num("cache_creation_input_tokens")
                    model = message.get("model") if isinstance(message.get("model"), str) else model
    except (OSError, TypeError):
        return None, None, {}
    figures = dict(totals)
    if last is not None:
        figures["context_used"] = last  # what the latest request sent: the context in use
    if model:
        figures["model"] = model
    return title, custom, figures


def custom_title(transcript_path, fallback):
    """The name given to the session: <transcript>/custom-title.json (desktop app), else the transcript's."""
    if isinstance(transcript_path, str) and transcript_path.endswith(".jsonl"):
        saved = (read_json(os.path.join(transcript_path[:-6], "custom-title.json")) or {})
        name = saved.get("customTitle") if isinstance(saved, dict) else None
        if isinstance(name, str) and name.strip():
            return name
    return fallback


def is_prompt(message):
    """A prompt typed by the person (not a tool result)."""
    content = (message or {}).get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    return isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "text" for c in content)


def send(ingest_url, token, session_id, title, figures):
    event = {"hook_event_name": "SessionInfo", "session_id": session_id}
    if figures:  # nothing read: leave what the board has
        event["info"] = figures
    if title:
        event["title"] = title
    body = json.dumps(event).encode()
    request = urllib.request.Request(ingest_url.rstrip("/") + "/ingest", data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
        "User-Agent": USER_AGENT,
    })
    try:
        _open(request, timeout=TIMEOUT).close()
    except OSError:
        pass


def main(argv):
    if os.environ.get("CLAUDE_PLUGIN_OPTION_SEND_USAGE", "").strip().lower() in ("false", "0", "no", "off"):
        return  # turned off: the plugin sends session events only
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

    title, custom, figures = from_transcript(event.get("transcript_path"))
    custom = custom_title(event.get("transcript_path"), custom)
    bid = bridge_id(session_id)
    if bid:  # linked to claude.ai: its title, context limit and cost
        linked_title, extra = from_claude_ai(bid)
        title = linked_title or title  # claude.ai unreachable: keep what the transcript had
        figures.update(extra)
    title = custom or title  # a name the person gave wins over generated ones
    title = " ".join(title.split()) if title else ""
    if not title and not figures:
        return

    # Sent every time: the board ignores what did not change, and a session it forgot gets it back.
    send(argv[1], token, session_id, title, figures)


if __name__ == "__main__":
    # Windows reads pipes in the local code page (cp1252), which turns Claude Code's UTF-8 into mojibake.
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    try:
        main(sys.argv)
    except Exception:  # never disturb the session
        pass
