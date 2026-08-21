from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wechat_decrypt_tool.routers import work_archive as router_module


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router_module.router)
    return TestClient(app)


def test_work_archive_routes_include_sse_and_lifecycle_endpoints():
    paths = {route.path for route in router_module.router.routes}
    assert "/api/work-archive/profiles" in paths
    assert "/api/work-archive/adoption/preflight" in paths
    assert "/api/work-archive/profiles/{profile_id}/sync" in paths
    assert "/api/work-archive/profiles/{profile_id}/cancel" in paths
    assert "/api/work-archive/profiles/{profile_id}/events" in paths


def test_profile_list_and_cancel_use_registered_profile_only(monkeypatch):
    service = router_module.WORK_ARCHIVE_SERVICE
    monkeypatch.setattr(service, "list_profiles", lambda: [{"id": "registered"}])
    monkeypatch.setattr(service, "get_profile", lambda profile_id: object() if profile_id == "registered" else (_ for _ in ()).throw(KeyError(profile_id)))
    monkeypatch.setattr(service, "cancel", lambda profile_id: profile_id == "registered")
    client = _client()

    response = client.get("/api/work-archive/profiles")
    assert response.status_code == 200
    assert response.json()["profiles"] == [{"id": "registered"}]

    response = client.post("/api/work-archive/profiles/registered/cancel")
    assert response.status_code == 200
    assert response.json()["cancelRequested"] is True

    response = client.post("/api/work-archive/profiles/not-registered/cancel")
    assert response.status_code == 404
