from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["PETMATE_NO_AUTOAPP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app.cli import add_device
from app.config import Settings
from app.main import create_app
from app.push import LoggingPushSender

DEVICE_ID = "PM-000123"
DEVICE_SECRET = "factory-secret-123"
TURN_SECRET = "turn-test-secret"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        jwt_secret="test-jwt-secret-that-is-long-enough-for-hs256",
        turn_secret=TURN_SECRET,
        stun_urls=["stun:turn.petmate.example:3478"],
        turn_urls=[
            "turn:turn.petmate.example:3478?transport=udp",
            "turn:turn.petmate.example:3478?transport=tcp",
            "turns:turn.petmate.example:5349?transport=tcp",
        ],
        storage_dir=str(tmp_path / "clips"),
        rate_limit_enabled=False,
    )


@pytest.fixture
def push() -> LoggingPushSender:
    return LoggingPushSender()


@pytest.fixture
def app(settings, push):
    a = create_app(settings, push=push)
    add_device(a.state.db, DEVICE_ID, DEVICE_SECRET)
    add_device(a.state.db, "PM-000999", "other-secret")
    return a


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register_user(client, email="alice@example.com", password="password123") -> str:
    r = client.post("/v1/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def claim_device(client, user_token: str, device_id=DEVICE_ID, secret=DEVICE_SECRET) -> str:
    r = client.post("/v1/claim-tokens", headers=auth(user_token))
    assert r.status_code == 201, r.text
    ct = r.json()["claim_token"]
    r = client.post("/v1/devices/register",
                    json={"device_id": device_id, "device_secret": secret, "claim_token": ct})
    assert r.status_code == 201, r.text
    return r.json()["device_token"]


@pytest.fixture
def owner(client):
    """(user_token, device_token) for a claimed device."""
    ut = register_user(client)
    dt = claim_device(client, ut)
    return ut, dt
