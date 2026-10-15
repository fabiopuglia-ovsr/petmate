"""GET /v1/server-info and the static web test console (/console/)."""
from __future__ import annotations

import pytest

from app.config import DEFAULT_STUN, Settings
from app.main import create_app
from fastapi.testclient import TestClient

from .conftest import auth, register_user


def test_server_info_unauthenticated_lan_urls(client, monkeypatch):
    monkeypatch.setenv("LAN_HOSTS", "192.168.1.50")
    r = client.get("/v1/server-info")
    assert r.status_code == 200
    j = r.json()
    assert j["api_prefix"] == "/v1"
    assert j["lan_hosts"][0] == "192.168.1.50"
    # TestClient uses host "testserver" (not localhost) → the request origin comes first
    assert j["base_urls"][0] == "http://testserver/v1"
    assert "http://192.168.1.50:8000/v1" in j["base_urls"]
    assert all(u.startswith("http://") for u in j["base_urls"])
    assert "http://192.168.1.50:8000/console/" in j["console_urls"]


def test_server_info_localhost_request_not_proposed(app, monkeypatch):
    monkeypatch.setenv("LAN_HOSTS", "10.0.0.7")
    with TestClient(app, base_url="http://localhost:8000") as c:
        j = c.get("/v1/server-info").json()
    assert j["base_urls"][0] == "http://10.0.0.7:8000/v1"
    assert not any("localhost" in u or "127.0.0.1" in u for u in j["base_urls"])


def test_server_info_public_base_url_hides_lan(settings, push, monkeypatch):
    monkeypatch.setenv("LAN_HOSTS", "192.168.1.50")
    settings.public_base_url = "https://api.petmate.example"
    with TestClient(create_app(settings, push=push)) as c:
        j = c.get("/v1/server-info").json()
    assert j["base_urls"] == ["https://api.petmate.example/v1"]
    assert j["lan_hosts"] == []


def test_console_index_and_redirects(client):
    r = client.get("/console/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Console di test" in r.text
    assert '<script type="importmap">' in r.text
    assert r.headers.get("cache-control") == "no-cache"
    for path in ("/console", "/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/console/"


@pytest.mark.parametrize("path", [
    "js/app.js", "js/api.js", "js/signal.js", "js/ui.js", "js/wizard.js", "js/live.js", "js/prov-crypto.js",
    "vendor/noble-curves/ed25519.js", "vendor/noble-hashes/hkdf.js", "vendor/noble-hashes/sha2.js",
    "vendor/noble-ciphers/chacha.js",
])
def test_console_js_modules_mime(client, path):
    r = client.get(f"/console/{path}")
    assert r.status_code == 200, path
    assert r.headers["content-type"].split(";")[0] in ("text/javascript", "application/javascript")


def test_console_css_mime(client):
    r = client.get("/console/css/console.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")


def test_console_missing_file_404(client):
    assert client.get("/console/js/nope.js").status_code == 404


def test_api_routes_not_shadowed_by_console(client):
    assert client.get("/v1/health").json() == {"ok": True}
    assert client.get("/health").json() == {"ok": True}


def test_ice_servers_stun_only_without_turn(settings, push):
    settings.stun_urls = [DEFAULT_STUN]
    settings.turn_urls = []
    with TestClient(create_app(settings, push=push)) as c:
        tok = register_user(c)
        r = c.get("/v1/ice-servers", headers=auth(tok))
    assert r.status_code == 200
    assert r.json() == {"ice_servers": [{"urls": [DEFAULT_STUN]}]}


@pytest.mark.parametrize("env,expected", [
    ({}, [DEFAULT_STUN]),
    ({"STUN_URLS": ""}, [DEFAULT_STUN]),
    ({"STUN_URLS": "none"}, []),
    ({"STUN_URLS": "stun:a:3478,stun:b:3478"}, ["stun:a:3478", "stun:b:3478"]),
    ({"TURN_HOST": "turn.x"}, ["stun:turn.x:3478"]),
])
def test_stun_from_env(monkeypatch, env, expected):
    for k in ("STUN_URLS", "TURN_HOST", "TURN_URLS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert s.stun_urls == expected
    if "TURN_HOST" not in env:
        assert s.turn_urls == []
