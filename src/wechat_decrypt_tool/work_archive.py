"""Realtime, incremental archives for user-confirmed work conversations.

Runtime profiles live below the application's output directory. Archive state is
kept beside the archive itself so a profile can be moved or backed up as one unit.
Only direct WCDB realtime reads are accepted; this module never falls back to a
decrypted snapshot or a remote media URL.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .app_paths import get_output_dir
from .chat_helpers import _resolve_account_dir
from .db_change_watcher import scan_db_storage_mtime_ns
from .logging_config import get_logger
from .wcdb_realtime import WCDB_REALTIME
from . import chat_export_service as chat_export

logger = get_logger(__name__)

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ARCHIVE_FIELDS = ("time", "ts", "sender", "sender_wxid", "type", "content", "archived", "card")
MEDIA_RETRY_SECONDS = (30, 120, 600, 3600)
URL_RE = re.compile(r"https?://[^\s，。、]+", re.IGNORECASE)
LEGACY_LOCAL_TYPE_LABELS = {
    3: "image",
    34: "voice",
    43: "video",
    47: "emoji",
    48: "location",
    49: "file",
    10000: "system",
    25769803825: "file",
    244813135921: "file",
}
LEGACY_SOURCE_TYPE_COMPATIBILITY = {
    "公众号": {"link"},
    "链接卡片": {"link", "system", "text"},
    "聊天记录转发": {"chathistory"},
    "视频号": {"chathistory", "link"},
    "通话": {"voip"},
    "小程序": {"link", "miniprogram"},
    "binary": {"text"},
    "file": {"file", "quote"},
    "link": {"text", "link"},
    "longtext": {"text"},
}


def _now() -> int:
    return int(time.time())


def _safe_component(value: str, fallback: str = "conversation") -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", str(value or "").strip()).strip(" .")
    return value[:100] or fallback


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


@dataclass
class WorkArchiveProfile:
    id: str
    name: str
    account: str
    archiveRoot: str
    enabled: bool = False
    includedUsernames: list[str] = field(default_factory=list)
    pendingUsernames: list[str] = field(default_factory=list)
    excludedUsernames: list[str] = field(default_factory=list)
    conversations: dict[str, dict[str, Any]] = field(default_factory=dict)
    pendingMeta: dict[str, dict[str, Any]] = field(default_factory=dict)
    mediaPolicy: str = "local_only"
    scanSeconds: int = 10
    quietSeconds: int = 30
    maxDelaySeconds: int = 120
    reconcileSeconds: int = 1800
    adoptedAt: int = 0

    def validate(self) -> None:
        if not PROFILE_ID_RE.fullmatch(str(self.id or "")):
            raise ValueError("Profile id must use lowercase letters, digits, '-' or '_'.")
        if not str(self.name or "").strip():
            raise ValueError("Profile name is required.")
        if not str(self.account or "").strip():
            raise ValueError("Profile account is required.")
        root = Path(str(self.archiveRoot or "")).expanduser()
        if not root.is_absolute():
            raise ValueError("archiveRoot must be an absolute path.")
        if self.mediaPolicy != "local_only":
            raise ValueError("Only the local_only media policy is supported.")
        self.scanSeconds = 10
        self.quietSeconds = 30
        self.maxDelaySeconds = 120
        self.reconcileSeconds = 1800
        self.includedUsernames = sorted({str(item).strip() for item in self.includedUsernames if str(item).strip()})
        self.pendingUsernames = sorted({str(item).strip() for item in self.pendingUsernames if str(item).strip()})
        self.excludedUsernames = sorted({str(item).strip() for item in self.excludedUsernames if str(item).strip()})
        included = set(self.includedUsernames)
        self.pendingUsernames = [item for item in self.pendingUsernames if item not in included]
        self.excludedUsernames = [item for item in self.excludedUsernames if item not in included]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkArchiveProfile":
        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        profile = cls(**{key: value for key, value in dict(payload or {}).items() if key in allowed})
        profile.validate()
        return profile

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


class WorkArchiveProfileStore:
    @property
    def root(self) -> Path:
        return get_output_dir() / "work_archive"

    @property
    def path(self) -> Path:
        return self.root / "profiles.json"

    def list(self) -> list[WorkArchiveProfile]:
        payload = _read_json(self.path, {"profiles": []})
        profiles: list[WorkArchiveProfile] = []
        for item in payload.get("profiles", []) if isinstance(payload, dict) else []:
            try:
                profiles.append(WorkArchiveProfile.from_dict(item))
            except Exception:
                logger.exception("[work-archive] ignored invalid profile")
        return profiles

    def get(self, profile_id: str) -> WorkArchiveProfile:
        wanted = str(profile_id or "").strip()
        for profile in self.list():
            if profile.id == wanted:
                return profile
        raise KeyError(f"Unknown archive profile: {wanted}")

    def save(self, profile: WorkArchiveProfile) -> WorkArchiveProfile:
        profile.validate()
        profiles = self.list()
        replaced = False
        for index, current in enumerate(profiles):
            if current.id == profile.id:
                profiles[index] = profile
                replaced = True
                break
        if not replaced:
            profiles.append(profile)
        _atomic_write_json(self.path, {"version": 1, "profiles": [item.to_dict() for item in profiles]})
        return profile

    def delete(self, profile_id: str) -> bool:
        profiles = self.list()
        kept = [item for item in profiles if item.id != str(profile_id or "").strip()]
        if len(kept) == len(profiles):
            return False
        _atomic_write_json(self.path, {"version": 1, "profiles": [item.to_dict() for item in kept]})
        return True


class WorkArchiveState:
    def __init__(self, profile: WorkArchiveProfile):
        self.profile = profile
        self.root = Path(profile.archiveRoot) / ".work-archive"
        self.path = self.root / "state.sqlite3"

    def connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS cursors (
                username TEXT PRIMARY KEY,
                create_time INTEGER NOT NULL DEFAULT 0,
                sort_seq INTEGER NOT NULL DEFAULT 0,
                local_id INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS message_keys (
                username TEXT NOT NULL,
                message_key TEXT NOT NULL,
                create_time INTEGER NOT NULL DEFAULT 0,
                sort_seq INTEGER NOT NULL DEFAULT 0,
                local_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (username, message_key)
            );
            CREATE INDEX IF NOT EXISTS ix_message_keys_time ON message_keys(username, create_time);
            CREATE TABLE IF NOT EXISTS pending_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                message_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                UNIQUE(username, message_key)
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_at INTEGER NOT NULL,
                finished_at INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                added INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            );
            """
        )
        return conn

    def cursor(self, conn: sqlite3.Connection, username: str) -> tuple[int, int, int]:
        row = conn.execute(
            "SELECT create_time, sort_seq, local_id FROM cursors WHERE username = ?", (username,)
        ).fetchone()
        if not row:
            return 0, 0, 0
        return int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)

    def has_key(self, conn: sqlite3.Connection, username: str, message_key: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM message_keys WHERE username = ? AND message_key = ?", (username, message_key)
        ).fetchone() is not None

    def remember_rows(self, conn: sqlite3.Connection, username: str, rows: Iterable[Any]) -> None:
        last = None
        for row in rows:
            message_key = message_identity(row)
            conn.execute(
                "INSERT OR IGNORE INTO message_keys(username, message_key, create_time, sort_seq, local_id) VALUES(?,?,?,?,?)",
                (username, message_key, int(row.create_time or 0), int(row.sort_seq or 0), int(row.local_id or 0)),
            )
            last = row
        if last is not None:
            conn.execute(
                """INSERT INTO cursors(username, create_time, sort_seq, local_id, updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(username) DO UPDATE SET create_time=excluded.create_time,
                sort_seq=excluded.sort_seq, local_id=excluded.local_id, updated_at=excluded.updated_at""",
                (username, int(last.create_time or 0), int(last.sort_seq or 0), int(last.local_id or 0), _now()),
            )

    def queue_media(self, conn: sqlite3.Connection, username: str, message_key: str, payload: dict[str, Any]) -> None:
        conn.execute(
            """INSERT INTO pending_media(username, message_key, payload_json, attempts, next_attempt_at)
            VALUES(?,?,?,?,?) ON CONFLICT(username, message_key) DO UPDATE SET payload_json=excluded.payload_json""",
            (username, message_key, json.dumps(payload, ensure_ascii=False), 0, _now() + MEDIA_RETRY_SECONDS[0]),
        )


