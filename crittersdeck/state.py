"""Session state machine: pure functions from hook events to a session's status."""

import os

WORKING = "working"
NEEDS_YOU = "needs_you"
DONE_UNSEEN = "done_unseen"
IDLE = "idle"
ENDED = "ended"

DETAIL_MAX = 200
TITLE_MAX = 60

# Notification types that mean "a person must act in the session".
_ASKING_NOTIFICATIONS = {"permission_prompt", "elicitation_dialog"}


def summarize(tool_input):
    """A short, single-line description of what a tool is about to do."""
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "path", "url", "pattern", "query", "description", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            text = " ".join(value.split())
            return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 1] + "…"
    return ""


def title_from(prompt):
    """A session title: the first line of its first prompt, shortened."""
    if not isinstance(prompt, str):
        return ""
    line = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "")
    line = " ".join(line.split())
    return line if len(line) <= TITLE_MAX else line[: TITLE_MAX - 1] + "…"


def folder_name(cwd):
    if not isinstance(cwd, str) or not cwd:
        return ""
    return os.path.basename(cwd.replace("\\", "/").rstrip("/")) or cwd


def apply_event(session, event, now):
    """Return the session after `event`. `session` is a dict (or None for a new one).

    Only the fields the board needs are kept: never the prompt or the full tool input.
    """
    s = dict(session or {"status": IDLE, "tool": "", "detail": "", "folder": "", "started": now})
    name = event.get("hook_event_name")
    cwd = folder_name(event.get("cwd"))
    if cwd:
        s["folder"] = cwd
    s["updated"] = now

    if name in ("UserPromptSubmit", "PostToolUse"):
        s["status"] = WORKING
        if name == "UserPromptSubmit":
            s["tool"], s["detail"] = "", ""
            if not s.get("title"):  # only the first prompt names the session; later ones are not kept
                s["title"] = title_from(event.get("prompt"))
    elif name == "PreToolUse":
        s["status"] = WORKING
        s["tool"] = str(event.get("tool_name") or "")
        s["detail"] = summarize(event.get("tool_input"))
    elif name == "PermissionRequest":
        s["status"] = NEEDS_YOU
        s["tool"] = str(event.get("tool_name") or "")
        s["detail"] = summarize(event.get("tool_input"))
    elif name == "Notification":
        if event.get("notification_type") in _ASKING_NOTIFICATIONS:
            s["status"] = NEEDS_YOU
            if not s.get("detail"):
                s["detail"] = summarize({"description": event.get("message")})
    elif name == "Stop":
        s["status"] = DONE_UNSEEN
        s["tool"] = ""
    elif name == "SessionEnd":
        s["status"] = ENDED
        s["tool"] = ""
    elif name == "SessionStart":
        s.setdefault("status", IDLE)
    return s


def effective_status(session, now, idle_after):
    """Working or asking sessions that went quiet for `idle_after` seconds read as idle."""
    status = session["status"]
    if status in (WORKING, NEEDS_YOU) and now - session["updated"] > idle_after:
        return IDLE
    return status


def mark_seen(session):
    s = dict(session)
    if s["status"] == DONE_UNSEEN:
        s["status"] = IDLE
    return s
