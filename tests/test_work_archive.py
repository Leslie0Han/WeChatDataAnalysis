from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wechat_decrypt_tool import work_archive


def _record(ts: int = 1) -> dict:
    return {
        "time": "1970-01-01 00:00",
        "ts": ts,
        "sender": "sender",
        "sender_wxid": "user",
        "type": "text",
        "content": "hello",
        "archived": None,
        "card": None,
    }


def _profile(root: Path, **overrides) -> work_archive.WorkArchiveProfile:
    payload = {
        "id": "work-chats",
        "name": "Work chats",
        "account": "account-a",
        "archiveRoot": str(root),
    }
    payload.update(overrides)
    return work_archive.WorkArchiveProfile.from_dict(payload)


def _row(local_id: int, *, server_id: int = 0, create_time: int = 100, sort_seq: int = 0):
    return SimpleNamespace(
        db_stem="message_0",
        table_name="Msg_test",
        local_id=local_id,
        server_id=server_id,
        create_time=create_time,
        sort_seq=sort_seq,
    )


def test_profile_store_is_runtime_only_and_forces_fixed_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    output = tmp_path / "output"
    monkeypatch.setenv("WECHAT_TOOL_OUTPUT_DIR", str(output))
    store = work_archive.WorkArchiveProfileStore()
    profile = _profile(tmp_path / "archive", scanSeconds=1, quietSeconds=1, maxDelaySeconds=1)
    store.save(profile)

    restored = store.get(profile.id)
    assert restored.scanSeconds == 10
    assert restored.quietSeconds == 30
    assert restored.maxDelaySeconds == 120
    assert store.path == output / "work_archive" / "profiles.json"
    stored = json.loads(store.path.read_text(encoding="utf-8"))
    assert stored["profiles"][0]["archiveRoot"] == str(tmp_path / "archive")


def test_profile_rejects_relative_archive_root():
    with pytest.raises(ValueError, match="absolute"):
        work_archive.WorkArchiveProfile.from_dict(
            {"id": "work", "name": "Work", "account": "a", "archiveRoot": "relative/path"}
        )


def test_state_deduplicates_server_and_fallback_keys_with_same_second(tmp_path: Path):
    profile = _profile(tmp_path / "archive")
    state = work_archive.WorkArchiveState(profile)
    rows = [
        _row(1, server_id=900, create_time=100, sort_seq=1),
        _row(2, create_time=100, sort_seq=2),
        _row(3, create_time=100, sort_seq=3),
    ]
    with state.connect() as conn:
        state.remember_rows(conn, "conversation", rows)
        assert state.has_key(conn, "conversation", "server:900")
        assert state.has_key(conn, "conversation", "local:message_0:Msg_test:2")
        assert state.cursor(conn, "conversation") == (100, 3, 3)
        state.remember_rows(conn, "conversation", rows)
        count = conn.execute("SELECT COUNT(*) FROM message_keys").fetchone()[0]
    assert count == 3


def test_atomic_replace_failure_preserves_previous_file(tmp_path: Path):
    target = tmp_path / "archive.json"
    target.write_text("old", encoding="utf-8")
    with patch.object(work_archive.os, "replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            work_archive._atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.tmp"))


def _make_adoption_fixture(root: Path, source_count: int = 1):
    display = "Conversation"
    username = "conversation-id"
    conversation = root / display
    conversation.mkdir(parents=True)
    (conversation / f"聊天记录_{display}.json").write_text(
        json.dumps([_record()], ensure_ascii=False), encoding="utf-8"
    )
    (conversation / "manifest.json").write_text("[]", encoding="utf-8")
    (root / "_archive_plan.json").write_text(
        json.dumps(
            {
                "work_groups": [],
                "work_singles": [
                    {"username": username, "display": display, "is_group": False, "msg_count": 1}
                ],
                "borderline": [],
                "excluded": [{"username": "known-old"}],
            }
        ),
        encoding="utf-8",
    )
    return username, source_count