def message_identity(row: Any) -> str:
    server_id = int(getattr(row, "server_id", 0) or 0)
    if server_id:
        return f"server:{server_id}"
    return "local:{0}:{1}:{2}".format(
        str(getattr(row, "db_stem", "") or ""),
        str(getattr(row, "table_name", "") or ""),
        int(getattr(row, "local_id", 0) or 0),
    )


class _ArchiveMediaSink:
    """ZipFile-shaped local sink used by the existing offline media materializer."""

    FOLDERS = {
        "images": "图片",
        "emojis": "图片",
        "video_thumbs": "图片",
        "videos": "视频",
        "files": "文件",
        "voices": "语音",
    }

    def __init__(self, conversation_dir: Path):
        self.conversation_dir = conversation_dir
        self.last_relative = ""

    def _target(self, arcname: str) -> Path:
        parts = Path(str(arcname or "").replace("\\", "/")).parts
        bucket = parts[-2] if len(parts) >= 2 else "misc"
        folder = self.FOLDERS.get(bucket, "其他")
        filename = _safe_component(parts[-1] if parts else "media.dat", "media.dat")
        target = self.conversation_dir / folder / filename
        self.last_relative = f"{folder}/{filename}"
        return target

    def write(self, filename: Path | str, arcname: str) -> None:
        target = self._target(arcname)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(str(filename), str(temp))
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def writestr(self, arcname: str, data: bytes | bytearray | str) -> None:
        target = self._target(arcname)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_bytes(payload)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)


def _materialize_local_media(account_dir: Path, username: str, conversation_dir: Path, msg: dict[str, Any]) -> str:
    render_type = str(msg.get("renderType") or "")
    sink = _ArchiveMediaSink(conversation_dir)
    written: dict[str, str] = {}
    arc = ""
    if render_type == "voice" and int(msg.get("serverId") or 0) > 0:
        arc, _ = chat_export._materialize_voice(
            zf=sink,
            account_dir=account_dir,
            media_db_path=account_dir / "media.db",
            server_id=int(msg.get("serverId") or 0),
            media_written=written,
        )
    else:
        attempts: list[tuple[str, str, str, str]] = []
        if render_type == "image":
            md5_values = [str(msg.get("imageMd5") or ""), *[str(item or "") for item in msg.get("imageMd5Candidates") or []]]
            file_values = [str(msg.get("imageFileId") or ""), *[str(item or "") for item in msg.get("imageFileIdCandidates") or []]]
            attempts = [("image", md5, "", "") for md5 in dict.fromkeys(md5_values) if md5]
            attempts.extend(("image", "", file_id, "") for file_id in dict.fromkeys(file_values) if file_id)
        elif render_type == "emoji":
            attempts = [("emoji", str(msg.get("emojiMd5") or ""), "", "")]
        elif render_type == "video":
            attempts = [
                ("video", str(msg.get("videoMd5") or ""), str(msg.get("videoFileId") or ""), ""),
                ("video_thumb", str(msg.get("videoThumbMd5") or ""), str(msg.get("videoThumbFileId") or ""), ""),
            ]
        elif render_type == "file":
            attempts = [
                ("file", str(msg.get("fileMd5") or ""), str(msg.get("fileFileId") or ""), str(msg.get("title") or ""))
            ]
        for kind, md5, file_id, suggested_name in attempts:
            if not md5 and not file_id:
                continue
            arc, _ = chat_export._materialize_media(
                zf=sink,
                account_dir=account_dir,
                conv_username=username,
                kind=kind,
                md5=md5.strip().lower(),
                file_id=file_id.strip(),
                media_written=written,
                suggested_name=suggested_name,
                media_index=None,
            )
            if arc:
                break
    return sink.last_relative if arc else ""


