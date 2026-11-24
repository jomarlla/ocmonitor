import json
import sqlite3
import time
from pathlib import Path

from ocmonitor.utils.sqlite_utils import SQLiteProcessor


def _assistant_message(tokens: int = 10) -> str:
    return json.dumps(
        {
            "role": "assistant",
            "tokens": {"input": tokens, "output": 0, "cache": {"write": 0, "read": 0}},
            "time": {
                "created": int(time.time() * 1000),
                "completed": int(time.time() * 1000),
            },
        }
    )


def _create_base_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE project (
            id TEXT PRIMARY KEY,
            worktree TEXT,
            name TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            parent_id TEXT,
            title TEXT,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            time_updated INTEGER NOT NULL,
            data TEXT NOT NULL
        )
        """
    )


def test_get_all_active_workflows_orders_by_latest_parent_activity(tmp_path: Path):
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _create_base_schema(conn)

    now_ms = int(time.time() * 1000)

    conn.execute(
        "INSERT INTO project (id, worktree, name) VALUES (?, ?, ?)",
        ("proj", "/tmp/project", "project"),
    )

    # This parent is created earlier, but has the newest parent message.
    conn.execute(
        "INSERT INTO session (id, project_id, parent_id, title, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "older-parent",
            "proj",
            None,
            "Older Parent",
            now_ms - 10_000,
            now_ms - 10_000,
        ),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "msg-older-new",
            "older-parent",
            now_ms - 1_000,
            now_ms - 1_000,
            _assistant_message(),
        ),
    )

    # This parent is created later, but has an older parent message.
    conn.execute(
        "INSERT INTO session (id, project_id, parent_id, title, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?)",
        ("newer-parent", "proj", None, "Newer Parent", now_ms - 2_000, now_ms - 2_000),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (
            "msg-newer-old",
            "newer-parent",
            now_ms - 5_000,
            now_ms - 5_000,
            _assistant_message(),
        ),
    )

    conn.commit()
    conn.close()

    workflows = SQLiteProcessor.get_all_active_workflows(
        db_path=db_path,
        active_threshold_minutes=60,
    )

    assert [w["workflow_id"] for w in workflows[:2]] == ["older-parent", "newer-parent"]


def test_get_workflow_by_session_id_loads_finished_session(tmp_path: Path):
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _create_base_schema(conn)

    old_ms = int(time.time() * 1000) - 3 * 24 * 60 * 60 * 1000
    conn.execute(
        "INSERT INTO project (id, worktree, name) VALUES (?, ?, ?)",
        ("proj", "/tmp/project", "project"),
    )
    conn.execute(
        "INSERT INTO session (id, project_id, parent_id, title, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?)",
        ("old-parent", "proj", None, "Old Parent", old_ms, old_ms),
    )
    conn.execute(
        "INSERT INTO session (id, project_id, parent_id, title, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?)",
        ("old-sub", "proj", "old-parent", "Old Sub", old_ms + 1000, old_ms + 1000),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        ("msg-parent", "old-parent", old_ms, old_ms, _assistant_message()),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        ("msg-sub", "old-sub", old_ms + 1000, old_ms + 1000, _assistant_message()),
    )
    conn.commit()
    conn.close()

    # The finished session is outside the active window...
    assert SQLiteProcessor.get_all_active_workflows(db_path) == []

    # ...but is still listed when browsing all workflows and can be loaded
    # by ID, alone or through its sub-agent.
    all_workflows = SQLiteProcessor.get_all_workflows(db_path)
    assert [w["workflow_id"] for w in all_workflows] == ["old-parent"]
    workflow = SQLiteProcessor.get_workflow_by_session_id("old-parent", db_path)
    assert workflow is not None
    assert workflow["workflow_id"] == "old-parent"
    assert workflow["sub_agent_count"] == 1

    via_sub = SQLiteProcessor.get_workflow_by_session_id("old-sub", db_path)
    assert via_sub is not None
    assert via_sub["workflow_id"] == "old-parent"

    assert SQLiteProcessor.get_workflow_by_session_id("missing", db_path) is None


def test_resolve_session_id_by_unique_prefix(tmp_path: Path):
    db_path = tmp_path / "opencode.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _create_base_schema(conn)

    now_ms = int(time.time() * 1000)
    sessions = [
        ("ses_abc123", now_ms - 3000),
        ("ses_abd456", now_ms - 2000),
        ("ses_xyz789", now_ms - 1000),
        # Underscore must be matched literally, not as a LIKE wildcard.
        ("sesXabc000", now_ms),
    ]
    for session_id, created in sessions:
        conn.execute(
            "INSERT INTO session (id, project_id, parent_id, title, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, "proj", None, session_id, created, created),
        )
    conn.commit()
    conn.close()

    # Exact ID wins.
    assert SQLiteProcessor.resolve_session_id("ses_abc123", db_path) == (
        "ses_abc123",
        [],
    )

    # A unique prefix resolves to the full ID.
    assert SQLiteProcessor.resolve_session_id("ses_abc", db_path) == (
        "ses_abc123",
        [],
    )
    assert SQLiteProcessor.resolve_session_id("ses_xyz7", db_path) == (
        "ses_xyz789",
        [],
    )

    # An ambiguous prefix returns all candidates and no full ID.
    full_id, candidates = SQLiteProcessor.resolve_session_id("ses_ab", db_path)
    assert full_id is None
    assert set(candidates) == {"ses_abc123", "ses_abd456"}

    # The underscore is literal: "ses_" does not match "sesXabc000".
    full_id, candidates = SQLiteProcessor.resolve_session_id("ses_", db_path)
    assert full_id is None
    assert set(candidates) == {"ses_abc123", "ses_abd456", "ses_xyz789"}

    assert SQLiteProcessor.resolve_session_id("nope", db_path) == (None, [])
    assert SQLiteProcessor.resolve_session_id("", db_path) == (None, [])