def test_adoption_preflight_accepts_matching_archive(tmp_path: Path):
    root = tmp_path / "archive"
    _username, source_count = _make_adoption_fixture(root)
    service = work_archive.WorkArchiveService()
    with (
        patch.object(service, "_account_realtime", return_value=(tmp_path / "account", object())),
        patch.object(work_archive.chat_export, "_iter_rows_for_conversation", return_value=[_row(1, create_time=1)]),
        patch.object(work_archive, "_archive_record_matches_source_row", return_value=True),
    ):
        report = service.preflight_adoption(account="account-a", archive_root=str(root))
    assert report["ok"] is True
    assert report["conversationCount"] == 1
    assert report["messageCount"] == 1
    assert report["pendingMessageCount"] == 0
    assert (root / ".work-archive" / "adoption-diff.json").exists()


def test_adoption_preflight_accepts_verified_realtime_backlog(tmp_path: Path):
    root = tmp_path / "archive"
    _make_adoption_fixture(root)
    service = work_archive.WorkArchiveService()
    with (
        patch.object(service, "_account_realtime", return_value=(tmp_path / "account", object())),
        patch.object(
            work_archive.chat_export,
            "_iter_rows_for_conversation",
            return_value=[_row(1, create_time=1), _row(2, create_time=2)],
        ),
        patch.object(work_archive, "_archive_record_matches_source_row", return_value=True),
    ):
        report = service.preflight_adoption(account="account-a", archive_root=str(root))
    assert report["ok"] is True
    assert report["pendingMessageCount"] == 1
    assert report["details"][0]["pendingCount"] == 1


def test_adoption_preflight_blocks_source_behind_or_non_prefix(tmp_path: Path):
    root = tmp_path / "archive"
    _make_adoption_fixture(root)
    service = work_archive.WorkArchiveService()
    with (
        patch.object(service, "_account_realtime", return_value=(tmp_path / "account", object())),
        patch.object(work_archive.chat_export, "_iter_rows_for_conversation", return_value=[]),
    ):
        behind = service.preflight_adoption(account="account-a", archive_root=str(root))
    assert behind["ok"] is False
    assert "source=0 is behind archive=1" in behind["details"][0]["errors"]

    with (
        patch.object(service, "_account_realtime", return_value=(tmp_path / "account", object())),
        patch.object(work_archive.chat_export, "_iter_rows_for_conversation", return_value=[_row(1, create_time=1)]),
        patch.object(work_archive, "_archive_record_matches_source_row", return_value=False),
    ):
        non_prefix = service.preflight_adoption(account="account-a", archive_root=str(root))
    assert non_prefix["ok"] is False
    assert "archive is not a realtime prefix" in non_prefix["details"][0]["errors"][0]


def test_adoption_seeds_overlap_and_keeps_auto_archive_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_TOOL_OUTPUT_DIR", str(tmp_path / "output"))
    root = tmp_path / "archive"
    username, _ = _make_adoption_fixture(root)
    rows = [_row(index, server_id=1000 + index, create_time=index) for index in range(1, 121)]
    service = work_archive.WorkArchiveService()
    with (
        patch.object(
            service,
            "preflight_adoption",
            return_value={
                "ok": True,
                "status": "success",
                "details": [{"username": username, "archiveCount": 1}],
            },
        ),
        patch.object(service, "_account_realtime", return_value=(tmp_path / "account", object())),
        patch.object(work_archive.chat_export, "_iter_rows_for_conversation", return_value=rows),
    ):
        result = service.adopt(
            profile_id="work-chats", name="Work chats", account="account-a", archive_root=str(root)
        )
    profile = work_archive.WorkArchiveProfile.from_dict(result["profile"])
    assert profile.enabled is False
    assert profile.includedUsernames == [username]
    assert "known-old" in profile.excludedUsernames
    state = work_archive.WorkArchiveState(profile)
    with state.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM message_keys").fetchone()[0] == 1
        assert state.cursor(conn, username) == (1, 0, 1)


def test_pending_conversation_requires_explicit_status_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_TOOL_OUTPUT_DIR", str(tmp_path / "output"))
    service = work_archive.WorkArchiveService()
    profile = _profile(
        tmp_path / "archive",
        pendingUsernames=["new-conversation"],
        pendingMeta={"new-conversation": {"displayName": "New", "isGroup": False}},
    )
    service.store.save(profile)
    pending = service.list_pending(profile.id)
    assert pending["items"] == [
        {"username": "new-conversation", "displayName": "New", "isGroup": False}
    ]
    updated = service.set_conversation_status(profile.id, "new-conversation", "included")
    assert "new-conversation" in updated["includedUsernames"]
    assert "new-conversation" not in updated["pendingUsernames"]