def _legacy_archive_type(row: Any, msg: dict[str, Any]) -> str:
    local_type = int(getattr(row, "local_type", 0) or 0)
    if local_type == 1:
        content = str(msg.get("content") or "")
        if URL_RE.search(content):
            return "link"
        if len(content) > 400:
            return "longtext"
        return "text"
    if local_type in LEGACY_LOCAL_TYPE_LABELS:
        return LEGACY_LOCAL_TYPE_LABELS[local_type]

    render_type = str(msg.get("renderType") or "text")
    render_key = render_type.lower()
    if render_key == "chathistory":
        return "聊天记录转发"
    if render_key == "voip":
        return "通话"
    if render_key == "channels":
        return "视频号"
    if render_key == "miniprogram":
        return "小程序"
    if render_key == "link":
        link_text = " ".join(str(msg.get(key) or "") for key in ("url", "content"))
        return "公众号" if "mp.weixin.qq.com" in link_text.lower() else "链接卡片"
    return render_type


def _legacy_type_is_compatible(archive_type: str, source_type: str) -> bool:
    archive_value = str(archive_type or "text")
    source_value = str(source_type or "text").lower()
    return archive_value.lower() == source_value or source_value in LEGACY_SOURCE_TYPE_COMPATIBILITY.get(
        archive_value, set()
    )


def _card_from_message(msg: dict[str, Any], archive_type: str = "") -> Optional[dict[str, str]]:
    render_type = str(msg.get("renderType") or "")
    if render_type.lower() not in {"link", "quote", "chathistory", "miniprogram", "channels"}:
        return None
    return {
        "type": archive_type or render_type,
        "title": str(msg.get("title") or msg.get("quoteTitle") or ""),
        "url": str(msg.get("url") or ""),
        "des": str(msg.get("content") or msg.get("quoteContent") or ""),
        "author": str(msg.get("from") or msg.get("fromUsername") or ""),
    }


def _record_from_message(msg: dict[str, Any], display_names: dict[str, str], archived: str = "") -> dict[str, Any]:
    timestamp = int(msg.get("createTime") or 0)
    sender_username = str(msg.get("senderUsername") or "").strip()
    sender = str(msg.get("senderDisplayName") or display_names.get(sender_username) or sender_username or "未知")
    render_type = str(msg.get("renderType") or "text")
    archive_type = str(msg.get("_archiveType") or render_type)
    content = str(msg.get("content") or "").strip()
    if archived:
        filename = Path(archived).name
        if render_type == "image":
            content = f"【图片】[{filename}]({archived})"
        elif render_type == "video":
            content = f"【视频】[{filename}]({archived})"
        elif render_type == "file":
            content = f"【文件】[`{str(msg.get('title') or filename)}`]({archived})"
        elif render_type == "voice":
            content = f"【语音】[{filename}]({archived})"
        elif render_type == "emoji":
            content = f"【表情】[{filename}]({archived})"
    elif render_type in {"image", "video", "file", "voice", "emoji"}:
        content = content or f"【{render_type}】"
    elif archive_type in {"公众号", "链接卡片", "聊天记录转发", "视频号", "通话", "小程序"}:
        if not content.startswith(f"【{archive_type}】"):
            content = f"【{archive_type}】{content}"
    elif archive_type == "link" and not content.startswith("【链接】"):
        content = f"【链接】{content}"
    elif archive_type == "longtext" and not content.startswith("【长文】"):
        content = f"【长文】{content}"
    local_time = dt.datetime.fromtimestamp(timestamp)
    return {
        "time": local_time.strftime("%Y-%m-%d %H:%M"),
        "ts": timestamp,
        "sender": sender,
        "sender_wxid": sender_username,
        "type": archive_type,
        "content": content,
        "archived": archived or None,
        "card": _card_from_message(msg, archive_type),
    }


def _archive_record_matches_source_row(record: dict[str, Any], row: Any, username: str) -> bool:
    try:
        msg = chat_export._parse_message_for_export(
            row=row,
            conv_username=username,
            is_group=bool(username.endswith("@chatroom")),
            resource_conn=None,
            resource_chat_id=None,
            resolve_display_name=lambda value: value,
        )
    except Exception:
        return False
    archive_type = str(record.get("type") or "text")
    source_type = str(msg.get("renderType") or "text")
    source_archive_type = _legacy_archive_type(row, msg)
    if int(record.get("ts") or 0) != int(msg.get("createTime") or 0):
        return False
    if archive_type != source_archive_type and not _legacy_type_is_compatible(archive_type, source_type):
        return False
    stable_source_sender = str(getattr(row, "sender_username", "") or msg.get("senderUsername") or "")
    sender_matches = str(record.get("sender_wxid") or "") == stable_source_sender
    return sender_matches or archive_type in {"system", "链接卡片"}


