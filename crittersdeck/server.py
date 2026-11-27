"""crittersdeck: the collector (hook events in) and the board (web UI + API out).

`create_apps` returns two ASGI apps that share one state:
  - the board: web UI, API and also /ingest (for machines on the same network);
  - the ingest app: only POST /ingest. This is the one to publish to the internet.
"""

import asyncio
import contextlib
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, state
from .approvals import ALLOW, DENY, Approvals, hook_reply
from . import __version__
from .snippet import EVENTS, INFO_EVENT, TITLE_EVENT, hooks_block, install_commands
from .store import Store

ANSWER = "answer"  # answers to AskUserQuestion, from the board
# Messages between sessions (the plugin's deck_* tools and its delivery hooks).
AGENT_EVENTS = ("DeckPeers", "DeckSend", "DeckInbox", "DeckWait", "DeckRooms", "DeckLeave")
ROOMS_MAX, ROOM_MEMBERS_MAX, ROOM_NAME_MAX = 50, 30, 40
WELCOME_NAMES = 10            # members named in the welcome notice
ROOM_HISTORY = 30             # messages an agent gets when it asks for a room's history
WAKE_COOLDOWN = 120           # an agent's room post does not wake a member this room woke this recently
ROOM_AGENT_WAKES = 6          # wakes from agents' posts in a room before it pauses until the board posts
WAIT_MAX = 50  # long-poll cap, seconds: under the ~100 s idle limit of proxies such as Cloudflare
MESSAGE_MAX, REF_MAX = 4000, 300
MESSAGES_PER_MINUTE = 10      # per source, on top of the ingest rate limit
DELIVERY_CHARS = 8000         # per delivery: the rest waits for the next one
MESSAGING_SEEN_FOR = 3 * 86400  # a source that asked for its inbox lately has messaging on
MAX_BODY = 256 * 1024
FULL_INPUT_MAX = 4000
NAME_MAX = 120
KEEPALIVE = 15
RATE_PER_SECOND = 20
MAX_SESSIONS_PER_SOURCE = 200
MAX_OPEN_REQUESTS_PER_SOURCE = 10
LOGIN_FAILURES_PER_MINUTE = 5
COOKIE = "cd_session"
WEB_DIR = Path(__file__).parent / "web"
CSP = "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Events after which a session has moved on: any open permission request was answered elsewhere.
_MOVED_ON = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd"}


@dataclass
class Settings:
    db_path: str = "crittersdeck.db"
    password_hash: str = ""
    trust_proxy_header: str = ""
    trusted_proxies: tuple = ()
    public_url: str = ""
    plugin_marketplace: str = ""
    idle_minutes: float = 30
    ended_hours: float = 24
    keepalive: float = KEEPALIVE

    @classmethod
    def from_env(cls):
        e = os.environ
        proxies = tuple(p.strip() for p in e.get("CRITTERSDECK_TRUSTED_PROXIES", "").split(",") if p.strip())
        return cls(
            db_path=e.get("CRITTERSDECK_DB", "crittersdeck.db"),
            password_hash=e.get("CRITTERSDECK_PASSWORD_HASH", ""),
            trust_proxy_header=e.get("CRITTERSDECK_TRUST_PROXY_HEADER", ""),
            trusted_proxies=proxies,
            public_url=e.get("CRITTERSDECK_PUBLIC_URL", ""),
            plugin_marketplace=e.get("CRITTERSDECK_PLUGIN_MARKETPLACE", ""),
            idle_minutes=float(e.get("CRITTERSDECK_IDLE_MINUTES", "30")),
        )


@dataclass
class _Bucket:
    rate: float = RATE_PER_SECOND
    capacity: float = RATE_PER_SECOND
    tokens: float = RATE_PER_SECOND
    last: float = field(default_factory=time.monotonic)

    def take(self):
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


# Characters that can make text read differently from what runs: controls, bidi, zero-width.
def _visible(ch):
    if ch in "\n\t":
        return ch
    if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp"):
        return f"\\u{{{ord(ch):04x}}}"
    return ch


def readable_input(tool_input):
    """What a person reads before allowing a tool.

    Returns (text, description, complete). Hidden characters are made visible, and
    `complete` is False when the text had to be cut, so the board must not offer Allow.
    """
    description = ""
    if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
        text = tool_input["command"]
        if isinstance(tool_input.get("description"), str):
            description = tool_input["description"]
    else:
        text = json.dumps(tool_input, ensure_ascii=False, indent=1) if tool_input is not None else ""
    text = "".join(map(_visible, text))
    description = "".join(map(_visible, description))[:300]
    if len(text) > FULL_INPUT_MAX:
        return text[:FULL_INPUT_MAX], description, False
    return text, description, True


QUESTIONS_MAX, OPTIONS_MAX, OTHER_MAX = 8, 20, 500


