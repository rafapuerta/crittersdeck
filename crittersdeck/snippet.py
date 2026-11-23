"""The `hooks` block a source pastes into its Claude Code settings.json."""

EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Notification",
    "Stop",
    "SessionEnd",
)

# Sent by the crittersdeck-titles plugin, not by a Claude Code hook.
TITLE_EVENT = "TitleUpdate"

# A permission request stays open until someone answers, in the session or on the board.
PERMISSION_TIMEOUT = 86400
EVENT_TIMEOUT = 5


def install_commands(marketplace, base_url, token):
    """Commands that install the crittersdeck plugin for a source (see plugin/ in the repo)."""
    return [
        f"claude plugin marketplace add {marketplace}",
        f"claude plugin install crittersdeck@crittersdeck --config ingest_url={base_url.rstrip('/')} --config token={token}",
    ]


def hooks_block(base_url, token):
    url = base_url.rstrip("/") + "/ingest"
    hooks = {}
    for event in EVENTS:
        hook = {
            "type": "http",
            "url": url,
            "headers": {"Authorization": f"Bearer {token}"},
            "timeout": PERMISSION_TIMEOUT if event == "PermissionRequest" else EVENT_TIMEOUT,
        }
        hooks[event] = [{"hooks": [hook]}]
    return {"hooks": hooks}
