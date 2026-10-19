"""Pruebas básicas. No usan Internet: GitHub se simula con httpx.MockTransport.

Ejecutar:  pytest -q
"""

import copy
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config_service import Settings  # noqa: E402
from main import create_app  # noqa: E402
from models import compare_versions  # noqa: E402

BUNDLED = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ADMIN = {"X-Admin-Key": "clave-de-prueba"}


class FakeGitHub:
    """Hace de GitHub: devuelve lo que la prueba le diga."""

    def __init__(self):
        self.status = 200
        self.payload = copy.deepcopy(BUNDLED)
        self.raw = None
        self.fail = False
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("sin internet", request=request)
        if self.raw is not None:
            return httpx.Response(200, content=self.raw)
        return httpx.Response(self.status, json=self.payload, headers={"ETag": '"abc"'})


def make(tmp_path, gh=None, repo="usuario/fpsmatrix-config", token="", admin="clave-de-prueba"):
    gh = gh or FakeGitHub()
    settings = Settings(
        github_repo=repo,
        github_token=token,
        cache_dir=tmp_path / "cache",
        admin_api_key=admin,
        allowed_url_hosts=["github.com", "fpsmatrix.com"],
    )
    app = create_app(settings, transport=httpx.MockTransport(gh.handler), auto_refresh=False)
    return TestClient(app), gh


def newer(gh, version=2, **changes):
    gh.payload["config_version"] = version
    gh.payload.update(changes)


def test_health_and_bundled_config(tmp_path):
    client, _ = make(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["config_source"] == "bundled"
    assert body["config_version"] == 1


def test_public_config_hides_private_section(tmp_path):
    client, _ = make(tmp_path)
    r = client.get("/v1/config")
    assert r.status_code == 200
    assert "private" not in r.json()
    assert r.json()["parameters"]["fps.competitive_cap_hz"] == 360
    # ETag: si no cambió, 304
    r2 = client.get("/v1/config", headers={"If-None-Match": r.headers["ETag"]})
    assert r2.status_code == 304


def test_version_endpoint(tmp_path):
    client, _ = make(tmp_path)
    body = client.get("/v1/version", params={"app_version": "1.9.4"}).json()
    assert body["update_available"] is True and body["supported"] is True
    body = client.get("/v1/version", params={"app_version": "1.8.0"}).json()
    assert body["supported"] is False
    body = client.get("/v1/version", params={"app_version": "1.9.5"}).json()
    assert body["update_available"] is False
    r = client.get("/v1/version", params={"app_version": "uno"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


def test_compare_versions_with_test_builds():
    assert compare_versions("1.9.5-u", "1.9.5") == -1
    assert compare_versions("1.9.5", "1.9.4") == 1
    assert compare_versions("1.10.0", "1.9.9") == 1
    assert compare_versions("1.9.5", "1.9.5") == 0


def test_features_respect_version_premium_and_sensitive(tmp_path):
    client, _ = make(tmp_path)
    f = client.get("/v1/features", params={"app_version": "1.9.4"}).json()["features"]
    assert f["benchmark"]["enabled"] is True
    assert f["game_mode_priority"]["enabled"] is False  # pide 1.9.5
    assert f["advanced_reports"]["enabled"] is False  # premium
    f = client.get("/v1/features", params={"app_version": "1.9.5"}).json()["features"]
    assert f["game_mode_priority"]["enabled"] is True
    assert f["game_mode_priority"]["sensitive"] is True


def test_admin_requires_key(tmp_path):
    client, _ = make(tmp_path)
    assert client.get("/admin/config").status_code == 401
    assert client.get("/admin/config", headers={"X-Admin-Key": "mala"}).status_code == 401
    r = client.get("/admin/config", headers=ADMIN)
    assert r.status_code == 200 and "private" in r.json()


def test_admin_disabled_without_key(tmp_path):
    client, _ = make(tmp_path, admin="")
    assert client.get("/admin/config", headers=ADMIN).status_code == 404


def test_refresh_applies_and_caches(tmp_path):
    client, gh = make(tmp_path)
    newer(gh, 2)
    gh.payload["parameters"]["fps.competitive_cap_hz"] = 240
    r = client.post("/admin/refresh", headers=ADMIN)
    assert r.status_code == 200 and r.json()["updated"] is True
    assert client.get("/v1/config").json()["parameters"]["fps.competitive_cap_hz"] == 240
    assert (tmp_path / "cache" / "config.cache.json").exists()
    assert gh.requests[0].url.host == "raw.githubusercontent.com"
    assert "authorization" not in gh.requests[0].headers


def test_offline_restart_uses_cache(tmp_path):
    client, gh = make(tmp_path)
    newer(gh, 3)
    client.post("/admin/refresh", headers=ADMIN)
    # "reinicio" sin Internet
    gh2 = FakeGitHub()
    gh2.fail = True
    client2, _ = make(tmp_path, gh2)
    assert client2.get("/health").json()["config_source"] == "cache"
    assert client2.get("/v1/config").json()["config_version"] == 3
    r = client2.post("/admin/refresh", headers=ADMIN)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_unreachable"
    health = client2.get("/health").json()
    assert health["status"] == "degraded" and health["config_version"] == 3


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda p: p.update(features={"x": {"enabled": True, "run_script": "del C:\\"}}), "extra"),
        (lambda p: p["parameters"].update({"cmd": ["powershell", "-c", "..."]}), "parameters"),
        (lambda p: p["app"].update(download_url="http://github.com/x"), "https"),
        (lambda p: p["app"].update(download_url="https://evil.example.com/x.exe"), "dominio"),
        (lambda p: p["app"].update(min_supported_version="9.9.9"), "min_supported_version"),
        (lambda p: p.update(schema_version=99), "schema_version"),
    ],
)
def test_invalid_remote_config_is_rejected_and_last_valid_kept(tmp_path, mutate, expected):
    client, gh = make(tmp_path)
    newer(gh, 5)
    mutate(gh.payload)
    r = client.post("/admin/refresh", headers=ADMIN)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "config_rejected"
    assert client.get("/v1/config").json()["config_version"] == 1


