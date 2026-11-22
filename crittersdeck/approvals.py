"""Permission requests waiting for an answer from the board.

A PermissionRequest hook keeps its HTTP request open while Claude Code shows its own
dialog in the session. Whichever side answers first wins: an answer from the board is
returned to the hook; an answer in the session shows up as a later event for that
session (or a closed connection), which withdraws the request from the board.
"""

import asyncio
import secrets
import time

ALLOW = "allow"
DENY = "deny"


class Pending:
    def __init__(self, source_id, session_id, tool, text, description, complete):
        self.id = secrets.token_urlsafe(12)
        self.source_id = source_id
        self.session_id = session_id
        self.tool = tool
        self.text = text
        self.description = description
        self.complete = complete
        self.created = time.time()
        self.future = asyncio.get_running_loop().create_future()

    def public(self):
        return {
            "id": self.id,
            "source_id": self.source_id,
            "session_id": self.session_id,
            "tool": self.tool,
            "input": self.text,
            "description": self.description,
            "complete": self.complete,
            "created": self.created,
        }


class Approvals:
    def __init__(self):
        self._by_id = {}

    def open(self, source_id, session_id, tool, text, description, complete):
        # A new request supersedes any older one still open for the same session.
        self.withdraw_session(source_id, session_id)
        p = Pending(source_id, session_id, tool, text, description, complete)
        self._by_id[p.id] = p
        return p

    def decide(self, request_id, decision):
        """Answer from the board. False when the request is gone (answered, withdrawn)."""
        p = self._by_id.pop(request_id, None)
        if p is None or p.future.done():
            return False
        p.future.set_result(decision)
        return True

    def withdraw_session(self, source_id, session_id):
        """The session moved on (answered in the app, interrupted...): drop its requests."""
        gone = [p for p in self._by_id.values() if (p.source_id, p.session_id) == (source_id, session_id)]
        for p in gone:
            self.close(p)
        return bool(gone)

    def withdraw_source(self, source_id):
        for p in [p for p in self._by_id.values() if p.source_id == source_id]:
            self.close(p)

    def close(self, p):
        self._by_id.pop(p.id, None)
        if not p.future.done():
            p.future.set_result(None)

    def get(self, request_id):
        p = self._by_id.get(request_id)
        return p if p is not None and not p.future.done() else None

    def count_source(self, source_id):
        return sum(1 for p in self._by_id.values() if p.source_id == source_id)

    def pending(self):
        return [p.public() for p in self._by_id.values()]


def hook_reply(decision):
    """The PermissionRequest hook output for a decision (None: let the session decide)."""
    if decision == ALLOW:
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    if decision == DENY:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": "Denied from crittersdeck."},
            }
        }
    return {}