def _render_markdown(name: str, username: str, records: list[dict[str, Any]]) -> str:
    lines = [f"# {name} 聊天记录", ""]
    if records:
        lines.extend(
            [
                f"> 时间跨度: {records[0]['time']} ~ {records[-1]['time']}(共 {len(records)} 条)",
                f"> 来源: WeChatDataAnalysis 实时数据(会话 {username})",
                "",
            ]
        )
    current_date = ""
    for record in records:
        date_text, _, time_text = str(record.get("time") or "").partition(" ")
        if date_text != current_date:
            lines.append(f"## {date_text}")
            current_date = date_text
        content = str(record.get("content") or "").replace("\r\n", "\n")
        lines.append(f"### {time_text} {record.get('sender') or '未知'}> {content}")

    for kind, label in (("file", "文件"), ("image", "图片"), ("video", "视频"), ("voice", "语音")):
        items = [record for record in records if record.get("type") == kind]
        lines.extend(["", f"## {label}汇总({len(items)})"])
        archived = [record for record in items if record.get("archived")]
        if archived:
            lines.extend(["", "| # | 时间 | 发送者 | 文件 |", "|---|------|--------|------|"])
            for index, record in enumerate(archived, start=1):
                path = str(record["archived"])
                lines.append(f"| {index} | {record['time']} | {record['sender']} | [{Path(path).name}]({path}) |")
        missing = [record for record in items if not record.get("archived")]
        if missing:
            lines.extend(["", f"未找到本地源文件 {len(missing)} 个(稍后自动重试):"])
            lines.extend(f"- {record['time']} {record['sender']}: {record['content']}" for record in missing)
    return "\n".join(lines).rstrip() + "\n"


