"""crittersdeck: the collector (hook events in) and the board (web UI + API out).

`create_apps` returns two ASGI apps that share one state:
  - the board: web UI, API and also /ingest (for machines on the same network);
  - the ingest app: only POST /ingest. This is the one to publish to the internet.
"""

import asyncio
import contextlib
import ipaddress
import json
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
from .snippet import EVENTS, hooks_block, install_commands
from .store import Store

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

    def broadcast():
        for q in list(subscribers):
            if q.empty():
                q.put_nowait(True)

    def snapshot():
        now = time.time()
        pending = {(p["source_id"], p["session_id"]): p for p in approvals.pending()}
        sessions = []
        for s in store.sessions():
            p = pending.get((s["source_id"], s["session_id"]))
            status = state.NEEDS_YOU if p else state.effective_status(s, now, settings.idle_minutes * 60)
            sessions.append({**s, "status": status, "pending": p})
        return {"now": now, "sources": store.sources(), "sessions": sessions}

    async def tick():
        while True:
            await asyncio.sleep(30)
            store.prune(time.time() - settings.ended_hours * 3600)
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
        if name not in EVENTS:
            return JSONResponse({})  # an event this version does not use

        sid = source["id"]
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
        pending = approvals.open(sid, session_id, s["tool"], text, description, complete)
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
        return {"user": user, "csrf": csrf, "password_login": bool(settings.password_hash)}

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
        decision = (await json_body(request)).get("decision")
        if decision not in (ALLOW, DENY):
            raise HTTPException(400, "decision must be allow or deny")
        pending = approvals.get(request_id)
        if pending is None:
            raise HTTPException(409, "this request was already answered")
        if decision == ALLOW and not pending.complete:
            raise HTTPException(409, "too long to show in full: answer it in the session")
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
        store.save_session(source_id, session_id, {**s, "title": "".join(map(_visible, " ".join(title.split())))})
        broadcast()
        return {}

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