def test_broken_json_is_rejected(tmp_path):
    gh = FakeGitHub()
    gh.raw = b"{ esto no es json"
    client, _ = make(tmp_path, gh)
    r = client.post("/admin/refresh", headers=ADMIN)
    assert r.status_code == 502 and r.json()["error"]["code"] == "github_bad_content"
    assert client.get("/health").json()["config_version"] == 1


def test_downgrade_is_rejected(tmp_path):
    client, gh = make(tmp_path)
    newer(gh, 4)
    client.post("/admin/refresh", headers=ADMIN)
    newer(gh, 2)
    r = client.post("/admin/refresh", headers=ADMIN)
    assert r.status_code == 422
    assert client.get("/v1/config").json()["config_version"] == 4


def test_kill_switch_turns_features_off(tmp_path):
    client, gh = make(tmp_path)
    newer(gh, 2, kill_switch={"enabled": True, "message": {"es": "Mantenimiento"}})
    client.post("/admin/refresh", headers=ADMIN)
    f = client.get("/v1/features", params={"app_version": "1.9.5"}).json()["features"]
    assert all(not x["enabled"] for x in f.values())


def test_private_repo_uses_token_only_server_side(tmp_path):
    client, gh = make(tmp_path, token="token-falso-de-prueba")
    newer(gh, 2)
    client.post("/admin/refresh", headers=ADMIN)
    req = gh.requests[0]
    assert req.url.host == "api.github.com"
    assert req.headers["authorization"] == "Bearer token-falso-de-prueba"
    # el token nunca aparece en respuestas públicas ni privadas
    for path in ("/health", "/v1/config", "/v1/version"):
        assert "token-falso" not in client.get(path).text
    assert "token-falso" not in client.get("/admin/config", headers=ADMIN).text


def test_without_repo_works_offline_with_bundled(tmp_path):
    client, _ = make(tmp_path, repo="")
    assert client.get("/health").json() == {
        **client.get("/health").json(),
        "status": "ok",
        "github_repo_configured": False,
    }
    r = client.post("/admin/refresh", headers=ADMIN)
    assert r.status_code == 503 and r.json()["error"]["code"] == "github_not_configured"