class WorkArchiveService:
    def __init__(self) -> None:
        self.store = WorkArchiveProfileStore()
        self._locks: dict[str, threading.Lock] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._event_seq = 0
        self._mu = threading.Lock()

    def _lock(self, profile_id: str) -> threading.Lock:
        with self._mu:
            return self._locks.setdefault(profile_id, threading.Lock())

    def _emit(self, profile_id: str, event: str, **payload: Any) -> None:
        with self._mu:
            self._event_seq += 1
            item = {"id": self._event_seq, "event": event, "profileId": profile_id, "at": _now(), **payload}
            events = self._events.setdefault(profile_id, [])
            events.append(item)
            del events[:-200]

    def events_after(self, profile_id: str, last_id: int = 0) -> list[dict[str, Any]]:
        with self._mu:
            return [dict(item) for item in self._events.get(profile_id, []) if int(item["id"]) > int(last_id)]

    def list_profiles(self) -> list[dict[str, Any]]:
        return [profile.to_dict() for profile in self.store.list()]

    def get_profile(self, profile_id: str) -> WorkArchiveProfile:
        return self.store.get(profile_id)

    def save_profile(self, payload: dict[str, Any], profile_id: str = "") -> dict[str, Any]:
        merged = dict(payload or {})
        if profile_id:
            try:
                current = self.store.get(profile_id).to_dict()
            except KeyError:
                current = {"id": profile_id}
            current.update(merged)
            merged = current
        profile = WorkArchiveProfile.from_dict(merged)
        return self.store.save(profile).to_dict()

    def unregister(self, profile_id: str) -> bool:
        self.cancel(profile_id)
        return self.store.delete(profile_id)

    def _account_realtime(self, profile: WorkArchiveProfile) -> tuple[Path, Any]:
        account_dir = _resolve_account_dir(profile.account)
        status = WCDB_REALTIME.get_status(account_dir)
        available = bool(status.get("dll_present") and status.get("key_present") and status.get("db_storage_dir"))
        if not available:
            raise RuntimeError("Realtime WCDB is not available; archive is paused until it recovers.")
        return account_dir, WCDB_REALTIME.ensure_connected(account_dir)

    def preflight_adoption(
        self,
        *,
        account: str,
        archive_root: str,
        plan_path: str = "",
        save_report: bool = True,
    ) -> dict[str, Any]:
        root = Path(str(archive_root or "")).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("archive_root must be an existing absolute directory.")
        source_plan = Path(plan_path).expanduser() if plan_path else root / "_archive_plan.json"
        plan = _read_json(source_plan, None)
        if not isinstance(plan, dict):
            raise ValueError("Archive plan is missing or invalid.")
        items = list(plan.get("work_groups") or []) + list(plan.get("work_singles") or [])
        if not items:
            raise ValueError("Archive plan contains no included conversations.")

        profile_probe = WorkArchiveProfile(id="adoption-check", name="Adoption check", account=account, archiveRoot=str(root))
        account_dir, realtime = self._account_realtime(profile_probe)
        details: list[dict[str, Any]] = []
        ok = True
        for item in items:
            username = str(item.get("username") or "").strip()
            display_name = str(item.get("display") or username).strip()
            expected_count = int(item.get("msg_count") or 0)
            conversation_dir = root / display_name
            json_path = conversation_dir / f"聊天记录_{display_name}.json"
            manifest_path = conversation_dir / "manifest.json"
            errors: list[str] = []
            records = _read_json(json_path, None)
            if not conversation_dir.is_dir():
                errors.append("conversation directory missing")
            if not isinstance(records, list):
                records = []
                errors.append("archive JSON missing or invalid")
            elif any(not isinstance(record, dict) or set(record) != set(ARCHIVE_FIELDS) for record in records):
                errors.append("archive JSON fields do not match the eight-field schema")
            manifest = _read_json(manifest_path, None)
            if not isinstance(manifest, list):
                errors.append("manifest missing or invalid")
                manifest = []
            missing_media = 0
            for entry in manifest:
                if not isinstance(entry, dict) or not entry.get("dest"):
                    continue
                target = Path(str(entry["dest"]))
                if not target.is_absolute():
                    target = conversation_dir / target
                if not target.exists():
                    missing_media += 1
            if missing_media:
                errors.append(f"manifest has {missing_media} missing media targets")
            source_rows: list[Any] = []
            try:
                source_rows = list(
                    chat_export._iter_rows_for_conversation(
                        account_dir=account_dir,
                        conv_username=username,
                        start_time=None,
                        end_time=None,
                        source="realtime",
                        rt_conn=realtime,
                    )
                )
            except Exception as exc:
                errors.append(f"realtime count failed: {exc}")
            source_count = (
                len(source_rows)
                if not any(error.startswith("realtime count failed:") for error in errors)
                else -1
            )
            archive_count = len(records)
            pending_count = 0
            if source_count >= 0:
                if source_count < archive_count:
                    errors.append(f"source={source_count} is behind archive={archive_count}")
                else:
                    mismatch_index = next(
                        (
                            index
                            for index, (record, row) in enumerate(zip(records, source_rows), start=1)
                            if not _archive_record_matches_source_row(record, row, username)
                        ),
                        0,
                    )
                    if mismatch_index:
                        errors.append(f"archive is not a realtime prefix at message {mismatch_index}")
                    else:
                        pending_count = source_count - archive_count
            details.append(
                {
                    "username": username,
                    "displayName": display_name,
                    "planCount": expected_count,
                    "archiveCount": archive_count,
                    "sourceCount": source_count,
                    "pendingCount": pending_count,
                    "manifestCount": len(manifest),
                    "missingMedia": missing_media,
                    "errors": errors,
                }
            )
            ok = ok and not errors
        report = {
            "status": "success" if ok else "blocked",
            "ok": ok,
            "account": account_dir.name,
            "archiveRoot": str(root),
            "conversationCount": len(details),
            "messageCount": sum(max(0, int(item["archiveCount"])) for item in details),
            "pendingMessageCount": sum(max(0, int(item["pendingCount"])) for item in details),
            "details": details,
        }
        if save_report:
            _atomic_write_json(root / ".work-archive" / "adoption-diff.json", report)
        return report

    def adopt(
        self,
        *,
        profile_id: str,
        name: str,
        account: str,
        archive_root: str,
        plan_path: str = "",
    ) -> dict[str, Any]:
        report = self.preflight_adoption(
            account=account, archive_root=archive_root, plan_path=plan_path, save_report=True
        )
        if not report["ok"]:
            raise ValueError("Archive adoption is blocked by preflight differences.")
        root = Path(archive_root)
        source_plan = Path(plan_path).expanduser() if plan_path else root / "_archive_plan.json"
        plan = _read_json(source_plan, {})
        items = list(plan.get("work_groups") or []) + list(plan.get("work_singles") or [])
        excluded_items = list(plan.get("borderline") or []) + list(plan.get("excluded") or [])
        conversations = {
            str(item["username"]): {
                "displayName": str(item.get("display") or item["username"]),
                "archiveDir": str(item.get("display") or item["username"]),
                "isGroup": bool(item.get("is_group")),
            }
            for item in items
        }
        profile = WorkArchiveProfile(
            id=profile_id,
            name=name,
            account=account,
            archiveRoot=str(root),
            enabled=False,
            includedUsernames=list(conversations),
            excludedUsernames=[str(item.get("username") or "").strip() for item in excluded_items],
            conversations=conversations,
            adoptedAt=_now(),
        )
        profile.validate()
        account_dir, realtime = self._account_realtime(profile)
        state = WorkArchiveState(profile)
        archive_counts = {
            str(item.get("username") or ""): int(item.get("archiveCount") or 0)
            for item in report.get("details") or []
        }
        with state.connect() as conn:
            for index, username in enumerate(profile.includedUsernames, start=1):
                rows = list(
                    chat_export._iter_rows_for_conversation(
                        account_dir=account_dir,
                        conv_username=username,
                        start_time=None,
                        end_time=None,
                        source="realtime",
                        rt_conn=realtime,
                    )
                )
                archived_rows = rows[: archive_counts.get(username, 0)]
                state.remember_rows(conn, username, archived_rows[-100:])
                self._emit(profile.id, "adoption_progress", current=index, total=len(profile.includedUsernames))
        self.store.save(profile)
        self._write_status_report(profile, {"state": "disabled", "message": "接管完成，等待用户开启自动归档。"})
        self._emit(profile.id, "adopted", conversations=len(profile.includedUsernames))
        return {"status": "success", "profile": profile.to_dict(), "preflight": report}

    def set_conversation_status(self, profile_id: str, username: str, status: str) -> dict[str, Any]:
        profile = self.store.get(profile_id)
        username = str(username or "").strip()
        if not username:
            raise ValueError("username is required.")
        if status not in {"included", "excluded", "pending"}:
            raise ValueError("status must be included, excluded, or pending.")
        for values in (profile.includedUsernames, profile.pendingUsernames, profile.excludedUsernames):
            while username in values:
                values.remove(username)
        getattr(profile, f"{status}Usernames").append(username)
        if status == "included" and username not in profile.conversations:
            meta = profile.pendingMeta.get(username) or {}
            display_name = str(meta.get("displayName") or username)
            profile.conversations[username] = {
                "displayName": display_name,
                "archiveDir": _safe_component(display_name),
                "isGroup": bool(meta.get("isGroup") or username.endswith("@chatroom")),
            }
        if status != "pending":
            profile.pendingMeta.pop(username, None)
        self.store.save(profile)
        self._emit(profile.id, "conversation_status", username=username, status=status)
        return profile.to_dict()

    def discover_pending(self, profile: WorkArchiveProfile, account_dir: Path) -> int:
        preview = chat_export.build_chat_export_targets_preview(
            account_dir=account_dir,
            source="realtime",
            include_hidden=False,
            include_official=False,
        )
        if str(preview.get("source") or "") != "realtime":
            raise RuntimeError("Realtime WCDB became unavailable; refusing snapshot-based discovery.")
        known = set(profile.includedUsernames) | set(profile.pendingUsernames) | set(profile.excludedUsernames)
        added = 0
        changed = False
        for target in preview.get("targets") or []:
            username = str(target.get("username") or "").strip()
            if not username:
                continue
            if username in profile.includedUsernames:
                meta = profile.conversations.setdefault(username, {})
                display_name = str(target.get("displayName") or target.get("name") or username)
                if display_name and meta.get("displayName") != display_name:
                    meta["displayName"] = display_name
                    changed = True
                continue
            if username in known:
                continue
            profile.pendingUsernames.append(username)
            profile.pendingMeta[username] = {
                "displayName": str(target.get("displayName") or target.get("name") or username),
                "isGroup": bool(target.get("isGroup")),
            }
            known.add(username)
            added += 1
        if added or changed:
            self.store.save(profile)
        if added:
            self._emit(profile.id, "pending_discovered", count=added)
        return added

    def preview_sync(self, profile_id: str) -> dict[str, Any]:
        profile = self.store.get(profile_id)
        account_dir, realtime = self._account_realtime(profile)
        state = WorkArchiveState(profile)
        details = []
        with state.connect() as conn:
            for username in profile.includedUsernames:
                cursor = state.cursor(conn, username)
                rows = list(
                    chat_export._iter_rows_for_conversation(
                        account_dir=account_dir,
                        conv_username=username,
                        start_time=max(0, cursor[0] - 5) if cursor[0] else None,
                        end_time=None,
                        source="realtime",
                        rt_conn=realtime,
                    )
                )
                count = sum(1 for row in rows if not state.has_key(conn, username, message_identity(row)))
                details.append({"username": username, "newMessages": count})
        return {"status": "success", "profileId": profile.id, "newMessages": sum(item["newMessages"] for item in details), "details": details}

    def run_sync(self, profile_id: str, *, reason: str = "manual") -> dict[str, Any]:
        profile = self.store.get(profile_id)
        lock = self._lock(profile.id)
        if not lock.acquire(blocking=False):
            return {"status": "running", "profileId": profile.id}
        cancel = threading.Event()
        self._cancel[profile.id] = cancel
        run_id = uuid.uuid4().hex
        started_at = _now()
        result: dict[str, Any] = {"status": "success", "profileId": profile.id, "runId": run_id, "added": 0}
        self._status[profile.id] = {**result, "state": "running", "reason": reason, "startedAt": started_at}
        self._emit(profile.id, "sync_started", runId=run_id, reason=reason)
        state = WorkArchiveState(profile)
        try:
            account_dir, realtime = self._account_realtime(profile)
            pending_added = self.discover_pending(profile, account_dir)
            display_names = {
                username: str(meta.get("displayName") or username)
                for username, meta in {**profile.pendingMeta, **profile.conversations}.items()
            }
            with state.connect() as conn:
                conn.execute(
                    "INSERT INTO runs(run_id, started_at, status) VALUES(?,?,?)", (run_id, started_at, "running")
                )
                def checkpoint() -> None:
                    if cancel.is_set():
                        raise InterruptedError("Archive sync cancelled.")

                for index, username in enumerate(profile.includedUsernames, start=1):
                    checkpoint()
                    cursor = state.cursor(conn, username)
                    rows = list(
                        chat_export._iter_rows_for_conversation(
                            account_dir=account_dir,
                            conv_username=username,
                            start_time=max(0, cursor[0] - 5) if cursor[0] else None,
                            end_time=None,
                            source="realtime",
                            rt_conn=realtime,
                            checkpoint=checkpoint,
                        )
                    )
                    new_rows = [row for row in rows if not state.has_key(conn, username, message_identity(row))]
                    if new_rows:
                        sender_usernames = {
                            str(getattr(row, "sender_username", "") or "").strip()
                            for row in new_rows
                            if str(getattr(row, "sender_username", "") or "").strip()
                        }
                        if sender_usernames:
                            try:
                                with realtime.lock:
                                    display_names.update(
                                        chat_export._wcdb_get_display_names(realtime.handle, list(sender_usernames))
                                    )
                            except Exception:
                                pass
                        added = self._append_conversation(
                            profile=profile,
                            account_dir=account_dir,
                            username=username,
                            rows=new_rows,
                            display_names=display_names,
                            state=state,
                            conn=conn,
                            cancel=cancel,
                        )
                        result["added"] += added
                    state.remember_rows(conn, username, rows[-100:])
                    conn.commit()
                    self._emit(
                        profile.id,
                        "sync_progress",
                        current=index,
                        total=len(profile.includedUsernames),
                        username=username,
                        added=result["added"],
                    )
                result["mediaResolved"] = self._retry_pending_media(
                    profile=profile,
                    account_dir=account_dir,
                    state=state,
                    conn=conn,
                    display_names=display_names,
                )
                conn.execute(
                    "UPDATE runs SET finished_at=?, status=?, added=? WHERE run_id=?",
                    (_now(), "success", int(result["added"]), run_id),
                )
            result.update({"pendingDiscovered": pending_added, "finishedAt": _now()})
            self._status[profile.id] = {**result, "state": "idle", "lastArchiveAt": result["finishedAt"]}
            self._write_status_report(profile, self._status[profile.id])
            self._emit(profile.id, "sync_finished", **result)
            return result
        except InterruptedError as exc:
            result.update({"status": "cancelled", "message": str(exc), "finishedAt": _now()})
            self._status[profile.id] = {**result, "state": "idle"}
            self._emit(profile.id, "sync_cancelled", runId=run_id)
            return result
        except Exception as exc:
            logger.exception("[work-archive] sync failed profile=%s", profile.id)
            result.update({"status": "error", "message": str(exc), "finishedAt": _now()})
            self._status[profile.id] = {**result, "state": "waiting_realtime" if "Realtime WCDB" in str(exc) else "error"}
            try:
                with state.connect() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO runs(run_id, started_at, finished_at, status, added, error) VALUES(?,?,?,?,?,?)",
                        (run_id, started_at, _now(), "error", int(result["added"]), str(exc)),
                    )
            except Exception:
                pass
            self._write_status_report(profile, self._status[profile.id])
            self._emit(profile.id, "sync_error", runId=run_id, message=str(exc))
            return result
        finally:
            self._cancel.pop(profile.id, None)
            lock.release()

    def _append_conversation(
        self,
        *,
        profile: WorkArchiveProfile,
        account_dir: Path,
        username: str,
        rows: list[Any],
        display_names: dict[str, str],
        state: WorkArchiveState,
        conn: sqlite3.Connection,
        cancel: threading.Event | None = None,
    ) -> int:
        meta = profile.conversations.get(username) or {}
        display_name = str(meta.get("displayName") or username)
        archive_dir_name = str(meta.get("archiveDir") or _safe_component(display_name))
        conversation_dir = Path(profile.archiveRoot) / archive_dir_name
        conversation_dir.mkdir(parents=True, exist_ok=True)
        json_path = conversation_dir / f"聊天记录_{archive_dir_name}.json"
        md_path = conversation_dir / f"聊天记录_{archive_dir_name}.md"
        manifest_path = conversation_dir / "manifest.json"
        records = _read_json(json_path, [])
        if not isinstance(records, list):
            raise ValueError(f"Invalid archive JSON: {json_path.name}")
        manifest = _read_json(manifest_path, [])
        if not isinstance(manifest, list):
            raise ValueError(f"Invalid manifest: {manifest_path.name}")
        new_records: list[dict[str, Any]] = []
        for row in rows:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Archive sync cancelled.")
            msg = chat_export._parse_message_for_export(
                row=row,
                conv_username=username,
                is_group=bool(username.endswith("@chatroom")),
                resource_conn=None,
                resource_chat_id=None,
                resolve_display_name=lambda value: display_names.get(value, value),
            )
            sender_username = str(msg.get("senderUsername") or "")
            msg["senderDisplayName"] = display_names.get(sender_username, sender_username)
            msg["_archiveType"] = _legacy_archive_type(row, msg)
            archived = _materialize_local_media(account_dir, username, conversation_dir, msg)
            record = _record_from_message(msg, display_names, archived)
            new_records.append(record)
            message_key = message_identity(row)
            if archived:
                manifest.append(
                    {
                        "message_key": message_key,
                        "time": record["time"],
                        "sender": record["sender"],
                        "category": archived.split("/", 1)[0],
                        "dest": archived,
                    }
                )
            elif str(msg.get("renderType") or "") in {"image", "emoji", "video", "voice", "file"}:
                state.queue_media(
                    conn,
                    username,
                    message_key,
                    {"recordIndex": len(records) + len(new_records) - 1, "message": msg},
                )
        combined = records + new_records
        _atomic_write_json(json_path, combined)
        _atomic_write_text(md_path, _render_markdown(display_name, username, combined))
        _atomic_write_json(manifest_path, manifest)
        state.remember_rows(conn, username, rows)
        return len(new_records)

    def _retry_pending_media(
        self,
        *,
        profile: WorkArchiveProfile,
        account_dir: Path,
        state: WorkArchiveState,
        conn: sqlite3.Connection,
        display_names: dict[str, str],
    ) -> int:
        resolved = 0
        rows = conn.execute(
            "SELECT * FROM pending_media WHERE next_attempt_at <= ? ORDER BY next_attempt_at, id", (_now(),)
        ).fetchall()
        for pending in rows:
            username = str(pending["username"] or "")
            try:
                payload = json.loads(str(pending["payload_json"] or "{}"))
                msg = payload.get("message") if isinstance(payload, dict) else None
                record_index = int(payload.get("recordIndex"))
                if not isinstance(msg, dict):
                    raise ValueError("pending media payload has no message")
                meta = profile.conversations.get(username) or {}
                display_name = str(meta.get("displayName") or username)
                archive_dir = str(meta.get("archiveDir") or _safe_component(display_name))
                conversation_dir = Path(profile.archiveRoot) / archive_dir
                json_path = conversation_dir / f"聊天记录_{archive_dir}.json"
                md_path = conversation_dir / f"聊天记录_{archive_dir}.md"
                manifest_path = conversation_dir / "manifest.json"
                archived = _materialize_local_media(account_dir, username, conversation_dir, msg)
                if not archived:
                    raise FileNotFoundError("local media is not available yet")
                records = _read_json(json_path, [])
                if not isinstance(records, list) or not (0 <= record_index < len(records)):
                    raise ValueError("pending media record index is no longer valid")
                records[record_index] = _record_from_message(msg, display_names, archived)
                manifest = _read_json(manifest_path, [])
                if not isinstance(manifest, list):
                    manifest = []
                manifest.append(
                    {
                        "message_key": str(pending["message_key"] or ""),
                        "time": records[record_index]["time"],
                        "sender": records[record_index]["sender"],
                        "category": archived.split("/", 1)[0],
                        "dest": archived,
                    }
                )
                _atomic_write_json(json_path, records)
                _atomic_write_text(md_path, _render_markdown(display_name, username, records))
                _atomic_write_json(manifest_path, manifest)
                conn.execute("DELETE FROM pending_media WHERE id = ?", (int(pending["id"]),))
                resolved += 1
            except Exception as exc:
                attempts = int(pending["attempts"] or 0) + 1
                delay = MEDIA_RETRY_SECONDS[attempts] if attempts < len(MEDIA_RETRY_SECONDS) else 21600
                conn.execute(
                    "UPDATE pending_media SET attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
                    (attempts, _now() + delay, str(exc), int(pending["id"])),
                )
        return resolved

    def cancel(self, profile_id: str) -> bool:
        event = self._cancel.get(str(profile_id or "").strip())
        if event is None:
            return False
        event.set()
        return True

    def status(self, profile_id: str) -> dict[str, Any]:
        profile = self.store.get(profile_id)
        current = dict(self._status.get(profile.id) or {})
        state = WorkArchiveState(profile)
        pending_media = 0
        last_run = None
        try:
            with state.connect() as conn:
                pending_media = int(conn.execute("SELECT COUNT(*) FROM pending_media").fetchone()[0])
                row = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
                last_run = dict(row) if row else None
        except Exception:
            pass
        return {
            "status": "success",
            "profileId": profile.id,
            "enabled": profile.enabled,
            "state": current.get("state") or ("idle" if profile.enabled else "disabled"),
            "included": len(profile.includedUsernames),
            "pending": len(profile.pendingUsernames),
            "pendingMedia": pending_media,
            "lastRun": last_run,
            **current,
        }

    def pending_media_due(self, profile: WorkArchiveProfile, now: int | None = None) -> bool:
        state = WorkArchiveState(profile)
        try:
            with state.connect() as conn:
                row = conn.execute("SELECT MIN(next_attempt_at) FROM pending_media").fetchone()
            return bool(row and row[0] is not None and int(row[0]) <= int(now or _now()))
        except Exception:
            return False

    def list_pending(self, profile_id: str) -> dict[str, Any]:
        profile = self.store.get(profile_id)
        return {
            "status": "success",
            "profileId": profile.id,
            "items": [
                {"username": username, **(profile.pendingMeta.get(username) or {})}
                for username in profile.pendingUsernames
            ],
        }

    def verify(self, profile_id: str) -> dict[str, Any]:
        profile = self.store.get(profile_id)
        issues = []
        total = 0
        for username in profile.includedUsernames:
            meta = profile.conversations.get(username) or {}
            archive_dir = str(meta.get("archiveDir") or _safe_component(str(meta.get("displayName") or username)))
            path = Path(profile.archiveRoot) / archive_dir / f"聊天记录_{archive_dir}.json"
            records = _read_json(path, None)
            if not isinstance(records, list):
                issues.append({"username": username, "error": "archive JSON missing or invalid"})
                continue
            invalid = sum(1 for record in records if not isinstance(record, dict) or set(record) != set(ARCHIVE_FIELDS))
            if invalid:
                issues.append({"username": username, "error": f"{invalid} records violate the eight-field schema"})
            total += len(records)
        return {"status": "success" if not issues else "error", "profileId": profile.id, "conversations": len(profile.includedUsernames), "messages": total, "issues": issues}

    def _write_status_report(self, profile: WorkArchiveProfile, status: dict[str, Any]) -> None:
        lines = [
            "# 工作会话归档状态",
            "",
            f"> 更新时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"- Profile: {profile.name}",
            f"- 状态: {status.get('state') or status.get('status') or 'unknown'}",
            f"- 自动归档: {'开启' if profile.enabled else '关闭'}",
            f"- 已纳入会话: {len(profile.includedUsernames)}",
            f"- 待确认会话: {len(profile.pendingUsernames)}",
            f"- 本次新增: {int(status.get('added') or 0)}",
        ]
        if status.get("message"):
            lines.append(f"- 信息: {status['message']}")
        _atomic_write_text(Path(profile.archiveRoot) / "工作会话归档状态.md", "\n".join(lines) + "\n")