def test_display_name_change_does_not_change_stable_archive_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_TOOL_OUTPUT_DIR", str(tmp_path / "output"))
    service = work_archive.WorkArchiveService()
    profile = _profile(
        tmp_path / "archive",
        includedUsernames=["stable-username"],
        conversations={
            "stable-username": {"displayName": "Old name", "archiveDir": "Old name", "isGroup": False}
        },
    )
    service.store.save(profile)
    monkeypatch.setattr(
        work_archive.chat_export,
        "build_chat_export_targets_preview",
        lambda **_kwargs: {
            "source": "realtime",
            "targets": [{"username": "stable-username", "displayName": "New name", "isGroup": False}],
        },
    )
    service.discover_pending(profile, tmp_path / "account")
    updated = service.store.get(profile.id)
    assert updated.conversations["stable-username"]["displayName"] == "New name"
    assert updated.conversations["stable-username"]["archiveDir"] == "Old name"


def test_archive_adapter_preserves_exact_eight_fields():
    result = work_archive._record_from_message(
        {
            "createTime": 100,
            "senderUsername": "stable-user",
            "senderDisplayName": "Display",
            "renderType": "text",
            "content": "hello",
        },
        {},
    )
    assert tuple(result) == work_archive.ARCHIVE_FIELDS
    assert result["sender_wxid"] == "stable-user"


def test_pending_media_queue_survives_restart(tmp_path: Path):
    profile = _profile(tmp_path / "archive")
    first = work_archive.WorkArchiveState(profile)
    with first.connect() as conn:
        first.queue_media(conn, "conversation", "server:1", {"recordIndex": 0, "message": {}})
    second = work_archive.WorkArchiveState(profile)
    with second.connect() as conn:
        row = conn.execute("SELECT attempts, next_attempt_at FROM pending_media").fetchone()
    assert row is not None
    assert int(row[0]) == 0
    assert int(row[1]) > 0


def test_delayed_local_media_is_linked_and_removed_from_retry_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    archive_root = tmp_path / "archive"
    profile = _profile(
        archive_root,
        includedUsernames=["conversation"],
        conversations={
            "conversation": {"displayName": "Conversation", "archiveDir": "Conversation", "isGroup": False}
        },
    )
    conversation_dir = archive_root / "Conversation"
    conversation_dir.mkdir(parents=True)
    json_path = conversation_dir / "聊天记录_Conversation.json"
    json_path.write_text(json.dumps([{**_record(), "type": "image", "content": "【图片】"}]), encoding="utf-8")
    (conversation_dir / "manifest.json").write_text("[]", encoding="utf-8")
    state = work_archive.WorkArchiveState(profile)
    service = work_archive.WorkArchiveService()
    message = {
        "createTime": 1,
        "senderUsername": "sender",
        "senderDisplayName": "Sender",
        "renderType": "image",
        "content": "[图片]",
    }
    monkeypatch.setattr(work_archive, "_materialize_local_media", lambda *_args, **_kwargs: "图片/local.jpg")
    with state.connect() as conn:
        state.queue_media(conn, "conversation", "server:1", {"recordIndex": 0, "message": message})
        conn.execute("UPDATE pending_media SET next_attempt_at = 0")
        resolved = service._retry_pending_media(
            profile=profile,
            account_dir=tmp_path / "account",
            state=state,
            conn=conn,
            display_names={"sender": "Sender"},
        )
        assert conn.execute("SELECT COUNT(*) FROM pending_media").fetchone()[0] == 0
    records = json.loads(json_path.read_text(encoding="utf-8"))
    assert resolved == 1
    assert records[0]["archived"] == "图片/local.jpg"
    assert "图片/local.jpg" in (conversation_dir / "聊天记录_Conversation.md").read_text(encoding="utf-8")


