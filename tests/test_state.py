import pytest

from crittersdeck import state

T0 = 1000.0


def ev(name, **kw):
    return {"hook_event_name": name, "session_id": "s1", "cwd": "/home/me/proj", **kw}


@pytest.mark.parametrize(
    "events, status",
    [
        (["SessionStart"], state.IDLE),
        (["UserPromptSubmit"], state.WORKING),
        (["UserPromptSubmit", "PreToolUse"], state.WORKING),
        (["UserPromptSubmit", "PreToolUse", "PermissionRequest"], state.NEEDS_YOU),
        (["UserPromptSubmit", "PermissionRequest", "PostToolUse"], state.WORKING),
        (["UserPromptSubmit", "Stop"], state.DONE_UNSEEN),
        (["UserPromptSubmit", "Stop", "SessionEnd"], state.ENDED),
        (["Stop", "UserPromptSubmit"], state.WORKING),
    ],
)
def test_transitions(events, status):
    s = None
    for name in events:
        s = state.apply_event(s, ev(name, tool_name="Bash", tool_input={"command": "ls"}), T0)
    assert s["status"] == status


def test_notification_only_asks_for_prompts():
    s = state.apply_event(None, ev("UserPromptSubmit"), T0)
    s = state.apply_event(s, ev("Notification", notification_type="idle_prompt", message="waiting"), T0)
    assert s["status"] == state.WORKING
    s = state.apply_event(s, ev("Notification", notification_type="permission_prompt", message="needs ok"), T0)
    assert s["status"] == state.NEEDS_YOU


def test_keeps_only_a_short_summary():
    big = "x" * 10_000
    s = state.apply_event(None, ev("PreToolUse", tool_name="Write", tool_input={"file_path": "/a/b.txt", "content": big}), T0)
    assert s["detail"] == "/a/b.txt"
    s = state.apply_event(None, ev("PreToolUse", tool_name="Bash", tool_input={"command": big}), T0)
    assert len(s["detail"]) == state.DETAIL_MAX
    s = state.apply_event(s, ev("UserPromptSubmit", prompt="first prompt"), T0)
    s = state.apply_event(s, ev("UserPromptSubmit", prompt="secret plans"), T0)
    assert "secret" not in repr(s)  # only the first prompt's first line is kept, as the title


def test_folder_name():
    assert state.folder_name("/home/me/project/") == "project"
    assert state.folder_name("C:\\Users\\me\\repo") == "repo"
    assert state.folder_name(None) == ""


def test_quiet_sessions_read_as_idle_and_seen_clears_done():
    s = state.apply_event(None, ev("UserPromptSubmit"), T0)
    assert state.effective_status(s, T0 + 10, idle_after=60) == state.WORKING
    assert state.effective_status(s, T0 + 61, idle_after=60) == state.IDLE
    done = state.apply_event(s, ev("Stop"), T0)
    assert state.effective_status(done, T0 + 9999, idle_after=60) == state.DONE_UNSEEN
    assert state.mark_seen(done)["status"] == state.IDLE


def test_title_comes_from_the_first_prompt_only():
    s = state.apply_event(None, ev("UserPromptSubmit", prompt="  \n Deploy   the board\nsecond line"), T0)
    assert s["title"] == "Deploy the board"
    s = state.apply_event(s, ev("UserPromptSubmit", prompt="something else"), T0)
    assert s["title"] == "Deploy the board"
    long = state.apply_event(None, ev("UserPromptSubmit", prompt="x" * 500), T0)
    assert len(long["title"]) == state.TITLE_MAX