def question_form(tool_input):
    """AskUserQuestion input as the board shows it, or None when it is not the expected shape
    (the board then shows it as any other tool, raw)."""
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(questions, list) or not 0 < len(questions) <= QUESTIONS_MAX:
        return None
    shown = lambda v: "".join(map(_visible, v))
    texts = [q.get("question") if isinstance(q, dict) else None for q in questions]
    if len(set(map(str, texts))) != len(texts):
        return None  # answers are keyed by the question text: repeated texts could not be told apart
    form = []
    for q in questions:
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not q["question"].strip():
            return None
        options = q.get("options")
        if not isinstance(options, list) or not 0 < len(options) <= OPTIONS_MAX:
            return None
        if not all(isinstance(o, dict) and isinstance(o.get("label"), str) and o["label"].strip() for o in options):
            return None
        form.append({
            "question": shown(q["question"]),
            "header": shown(q["header"]) if isinstance(q.get("header"), str) else "",
            "multi": q.get("multiSelect") is True,
            "options": [{"label": shown(o["label"]),
                         "description": shown(o["description"]) if isinstance(o.get("description"), str) else ""}
                        for o in options],
        })
    return form


def answered_input(tool_input, answers):
    """The AskUserQuestion input with the board's answers, or None if they do not fit the questions.

    `answers` has one entry per question: {"options": [indexes], "other": text}. A single-choice
    question takes one option or a text of one's own; a multi-choice one, any options and/or a text."""
    questions = tool_input["questions"]
    if not isinstance(answers, list) or len(answers) != len(questions):
        return None
    out = {}
    for q, a in zip(questions, answers):
        if not isinstance(a, dict):
            return None
        picked, other = a.get("options", []), a.get("other", "")
        if not isinstance(picked, list) or not isinstance(other, str) or len(other) > OTHER_MAX:
            return None
        other = " ".join(other.split())
        n = len(q["options"])
        if not all(type(i) is int and 0 <= i < n for i in picked) or len(set(picked)) != len(picked):
            return None
        chosen = [q["options"][i]["label"] for i in sorted(picked)] + ([other] if other else [])
        if not chosen or (len(chosen) > 1 and q.get("multiSelect") is not True):
            return None
        out[q["question"]] = ", ".join(chosen)
    return {**tool_input, "answers": out}


# Session usage the board keeps: counts only, plus the model's name.
_INFO_COUNTS = ("context_used", "context_max", "tokens_in", "tokens_out", "cache_read", "cache_write", "turns")


def clean_info(info):
    """Usage figures from the plugin, checked; None if anything is off."""
    if not isinstance(info, dict) or not set(info) <= {*_INFO_COUNTS, "cost_usd", "model"}:
        return None
    out = {}
    for k in _INFO_COUNTS:
        v = info.get(k)
        if v is None:
            continue
        if type(v) is not int or not 0 <= v < 10**13:
            return None
        out[k] = v
    cost = info.get("cost_usd")
    if cost is not None:
        if type(cost) not in (int, float) or not 0 <= cost < 10**7:
            return None
        out["cost_usd"] = round(float(cost), 4)
    model = info.get("model")
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            return None
        out["model"] = "".join(map(_visible, model.strip()))[:80]
    return out


def is_private_url(url):
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _in_networks(peer, networks):
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    ip = getattr(ip, "ipv4_mapped", None) or ip  # ::ffff:10.0.0.1 is 10.0.0.1
    return any(ip in net for net in networks)


def create_app(settings=None):
    """The board app (UI, API and /ingest). Its `state.ingest_app` serves only /ingest."""
    return create_apps(settings)[0]