@dataclass
class _MonitorProfileState:
    last_mtime_ns: int = 0
    first_change_at: float = 0.0
    due_at: float = 0.0
    last_reconcile_at: float = 0.0
    worker: Optional[threading.Thread] = None


class WorkArchiveMonitor:
    def __init__(self, service: WorkArchiveService):
        self.service = service
        self._states: dict[str, _MonitorProfileState] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mu = threading.Lock()

    def start(self) -> None:
        with self._mu:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="work-archive-monitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread:
            thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(10.0):
            try:
                self.tick()
            except Exception:
                logger.exception("[work-archive] monitor tick failed")

    def tick(self) -> None:
        now = time.time()
        profiles = self.service.store.list()
        enabled_ids = {profile.id for profile in profiles if profile.enabled}
        for profile_id in list(self._states):
            if profile_id not in enabled_ids:
                self._states.pop(profile_id, None)
        for profile in profiles:
            if not profile.enabled:
                continue
            state = self._states.setdefault(profile.id, _MonitorProfileState())
            if state.worker and not state.worker.is_alive():
                state.worker = None
            try:
                account_dir = _resolve_account_dir(profile.account)
                realtime_status = WCDB_REALTIME.get_status(account_dir)
                storage = Path(str(realtime_status.get("db_storage_dir") or ""))
                available = bool(realtime_status.get("dll_present") and realtime_status.get("key_present") and storage.is_dir())
                if not available:
                    self.service._status[profile.id] = {"state": "waiting_realtime", "lastDetectedAt": _now()}
                    continue
                mtime_ns = scan_db_storage_mtime_ns(storage)
            except Exception as exc:
                self.service._status[profile.id] = {"state": "waiting_realtime", "message": str(exc)}
                continue
            if mtime_ns and mtime_ns != state.last_mtime_ns:
                state.last_mtime_ns = mtime_ns
                state.first_change_at = state.first_change_at or now
                state.due_at = min(now + profile.quietSeconds, state.first_change_at + profile.maxDelaySeconds)
                self.service._status[profile.id] = {"state": "debouncing", "lastDetectedAt": _now()}
            reconcile_due = now - state.last_reconcile_at >= profile.reconcileSeconds
            media_due = self.service.pending_media_due(profile, int(now))
            should_run = (state.due_at and now >= state.due_at) or reconcile_due or media_due
            if should_run and state.worker is None:
                if media_due and not state.due_at and not reconcile_due:
                    reason = "media_retry"
                else:
                    reason = "reconcile" if reconcile_due and not state.due_at else "change"
                state.due_at = 0
                state.first_change_at = 0
                state.last_reconcile_at = now
                state.worker = threading.Thread(
                    target=self.service.run_sync,
                    kwargs={"profile_id": profile.id, "reason": reason},
                    name=f"work-archive-{profile.id}",
                    daemon=True,
                )
                state.worker.start()


WORK_ARCHIVE_SERVICE = WorkArchiveService()
WORK_ARCHIVE_MONITOR = WorkArchiveMonitor(WORK_ARCHIVE_SERVICE)