def test_sync_restart_catch_up_is_idempotent_and_keeps_archive_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WECHAT_TOOL_OUTPUT_DIR", str(tmp_path / "output"))
    archive_root = tmp_path / "archive"
    profile = _profile(
        archive_root,
        includedUsernames=["conversation"],
        conversations={
            "conversation": {"displayName": "Original name", "archiveDir": "Original name", "isGroup": False}
        },
    )
    service = work_archive.WorkArchiveService()
    service.store.save(profile)
    rows = [_row(1, server_id=101, create_time=100), _row(2, server_id=102, create_time=101)]
    monkeypatch.setattr(service, "_account_realtime", lambda _profile: (tmp_path / "account", object()))
    monkeypatch.setattr(service, "discover_pending", lambda _profile, _account_dir: 0)
    monkeypatch.setattr(work_archive.chat_export, "_iter_rows_for_conversation", lambda **_kwargs: list(rows))
    monkeypatch.setattr(
        work_archive.chat_export,
        "_parse_message_for_export",
        lambda **kwargs: {
            "createTime": kwargs["row"].create_time,
            "senderUsername": "stable-sender",
            "renderType": "text",
            "content": f"message-{kwargs['row'].local_id}",
        },
    )
    monkeypatch.setattr(work_archive, "_materialize_local_media", lambda *_args, **_kwargs: "")

    first = service.run_sync(profile.id)
    second = service.run_sync(profile.id)

    assert first["added"] == 2
    assert second["added"] == 0
    json_path = archive_root / "Original name" / "聊天记录_Original name.json"
    records = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(records) == 2
    assert all(tuple(record) == work_archive.ARCHIVE_FIELDS for record in records)
    assert not (archive_root / "stable-sender").exists()


def test_local_media_bridge_contains_no_remote_fetch_path():
    import inspect

    source = inspect.getsource(work_archive._materialize_local_media)
    assert "requests" not in source
    assert "cdn" not in source.lower()


def test_monitor_caps_continuous_activity_at_120_seconds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    profile = _profile(tmp_path / "archive", enabled=True)
    service = work_archive.WorkArchiveService()
    monkeypatch.setattr(service.store, "list", lambda: [profile])
    monitor = work_archive.WorkArchiveMonitor(service)
    monitor._states[profile.id] = work_archive._MonitorProfileState(last_reconcile_at=1000)
    storage = tmp_path / "storage"
    storage.mkdir()
    monkeypatch.setattr(work_archive, "_resolve_account_dir", lambda _account: tmp_path / "account")
    monkeypatch.setattr(
        work_archive.WCDB_REALTIME,
        "get_status",
        lambda _account_dir: {"dll_present": True, "key_present": True, "db_storage_dir": str(storage)},
    )
    scan_value = {"value": 0}
    monkeypatch.setattr(
        work_archive,
        "scan_db_storage_mtime_ns",
        lambda _storage: scan_value.__setitem__("value", scan_value["value"] + 1) or scan_value["value"],
    )
    clock = {"now": 1000.0}
    monkeypatch.setattr(work_archive.time, "time", lambda: clock["now"])
    started = []

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            self.started = False

        def start(self):
            self.started = True
            started.append(clock["now"])

        def is_alive(self):
            return self.started

    monkeypatch.setattr(work_archive.threading, "Thread", FakeThread)
    for current in range(1000, 1120, 10):
        clock["now"] = float(current)
        monitor.tick()
        assert not started
    clock["now"] = 1120.0
    monitor.tick()
    assert started == [1120.0]


def test_monitor_waits_without_snapshot_when_realtime_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    profile = _profile(tmp_path / "archive", enabled=True)
    service = work_archive.WorkArchiveService()
    monkeypatch.setattr(service.store, "list", lambda: [profile])
    monkeypatch.setattr(work_archive, "_resolve_account_dir", lambda _account: tmp_path / "account")
    monkeypatch.setattr(
        work_archive.WCDB_REALTIME,
        "get_status",
        lambda _account_dir: {"dll_present": True, "key_present": False, "db_storage_dir": ""},
    )
    monitor = work_archive.WorkArchiveMonitor(service)
    monitor.tick()
    assert service._status[profile.id]["state"] == "waiting_realtime"
    assert monitor._states[profile.id].worker is None