def create_apps(settings=None):
    settings = settings or Settings.from_env()
    if not settings.password_hash and not settings.trust_proxy_header:
        raise SystemExit("Set CRITTERSDECK_PASSWORD_HASH (python -m crittersdeck hash-password) "
                         "or CRITTERSDECK_TRUST_PROXY_HEADER behind an auth proxy.")
    for name, value in (("CRITTERSDECK_PUBLIC_URL", settings.public_url),
                        ("CRITTERSDECK_PLUGIN_MARKETPLACE", settings.plugin_marketplace)):
        if any(ch.isspace() for ch in value):
            raise SystemExit(f"{name} must not contain spaces: it goes into commands people paste.")
    if settings.trust_proxy_header and not settings.trusted_proxies:
        raise SystemExit("CRITTERSDECK_TRUST_PROXY_HEADER needs CRITTERSDECK_TRUSTED_PROXIES: "
                         "the address(es) of the proxy allowed to set it.")
    trusted_networks = [ipaddress.ip_network(p, strict=False) for p in settings.trusted_proxies]
    store = Store(settings.db_path)
    approvals = Approvals()
    logins = auth.Sessions()
    proxy_secret = secrets.token_bytes(32)
    buckets = {}
    login_bucket = _Bucket(rate=LOGIN_FAILURES_PER_MINUTE / 60, capacity=LOGIN_FAILURES_PER_MINUTE,
                           tokens=LOGIN_FAILURES_PER_MINUTE)
    subscribers = set()
    messaging = {}  # source id -> when it last asked for its inbox
    message_buckets = {}
    held = {}  # source id -> DeckWait requests being held
    woken = {}  # (source, session, room) -> when a room's message last woke that member
    agent_wakes = {}  # room id -> wakes caused by agents' posts since the board last posted there

    def broadcast():
        for q in list(subscribers):
            if q.empty():
                q.put_nowait(True)

    def snapshot():
        now = time.time()
        pending = {(p["source_id"], p["session_id"]): p for p in approvals.pending()}
        rows = store.sessions()
        unread = store.unread({(s["source_id"], s["session_id"]): s["started"] for s in rows if s["status"] != state.ENDED})
        sessions = []
        for s in rows:
            p = pending.get((s["source_id"], s["session_id"]))
            status = state.NEEDS_YOU if p else state.effective_status(s, now, settings.idle_minutes * 60)
            sessions.append({**s, "info": json.loads(s["info"] or "{}"), "status": status, "pending": p,
                             "unread": unread.get((s["source_id"], s["session_id"]), 0)})
        rooms = [{**r, "paused": agent_wakes.get(r["id"], 0) >= ROOM_AGENT_WAKES} for r in store.rooms()]
        return {"now": now, "sources": store.sources(), "sessions": sessions, "rooms": rooms}

    async def tick():
        while True:
            await asyncio.sleep(30)
            store.prune(time.time() - settings.ended_hours * 3600)
            for k in [k for k, t in woken.items() if time.time() - t > WAKE_COOLDOWN]:
                del woken[k]
            broadcast()  # idle transitions happen with time, not events

    def secure_headers(app):
        @app.middleware("http")
        async def headers(request, call_next):
            response = await call_next(request)
            response.headers.setdefault("Content-Security-Policy", CSP)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            return response

    # ---- ingest (hooks) ------------------------------------------------------

    async def ingest(request: Request):
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        source = store.source_by_token(token) if token else None
        if source is None:
            return JSONResponse({"error": "unknown source"}, status_code=401)
        if not buckets.setdefault(source["id"], _Bucket()).take():
            return JSONResponse({"error": "too many events"}, status_code=429)
        try:
            declared = int(request.headers.get("content-length") or 0)
        except ValueError:
            return JSONResponse({"error": "bad content-length"}, status_code=400)
        if declared > MAX_BODY:
            return JSONResponse({"error": "event too large"}, status_code=413)
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                return JSONResponse({"error": "event too large"}, status_code=413)
        try:
            event = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "not JSON"}, status_code=400)
        session_id = event.get("session_id") if isinstance(event, dict) else None
        if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
            return JSONResponse({"error": "missing or bad session_id"}, status_code=400)
        name = event.get("hook_event_name")
        sid = source["id"]
        if name in AGENT_EVENTS:
            return await agent_call(source, session_id, name, event)
        if name in (INFO_EVENT, TITLE_EVENT):
            return set_session_info(sid, session_id, event.get("title"), event.get("info") if name == INFO_EVENT else None)
        if name not in EVENTS:
            return JSONResponse({})  # an event this version does not use

        previous = store.session(sid, session_id)
        if previous is None and store.count_sessions(sid) >= MAX_SESSIONS_PER_SOURCE:
            evicted = store.evict_oldest(sid)
            if evicted:
                approvals.withdraw_session(sid, evicted)  # never leave a request the board cannot show
        if name in _MOVED_ON or name == "PermissionRequest":  # a new request replaces the session's old one
            approvals.withdraw_session(sid, session_id)
        s = state.apply_event(previous, event, time.time())
        s["folder"], s["tool"] = s["folder"][:NAME_MAX], s["tool"][:NAME_MAX]
        s["title"] = "".join(map(_visible, s.get("title", "")))[:NAME_MAX]
        try:
            store.save_session(sid, session_id, s)
        except sqlite3.IntegrityError:  # the source was deleted while this event was in flight
            return JSONResponse({"error": "unknown source"}, status_code=401)
        if name != "PermissionRequest":
            broadcast()
            return JSONResponse({})
        if approvals.count_source(sid) >= MAX_OPEN_REQUESTS_PER_SOURCE:
            broadcast()
            return JSONResponse({})  # no decision: the session's own dialog handles it

        text, description, complete = readable_input(event.get("tool_input"))
        questions = question_form(event.get("tool_input")) if s["tool"] == "AskUserQuestion" else None
        pending = approvals.open(sid, session_id, s["tool"], text, description, complete,
                                 questions, event.get("tool_input") if questions else None)
        broadcast()

        async def wait_for_answer():
            # Whitespace keeps proxies from closing an idle connection; JSON ignores it.
            try:
                while True:
                    try:
                        decision = await asyncio.wait_for(asyncio.shield(pending.future), settings.keepalive)
                        break
                    except asyncio.TimeoutError:
                        yield b" "
                yield json.dumps(hook_reply(decision)).encode()
            finally:
                approvals.close(pending)
                broadcast()

        return StreamingResponse(wait_for_answer(), media_type="application/json")

    def label(source_id, session_id):
        """Who a message is from, as a person reads it."""
        if not source_id:
            return "the board"
        src = next((x for x in store.sources() if x["id"] == source_id), None)
        s = store.session(source_id, session_id)
        title = (s or {}).get("title") or "untitled session"
        return f"session \"{title}\" on {src['name'] if src else 'a deleted machine'} ({session_id[:8]})"

    def message_view(m, names=None):
        return {"id": m["id"], "created": m["created"], "from": label(m["from_source"], m["from_session"]),
                "from_id": f"{m['from_source']}:{m['from_session']}" if m["from_source"] else "",
                "from_board": not m["from_source"], "to_kind": m["to_kind"], "text": m["text"], "ref": m["ref"],
                **({"room": m["room_id"], "room_name": (names if names is not None else store.room_names()).get(m["room_id"], "")}
                   if m.get("room_id") else {})}

    def taking_part(source_id):
        return time.time() - messaging.get(source_id, 0) < MESSAGING_SEEN_FOR

    def save_message(from_source, from_session, to, text, ref):
        """Checks and stores a message; returns (id, error)."""
        if not isinstance(text, str) or not text.strip() or len(text) > MESSAGE_MAX:
            return None, f"text: 1 to {MESSAGE_MAX} characters"
        if not isinstance(ref, str) or len(ref) > REF_MAX or any(c in ref for c in "\r\n"):
            return None, f"ref: one line, up to {REF_MAX} characters"
        room_id = ""
        if isinstance(to, str) and to.startswith("room:"):
            room = store.room(to[5:])
            if room is None or room["closed"] is not None:
                return None, "no such open room: use an id from deck_rooms"
            if from_source and not any(r["id"] == room["id"] for r in store.member_rooms(from_source, from_session)):
                return None, "this session is not a member of that room"
            kind, to_src, to_sid, room_id = "room", "", "", room["id"]
        elif to == "all":
            kind, to_src, to_sid = "all", "", ""
        elif isinstance(to, str) and ":" in to:
            to_src, _, to_sid = to.partition(":")
            if store.session(to_src, to_sid) is None or (from_source and not taking_part(to_src)):
                return None, "no such session: use an id from deck_peers"
            kind = "session"
        elif isinstance(to, str) and to.strip():
            src = next((x for x in store.sources() if x["name"].casefold() == to.strip().casefold()), None)
            if src is None or (from_source and not taking_part(src["id"])):
                return None, "no such machine: use a source name from deck_peers, a session id, or all"
            kind, to_src, to_sid = "source", src["id"], ""
        else:
            return None, "to: a session id, a machine name, or all"
        visible = lambda v: "".join(map(_visible, v))
        mid = store.add_message({"created": time.time(), "from_source": from_source, "from_session": from_session,
                                 "to_kind": kind, "to_source": to_src, "to_session": to_sid,
                                 "text": visible(text.strip()), "ref": visible(ref.strip()), "room_id": room_id})
        if room_id and not from_source:
            agent_wakes.pop(room_id, None)  # the person spoke: agents' posts may wake members again
        broadcast()
        return mid, None

    async def agent_call(source, session_id, name, event):
        """The plugin, for one session: who else is there, send a message, read its inbox.

        Only sources with messaging on take part: the server knows them because they ask for their
        inbox. Others are neither listed nor reachable."""
        sid = source["id"]
        messaging[sid] = time.time()
        me = store.session(sid, session_id)
        if me is None:
            return JSONResponse({"error": "this session is not on the board yet"}, status_code=409)
        if name == "DeckPeers":
            names = {x["id"]: x["name"] for x in store.sources()}
            now = time.time()
            peers = [{"id": f"{s['source_id']}:{s['session_id']}", "machine": names.get(s["source_id"], "?"),
                      "title": s["title"] or "untitled session", "folder": s["folder"],
                      "status": state.effective_status(s, now, settings.idle_minutes * 60),
                      "you": (s["source_id"], s["session_id"]) == (sid, session_id)}
                     for s in store.sessions() if s["status"] != state.ENDED and taking_part(s["source_id"])]
            machines = sorted(names[x] for x in names if taking_part(x))
            return JSONResponse({"peers": peers, "machines": machines})
        if name == "DeckSend":
            if not message_buckets.setdefault(sid, _Bucket(rate=MESSAGES_PER_MINUTE / 60, capacity=MESSAGES_PER_MINUTE,
                                                           tokens=MESSAGES_PER_MINUTE)).take():
                return JSONResponse({"error": f"at most {MESSAGES_PER_MINUTE} messages a minute"}, status_code=429)
            mid, error = save_message(sid, session_id, event.get("to"), event.get("text"), event.get("ref", ""))
            return JSONResponse({"error": error}, status_code=400) if error else JSONResponse({"sent": mid})
        if name == "DeckRooms":
            room_id = event.get("room")
            mine = store.member_rooms(sid, session_id)
            if room_id:  # one room's history; nothing is marked delivered
                if not any(r["id"] == room_id for r in mine):
                    return JSONResponse({"error": "this session is not a member of that room"}, status_code=404)
                names = store.room_names()
                return JSONResponse({"messages": [message_view(m, names) for m in store.room_messages(room_id, ROOM_HISTORY)]})
            members = {r["id"]: r["members"] for r in store.rooms()}
            return JSONResponse({"rooms": [{"id": r["id"], "name": r["name"], "members": [
                {"id": f"{m['source_id']}:{m['session_id']}", "label": label(m["source_id"], m["session_id"]),
                 "you": (m["source_id"], m["session_id"]) == (sid, session_id)} for m in members.get(r["id"], [])]}
                for r in mine]})
        if name == "DeckLeave":
            if not store.remove_member(str(event.get("room") or ""), sid, session_id):
                return JSONResponse({"error": "this session is not a member of that room"}, status_code=404)
            broadcast()
            return JSONResponse({"left": True})
        if name == "DeckWait":
            n = await waiting(sid, session_id, me["started"], event.get("wait"))
            return JSONResponse({"waiting": n or 0, "in_room": n is not None})
        rows = store.inbox(sid, session_id, me["started"], max_chars=DELIVERY_CHARS)
        if rows:
            broadcast()
        names = store.room_names()
        # Only a room member listens while idle (messages between sessions happen in rooms); anyone else gets
        # its messages on its next prompt, so an idle session never spends a model turn on an empty wait.
        wake = bool(store.member_rooms(sid, session_id))
        return JSONResponse({"messages": [message_view(m, names) for m in rows], "wake": wake})

    async def waiting(source_id, session_id, since, wait):
        """How many room messages wait for a session, without delivering them. With `wait` (seconds, capped),
        holds the request until there is at least one or the time is up (long-poll for `deck.py wait`).
        None when the session is in no open room: removed, or its room closed, it stops listening."""
        key = (source_id, session_id)
        ok = isinstance(wait, (int, float)) and not isinstance(wait, bool) and math.isfinite(wait)  # NaN would hold forever
        wait = min(max(wait, 0), WAIT_MAX) if ok else 0
        if held.get(source_id, 0) >= MAX_SESSIONS_PER_SOURCE:
            wait = 0  # one held request per session is all the plugin needs: answer the rest at once
        deadline = time.monotonic() + wait
        queue = asyncio.Queue()
        subscribers.add(queue)  # woken by every broadcast, which every new message makes
        held[source_id] = held.get(source_id, 0) + 1
        try:
            while True:
                if not store.member_rooms(source_id, session_id):
                    return None
                rows = [r for r in store.pending(source_id, session_id, since) if r[0]]  # only rooms wake
                now = time.time()
                # Agents in a room answering each other: a room wakes a member at most every WAKE_COOLDOWN s,
                # and after ROOM_AGENT_WAKES wakes from agents' posts it wakes nobody until the board posts.
                # The messages are still delivered on each member's next turn.
                waking = [(room, sender) for room, sender in rows if not (room and sender and (
                    agent_wakes.get(room, 0) >= ROOM_AGENT_WAKES or now - woken.get((*key, room), 0) < WAKE_COOLDOWN))]
                left = deadline - time.monotonic()
                if waking:
                    rooms_waking = {room for room, _ in waking if room}
                    for room in rooms_waking:
                        woken[(*key, room)] = now
                    by_agents = rooms_waking - {room for room, sender in waking if room and not sender}
                    for room in by_agents:
                        agent_wakes[room] = agent_wakes.get(room, 0) + 1
                    if any(agent_wakes[room] >= ROOM_AGENT_WAKES for room in by_agents):
                        broadcast()  # the room view shows it is paused
                    return len(rows)
                if left <= 0:
                    return 0
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(queue.get(), left)
        finally:
            subscribers.discard(queue)
            held[source_id] -= 1

    def set_session_info(source_id, session_id, title, info):
        """What the crittersdeck plugin reads on the machine (send_usage option): the title Claude gave the session
        and its usage (tokens, context, model, cost). The title replaces the first-prompt one but not a
        name given on the board. Neither counts as activity. `applied`: the board knows the session."""
        if title is not None and (not isinstance(title, str) or not title.strip()):
            return JSONResponse({"error": "bad title"}, status_code=400)
        usage = clean_info(info) if info is not None else None
        if (info is not None and usage is None) or (title is None and not usage):
            return JSONResponse({"error": "bad session info"}, status_code=400)
        s = store.session(source_id, session_id)
        if s is None:  # only sessions the hooks already reported
            return JSONResponse({"applied": False})
        new = dict(s)
        if title is not None and not s["renamed"]:
            new["title"] = "".join(map(_visible, " ".join(title.split())))[:NAME_MAX]
        if usage:  # merged: figures this event lacks (claude.ai unreachable) keep their last value
            new["info"] = json.dumps({**json.loads(s["info"] or "{}"), **usage}, sort_keys=True)
        if new != s:
            try:
                store.save_session(source_id, session_id, new)
            except sqlite3.IntegrityError:  # the source was deleted while this event was in flight
                return JSONResponse({"error": "unknown source"}, status_code=401)
            broadcast()
        return JSONResponse({"applied": True})

    # ---- board sign-in -------------------------------------------------------

    def current_user(request, touch=True):
        """(user, csrf) for a signed-in browser, else None. `touch` counts the request as activity."""
        if settings.trust_proxy_header:
            peer = request.client.host if request.client else ""
            user = request.headers.get(settings.trust_proxy_header)
            if user and _in_networks(peer, trusted_networks):
                return user, auth.proxy_csrf(proxy_secret, user)
        csrf = logins.csrf(request.cookies.get(COOKIE), touch=touch)
        return ("admin", csrf) if csrf else None

    def require_user(request, mutating=False):
        who = current_user(request)
        if who is None:
            raise HTTPException(401, "sign in")
        if mutating:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                raise HTTPException(403, "bad origin")
            given = request.headers.get("x-csrf-token", "").encode("utf-8", "surrogateescape")
            if not secrets.compare_digest(given, who[1].encode()):
                raise HTTPException(403, "bad CSRF token")
        return who

    async def json_body(request):
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    board = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan(tick))
    board.state.store, board.state.approvals = store, approvals
    secure_headers(board)
    # "//ingest" too: a base URL typed with a trailing slash must still work.
    board.add_api_route("/ingest", ingest, methods=["POST"])
    board.add_api_route("//ingest", ingest, methods=["POST"], include_in_schema=False)

    @board.post("/api/login")
    async def login(request: Request):
        if not settings.password_hash:
            raise HTTPException(404, "password sign-in is off")
        # Reserve an attempt before checking, so parallel guesses count too; a success gives it back.
        if not login_bucket.take():
            raise HTTPException(429, "too many failed sign-ins, wait a minute")
        password = (await json_body(request)).get("password", "")
        ok = isinstance(password, str) and await asyncio.to_thread(auth.check_password, password, settings.password_hash)
        if not ok:
            await asyncio.sleep(1)
            raise HTTPException(401, "wrong password")
        login_bucket.tokens = min(login_bucket.capacity, login_bucket.tokens + 1)
        sid = logins.create()
        response = JSONResponse({"user": "admin", "csrf": logins.csrf(sid), "password_login": True})
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, sid, httponly=True, samesite="strict", secure=secure,
                            max_age=int(auth.ABSOLUTE_TTL))
        return response

    @board.post("/api/logout")
    async def logout(request: Request):
        require_user(request, mutating=True)
        logins.drop(request.cookies.get(COOKIE))
        response = JSONResponse({})
        response.delete_cookie(COOKIE)
        return response

    @board.get("/api/me")
    async def me(request: Request):
        user, csrf = require_user(request)
        return {"user": user, "csrf": csrf, "password_login": bool(settings.password_hash),
                "version": __version__, "public_url": settings.public_url,
                "plugin_marketplace": settings.plugin_marketplace, "idle_minutes": settings.idle_minutes}

    # ---- board data ------------------------------------------------------------

    @board.get("/api/state")
    async def get_state(request: Request):
        require_user(request)
        return snapshot()

    @board.get("/api/events")
    async def events(request: Request):
        require_user(request)
        queue = asyncio.Queue(maxsize=1)
        subscribers.add(queue)

        async def stream():
            try:
                yield f"data: {json.dumps(snapshot())}\n\n"
                while current_user(request, touch=False) is not None:  # stop after sign-out or expiry
                    try:
                        await asyncio.wait_for(queue.get(), KEEPALIVE)
                        yield f"data: {json.dumps(snapshot())}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                subscribers.discard(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @board.post("/api/approvals/{request_id}")
    async def decide(request_id: str, request: Request):
        require_user(request, mutating=True)
        body = await json_body(request)
        decision = body.get("decision")
        if decision not in (ALLOW, DENY, ANSWER):
            raise HTTPException(400, "decision must be allow, deny or answer")
        pending = approvals.get(request_id)
        if pending is None:
            raise HTTPException(409, "this request was already answered")
        if decision == ANSWER:
            if not pending.questions:
                raise HTTPException(400, "this request has no questions")
            updated = answered_input(pending.tool_input, body.get("answers"))
            if updated is None:
                raise HTTPException(400, "answer every question: pick options or write your own")
            decision = {"behavior": ALLOW, "updatedInput": updated}
        elif decision == ALLOW and pending.questions:
            raise HTTPException(400, "a question is answered, not allowed: send the answers")
        elif decision == ALLOW and not pending.complete:
            raise HTTPException(409, "too long to show in full: answer it in the session")
        elif decision == DENY and pending.questions:
            decision = {"behavior": DENY, "message": "Skipped from crittersdeck: no answer was given."}
        approvals.decide(request_id, decision)
        s = store.session(pending.source_id, pending.session_id)
        if s is not None:  # the agent carries on (or is told no): it is working again
            store.save_session(pending.source_id, pending.session_id, {**s, "status": state.WORKING, "updated": time.time()})
        broadcast()
        return {}

    @board.post("/api/sessions/{source_id}/{session_id}/seen")
    async def seen(source_id: str, session_id: str, request: Request):
        require_user(request, mutating=True)
        s = store.session(source_id, session_id)
        if s is None:
            raise HTTPException(404, "no such session")
        store.save_session(source_id, session_id, state.mark_seen(s))
        broadcast()
        return {}

    @board.patch("/api/sessions/{source_id}/{session_id}")
    async def rename(source_id: str, session_id: str, request: Request):
        require_user(request, mutating=True)
        title = (await json_body(request)).get("title")
        if not isinstance(title, str) or not 0 < len(title.strip()) <= 80:
            raise HTTPException(400, "title: 1 to 80 characters")
        s = store.session(source_id, session_id)
        if s is None:
            raise HTTPException(404, "no such session")
        store.save_session(source_id, session_id, {**s, "title": "".join(map(_visible, " ".join(title.split()))), "renamed": 1})
        broadcast()
        return {}

    @board.get("/api/sessions/{source_id}/{session_id}/messages")
    async def thread(source_id: str, session_id: str, request: Request):
        require_user(request)
        if store.session(source_id, session_id) is None:
            raise HTTPException(404, "no such session")
        return [{**message_view(m), "delivered": bool(m["delivered"]),
                 "outgoing": (m["from_source"], m["from_session"]) == (source_id, session_id)}
                for m in store.thread(source_id, session_id)]

    @board.post("/api/sessions/{source_id}/{session_id}/messages")
    async def write(source_id: str, session_id: str, request: Request):
        require_user(request, mutating=True)
        if store.session(source_id, session_id) is None:
            raise HTTPException(404, "no such session")
        body = await json_body(request)
        mid, error = save_message("", "", f"{source_id}:{session_id}", body.get("text"), body.get("ref", ""))
        if error:
            raise HTTPException(400, error)
        return {"sent": mid}

    @board.delete("/api/sessions/{source_id}/{session_id}")
    async def forget(source_id: str, session_id: str, request: Request):
        require_user(request, mutating=True)
        approvals.withdraw_session(source_id, session_id)
        if not store.forget_session(source_id, session_id):
            raise HTTPException(404, "no such session")
        broadcast()
        return {}

    @board.get("/api/sources")
    async def list_sources(request: Request):
        require_user(request)
        return store.sources()

    @board.post("/api/sources")
    async def add_source(request: Request):
        require_user(request, mutating=True)
        data = await json_body(request)
        name, color = data.get("name"), data.get("color")
        name = name.strip() if isinstance(name, str) else ""
        color = color if isinstance(color, str) else ""
        if not 0 < len(name) <= 40:
            raise HTTPException(400, "name: 1 to 40 characters")
        if not _COLOR.match(color):
            raise HTTPException(400, "color: #rrggbb")
        if any(s["name"].casefold() == name.casefold() for s in store.sources()):
            raise HTTPException(409, "a source with that name already exists")
        source, token = store.add_source(name, color)
        base = settings.public_url or str(request.base_url)
        broadcast()
        return {
            "source": source,
            "token": token,
            "settings": hooks_block(base, token),
            "install": install_commands(settings.plugin_marketplace, base, token) if settings.plugin_marketplace else [],
            # Hook events carry commands and file contents: say so when they would cross networks in clear.
            "insecure": urlsplit(base).scheme != "https" and not is_private_url(base),
            # Without a public URL the hooks point at this board's own address, which other machines may not reach.
            "no_public_url": not settings.public_url,
        }

    @board.delete("/api/sources/{source_id}")
    async def delete_source(source_id: str, request: Request):
        require_user(request, mutating=True)
        approvals.withdraw_source(source_id)
        if not store.delete_source(source_id):
            raise HTTPException(404, "no such source")
        broadcast()
        return {}

    # ---- rooms: managed on the board only -------------------------------------------------

    def room_name(value, except_id=None):
        name = " ".join(value.split()) if isinstance(value, str) else ""
        name = "".join(map(_visible, name))
        if not 0 < len(name) <= ROOM_NAME_MAX:
            raise HTTPException(400, f"name: 1 to {ROOM_NAME_MAX} characters")
        if any(r["name"].casefold() == name.casefold() and r["closed"] is None and r["id"] != except_id
               for r in store.rooms()):
            raise HTTPException(409, "an open room with that name already exists")
        return name

    def open_room(room_id):
        room = store.room(room_id)
        if room is None:
            raise HTTPException(404, "no such room")
        if room["closed"] is not None:
            raise HTTPException(409, "the room is closed")
        return room

    def join(room, source_id, session_id):
        """Adds a session; it gets a welcome notice (no history: it asks for it with deck_rooms)."""
        s = store.session(source_id, session_id)
        if s is None:
            raise HTTPException(404, "no such session")
        if s["status"] == state.ENDED:
            raise HTTPException(409, "the session has ended")
        if not taking_part(source_id):
            raise HTTPException(409, "messages between agents are off on that session's machine (plugin setting)")
        members = next((r["members"] for r in store.rooms() if r["id"] == room["id"]), [])
        others = [(m["source_id"], m["session_id"]) for m in members]
        if (source_id, session_id) in others:
            return False
        if len(others) >= ROOM_MEMBERS_MAX:
            raise HTTPException(409, f"a room has at most {ROOM_MEMBERS_MAX} members")
        store.add_member(room["id"], source_id, session_id)
        # Members are listed up to WELCOME_NAMES, so the notice stays under MESSAGE_MAX with long titles.
        names = "; ".join(label(*k)[:200] for k in others[:WELCOME_NAMES]) or "only you so far"
        more = f" and {len(others) - WELCOME_NAMES} more (see deck_rooms)" if len(others) > WELCOME_NAMES else ""
        _, error = save_message("", "", f"{source_id}:{session_id}",
                                f"You were added to the room \"{room['name']}\" (room:{room['id']}). Members: {names}{more}."
                                f" Messages posted there from now on reach you. Read its history with deck_rooms, write with"
                                f" deck_send to room:{room['id']}, leave with deck_leave.", "")
        if error:  # never a silent member: undo, and say why
            store.remove_member(room["id"], source_id, session_id)
            raise HTTPException(500, f"could not send the welcome notice: {error}")
        return True

    @board.get("/api/rooms")
    async def list_rooms(request: Request):
        require_user(request)
        return store.rooms()

    @board.post("/api/rooms")
    async def create_room(request: Request):
        require_user(request, mutating=True)
        data = await json_body(request)
        name = room_name(data.get("name"))
        if sum(1 for r in store.rooms() if r["closed"] is None) >= ROOMS_MAX:
            raise HTTPException(409, f"at most {ROOMS_MAX} open rooms")
        members = data.get("members") or []
        if not isinstance(members, list) or not all(isinstance(m, str) and ":" in m for m in members):
            raise HTTPException(400, "members: a list of session ids (source:session)")
        for m in members:  # all checked before the room exists or anyone is welcomed: no half-made room
            s = store.session(*m.split(":", 1))
            if s is None or s["status"] == state.ENDED:
                raise HTTPException(404, f"no live session {m}")
            if not taking_part(s["source_id"]):
                raise HTTPException(409, f"messages between agents are off on the machine of {m} (plugin setting)")
        room = store.add_room(name)
        try:
            for m in members[:ROOM_MEMBERS_MAX]:
                join(room, *m.split(":", 1))
        except HTTPException:
            store.drop_room(room["id"])  # all or nothing: never a half-made room
            raise
        broadcast()
        return room

    @board.patch("/api/rooms/{room_id}")
    async def rename_room(room_id: str, request: Request):
        require_user(request, mutating=True)
        open_room(room_id)
        store.rename_room(room_id, room_name((await json_body(request)).get("name"), except_id=room_id))
        broadcast()
        return {}

    @board.delete("/api/rooms/{room_id}")
    async def close_room(room_id: str, request: Request):
        require_user(request, mutating=True)
        open_room(room_id)
        store.close_room(room_id)  # archived: read-only, kept a week
        agent_wakes.pop(room_id, None)
        broadcast()
        return {}

    @board.put("/api/rooms/{room_id}/members/{source_id}/{session_id}")
    async def add_member(room_id: str, source_id: str, session_id: str, request: Request):
        require_user(request, mutating=True)
        added = join(open_room(room_id), source_id, session_id)
        broadcast()
        return {"added": added}

    @board.delete("/api/rooms/{room_id}/members/{source_id}/{session_id}")
    async def remove_member(room_id: str, source_id: str, session_id: str, request: Request):
        require_user(request, mutating=True)
        open_room(room_id)
        if not store.remove_member(room_id, source_id, session_id):
            raise HTTPException(404, "not a member")
        broadcast()
        return {}

    @board.get("/api/rooms/{room_id}/messages")
    async def room_history(room_id: str, request: Request, before: int | None = None):
        require_user(request)
        if store.room(room_id) is None:
            raise HTTPException(404, "no such room")
        names = store.room_names()
        return [{**message_view(m, names), "delivered": m["delivered"], "recipients": m["recipients"]}
                for m in store.room_messages(room_id, 50, before)]

    @board.post("/api/rooms/{room_id}/messages")
    async def post_room(room_id: str, request: Request):
        require_user(request, mutating=True)
        open_room(room_id)
        body = await json_body(request)
        mid, error = save_message("", "", f"room:{room_id}", body.get("text"), body.get("ref", ""))
        if error:
            raise HTTPException(400, error)
        return {"sent": mid}

    @board.get("/healthz")
    async def healthz():
        return Response("ok")

    board.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")

    ingest_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    secure_headers(ingest_app)
    ingest_app.add_api_route("/ingest", ingest, methods=["POST"])
    ingest_app.add_api_route("//ingest", ingest, methods=["POST"], include_in_schema=False)
    ingest_app.add_api_route("/healthz", healthz, methods=["GET"])
    board.state.ingest_app = ingest_app
    return board, ingest_app


def _lifespan(tick):
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        ticker = asyncio.create_task(tick())
        yield
        ticker.cancel()

    return lifespan
