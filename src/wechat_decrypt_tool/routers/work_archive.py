from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..work_archive import WORK_ARCHIVE_SERVICE

router = APIRouter(prefix="/api/work-archive", tags=["work-archive"])


class ProfilePayload(BaseModel):
    id: str
    name: str
    account: str
    archiveRoot: str
    enabled: bool = False
    includedUsernames: list[str] = Field(default_factory=list)
    pendingUsernames: list[str] = Field(default_factory=list)
    excludedUsernames: list[str] = Field(default_factory=list)
    conversations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pendingMeta: dict[str, dict[str, Any]] = Field(default_factory=dict)
    mediaPolicy: str = "local_only"


class ProfileUpdatePayload(BaseModel):
    name: Optional[str] = None
    account: Optional[str] = None
    archiveRoot: Optional[str] = None
    enabled: Optional[bool] = None
    includedUsernames: Optional[list[str]] = None
    pendingUsernames: Optional[list[str]] = None
    excludedUsernames: Optional[list[str]] = None
    conversations: Optional[dict[str, dict[str, Any]]] = None
    pendingMeta: Optional[dict[str, dict[str, Any]]] = None
    mediaPolicy: Optional[str] = None


class AdoptionPreflightPayload(BaseModel):
    account: str
    archiveRoot: str
    planPath: str = ""


class AdoptionPayload(AdoptionPreflightPayload):
    id: str = "work-chats"
    name: str = "工作聊天"


class ConversationStatusPayload(BaseModel):
    username: str
    status: Literal["included", "excluded", "pending"]


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'"))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/profiles")
def list_profiles():
    return {"status": "success", "profiles": WORK_ARCHIVE_SERVICE.list_profiles()}


@router.post("/profiles")
def create_profile(payload: ProfilePayload):
    try:
        return {"status": "success", "profile": WORK_ARCHIVE_SERVICE.save_profile(payload.model_dump())}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.put("/profiles/{profile_id}")
def update_profile(profile_id: str, payload: ProfileUpdatePayload):
    try:
        values = payload.model_dump(exclude_none=True)
        return {"status": "success", "profile": WORK_ARCHIVE_SERVICE.save_profile(values, profile_id)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.delete("/profiles/{profile_id}")
def unregister_profile(profile_id: str):
    if not WORK_ARCHIVE_SERVICE.unregister(profile_id):
        raise HTTPException(status_code=404, detail="Archive profile not found.")
    return {"status": "success", "profileId": profile_id}


@router.post("/adoption/preflight")
def preflight_adoption(payload: AdoptionPreflightPayload):
    try:
        return WORK_ARCHIVE_SERVICE.preflight_adoption(
            account=payload.account,
            archive_root=payload.archiveRoot,
            plan_path=payload.planPath,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/adoption")
def adopt_archive(payload: AdoptionPayload):
    try:
        return WORK_ARCHIVE_SERVICE.adopt(
            profile_id=payload.id,
            name=payload.name,
            account=payload.account,
            archive_root=payload.archiveRoot,
            plan_path=payload.planPath,
        )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/profiles/{profile_id}/conversations/status")
def set_conversation_status(profile_id: str, payload: ConversationStatusPayload):
    try:
        profile = WORK_ARCHIVE_SERVICE.set_conversation_status(profile_id, payload.username, payload.status)
        return {"status": "success", "profile": profile}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/profiles/{profile_id}/preview")
def preview_sync(profile_id: str):
    try:
        return WORK_ARCHIVE_SERVICE.preview_sync(profile_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/profiles/{profile_id}/sync")
def run_sync(profile_id: str):
    try:
        WORK_ARCHIVE_SERVICE.get_profile(profile_id)
    except Exception as exc:
        raise _translate_error(exc) from exc
    thread = threading.Thread(
        target=WORK_ARCHIVE_SERVICE.run_sync,
        kwargs={"profile_id": profile_id, "reason": "manual"},
        name=f"work-archive-api-{profile_id}",
        daemon=True,
    )
    thread.start()
    return {"status": "accepted", "profileId": profile_id}


@router.post("/profiles/{profile_id}/cancel")
def cancel_sync(profile_id: str):
    try:
        WORK_ARCHIVE_SERVICE.get_profile(profile_id)
    except Exception as exc:
        raise _translate_error(exc) from exc
    return {"status": "success", "cancelRequested": WORK_ARCHIVE_SERVICE.cancel(profile_id)}


@router.get("/profiles/{profile_id}/status")
def get_status(profile_id: str):
    try:
        return WORK_ARCHIVE_SERVICE.status(profile_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/profiles/{profile_id}/pending")
def list_pending(profile_id: str):
    try:
        return WORK_ARCHIVE_SERVICE.list_pending(profile_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/profiles/{profile_id}/verify")
def verify(profile_id: str):
    try:
        return WORK_ARCHIVE_SERVICE.verify(profile_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/profiles/{profile_id}/events")
async def stream_events(request: Request, profile_id: str, last_event_id: int = 0):
    try:
        WORK_ARCHIVE_SERVICE.get_profile(profile_id)
    except Exception as exc:
        raise _translate_error(exc) from exc

    async def generate():
        cursor = max(0, int(last_event_id or request.headers.get("last-event-id") or 0))
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            events = WORK_ARCHIVE_SERVICE.events_after(profile_id, cursor)
            if not events:
                yield ": heartbeat\n\n"
            for item in events:
                cursor = int(item["id"])
                payload = json.dumps(item, ensure_ascii=False)
                yield f"id: {cursor}\nevent: {item['event']}\ndata: {payload}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream")
