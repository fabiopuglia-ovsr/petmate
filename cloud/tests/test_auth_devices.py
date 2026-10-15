from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import update

from app.models import ClaimToken

from .conftest import DEVICE_ID, DEVICE_SECRET, auth, claim_device, register_user


def test_health(client):
    assert client.get("/v1/health").json() == {"ok": True}


def test_register_login(client, settings):
    tok = register_user(client)
    claims = jwt.decode(tok, settings.jwt_secret, algorithms=["HS256"])
    assert claims["typ"] == "user"
    assert claims["exp"] - claims["iat"] == 24 * 3600
    # duplicate (case-insensitive)
    r = client.post("/v1/auth/register", json={"email": "ALICE@example.com", "password": "password123"})
    assert r.status_code == 409
    r = client.post("/v1/auth/login", json={"email": "alice@example.com", "password": "password123"})
    assert r.status_code == 200
    assert jwt.decode(r.json()["access_token"], settings.jwt_secret, algorithms=["HS256"])["sub"] == claims["sub"]
    r = client.post("/v1/auth/login", json={"email": "alice@example.com", "password": "wrong-pass"})
    assert r.status_code == 401
    r = client.post("/v1/auth/login", json={"email": "nobody@example.com", "password": "password123"})
    assert r.status_code == 401


def test_register_validation(client):
    assert client.post("/v1/auth/register", json={"email": "bad", "password": "password123"}).status_code == 422
    assert client.post("/v1/auth/register", json={"email": "a@b.it", "password": "short"}).status_code == 422


def test_auth_rate_limit(client, app):
    app.state.limiter.enabled = True
    app.state.settings.rate_auth = (3, 60.0)
    codes = [client.post("/v1/auth/login", json={"email": "x@y.it", "password": "password123"}).status_code
             for _ in range(5)]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429]


def test_claim_register_and_device_token(client, settings):
    ut = register_user(client)
    r = client.post("/v1/claim-tokens", headers=auth(ut))
    body = r.json()
    exp = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert timedelta(minutes=9) < exp - datetime.now(timezone.utc) <= timedelta(minutes=10)

    r = client.post("/v1/devices/register", json={
        "device_id": DEVICE_ID, "device_secret": DEVICE_SECRET, "claim_token": body["claim_token"]})
    assert r.status_code == 201, r.text
    out = r.json()
    uid = jwt.decode(ut, settings.jwt_secret, algorithms=["HS256"])["sub"]
    assert out["owner_user_id"] == int(uid)
    dclaims = jwt.decode(out["device_token"], settings.jwt_secret, algorithms=["HS256"])
    assert dclaims["typ"] == "device" and dclaims["sub"] == DEVICE_ID

    # single use: a different device can't reuse it
    r = client.post("/v1/devices/register", json={
        "device_id": "PM-000999", "device_secret": "other-secret", "claim_token": body["claim_token"]})
    assert r.status_code == 400

    # device token refresh
    r = client.post("/v1/devices/token", json={"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET})
    assert r.status_code == 200
    assert jwt.decode(r.json()["device_token"], settings.jwt_secret, algorithms=["HS256"])["typ"] == "device"
    r = client.post("/v1/devices/token", json={"device_id": DEVICE_ID, "device_secret": "nope"})
    assert r.status_code == 401

    devs = client.get("/v1/devices", headers=auth(ut)).json()
    assert len(devs) == 1
    assert set(devs[0]) == {"device_id", "name", "online", "last_seen", "fw"}
    assert devs[0]["device_id"] == DEVICE_ID and devs[0]["online"] is False


def test_register_bad_secret_and_unknown_device(client):
    ut = register_user(client)
    ct = client.post("/v1/claim-tokens", headers=auth(ut)).json()["claim_token"]
    r = client.post("/v1/devices/register", json={"device_id": DEVICE_ID, "device_secret": "wrong", "claim_token": ct})
    assert r.status_code == 401
    r = client.post("/v1/devices/register", json={"device_id": "PM-777777", "device_secret": "x", "claim_token": ct})
    assert r.status_code == 401
    # token was not consumed by failed attempts
    r = client.post("/v1/devices/register", json={"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET, "claim_token": ct})
    assert r.status_code == 201


def test_tofu(client, app):
    app.state.settings.allow_tofu = True
    ut = register_user(client)
    dt = claim_device(client, ut, device_id="PM-555555", secret="whatever")
    assert dt
    # now the secret is pinned
    r = client.post("/v1/devices/token", json={"device_id": "PM-555555", "device_secret": "other"})
    assert r.status_code == 401


def test_expired_claim_token(client, app):
    ut = register_user(client)
    ct = client.post("/v1/claim-tokens", headers=auth(ut)).json()["claim_token"]
    with app.state.db.session() as s:
        s.execute(update(ClaimToken).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        s.commit()
    r = client.post("/v1/devices/register", json={"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET, "claim_token": ct})
    assert r.status_code == 400 and "expired" in r.text


def test_ownership_checks(client, owner):
    ut, dt = owner
    bob = register_user(client, "bob@example.com")
    base = f"/v1/devices/{DEVICE_ID}"
    assert client.get("/v1/devices", headers=auth(bob)).json() == []
    assert client.patch(base, json={"name": "x"}, headers=auth(bob)).status_code == 403
    assert client.delete(base, headers=auth(bob)).status_code == 403
    assert client.get(base + "/config", headers=auth(bob)).status_code == 403
    assert client.put(base + "/config", json={"version": 1}, headers=auth(bob)).status_code == 403
    assert client.post(base + "/commands", json={"t": "feed", "grams": 10}, headers=auth(bob)).status_code == 403
    assert client.get(base + "/events", headers=auth(bob)).status_code == 403
    assert client.get(base + "/clips", headers=auth(bob)).status_code == 403
    # device token cannot use user endpoints, and cannot act on another device
    assert client.get("/v1/devices", headers=auth(dt)).status_code == 403
    assert client.post("/v1/devices/PM-000999/events", json={"t": "docked"}, headers=auth(dt)).status_code == 403
    # user token cannot post device events
    assert client.post(base + "/events", json={"t": "docked"}, headers=auth(ut)).status_code == 403
    # no token / garbage token
    assert client.get("/v1/devices").status_code == 401
    assert client.get("/v1/devices", headers=auth("garbage")).status_code == 401
    # unknown device
    assert client.get("/v1/devices/PM-404/config", headers=auth(ut)).status_code == 404


def test_expired_jwt_rejected(client, settings):
    tok = jwt.encode({"sub": "1", "typ": "user", "exp": int(time.time()) - 5}, settings.jwt_secret, algorithm="HS256")
    assert client.get("/v1/devices", headers=auth(tok)).status_code == 401


def test_patch_and_delete_device(client, owner):
    ut, dt = owner
    r = client.patch(f"/v1/devices/{DEVICE_ID}", json={"name": "Robottino"}, headers=auth(ut))
    assert r.status_code == 200 and r.json()["name"] == "Robottino"
    assert client.delete(f"/v1/devices/{DEVICE_ID}", headers=auth(ut)).status_code == 204
    assert client.get("/v1/devices", headers=auth(ut)).json() == []
    # unclaimed device can no longer obtain tokens until re-provisioned
    r = client.post("/v1/devices/token", json={"device_id": DEVICE_ID, "device_secret": DEVICE_SECRET})
    assert r.status_code == 403 and r.json()["detail"] == "device_unclaimed"
    # re-claim by another user works
    bob = register_user(client, "bob@example.com")
    claim_device(client, bob)
    assert [d["device_id"] for d in client.get("/v1/devices", headers=auth(bob)).json()] == [DEVICE_ID]


def test_config_get_put_reported(client, owner):
    ut, dt = owner
    base = f"/v1/devices/{DEVICE_ID}/config"
    assert client.get(base, headers=auth(ut)).status_code == 404
    cfg = {"version": 1, "pet": {"name": "Micio", "species": "cat", "weight_kg": 4.5},
           "notifications": {"meals": True, "activity": True, "battery": True, "pet_detected": False}}
    r = client.put(base, json=cfg, headers=auth(ut))
    assert r.status_code == 200 and r.json() == cfg
    assert r.headers["X-Petmate-Delivered"] == "false"
    assert client.get(base, headers=auth(ut)).json() == cfg
    assert client.get(base, headers=auth(dt)).json() == cfg
    assert client.post(base + "/reported", json=cfg, headers=auth(dt)).status_code == 204
    assert client.post(base + "/reported", json=cfg, headers=auth(ut)).status_code == 403
    assert client.get(base + "/reported", headers=auth(ut)).json() == cfg


def test_commands_offline_409(client, owner):
    ut, _ = owner
    r = client.post(f"/v1/devices/{DEVICE_ID}/commands", json={"t": "feed", "grams": 20}, headers=auth(ut))
    assert r.status_code == 409
    r = client.post(f"/v1/devices/{DEVICE_ID}/commands", json={"t": "bogus"}, headers=auth(ut))
    assert r.status_code == 422


def test_ice_servers(client, owner, settings):
    import base64
    import hashlib
    import hmac

    ut, dt = owner
    for tok, sub in ((ut, jwt.decode(ut, settings.jwt_secret, algorithms=["HS256"])["sub"]), (dt, DEVICE_ID)):
        r = client.get("/v1/ice-servers", headers=auth(tok))
        assert r.status_code == 200
        servers = r.json()["ice_servers"]
        assert servers[0] == {"urls": settings.stun_urls}
        turn = servers[1]
        assert turn["urls"] == settings.turn_urls
        expiry, _, s = turn["username"].partition(":")
        assert s == sub
        assert 3590 <= int(expiry) - time.time() <= 3600
        expected = base64.b64encode(hmac.new(settings.turn_secret.encode(), turn["username"].encode(), hashlib.sha1).digest()).decode()
        assert turn["credential"] == expected
    assert client.get("/v1/ice-servers").status_code == 401


def test_push_token_registration(client, owner):
    ut, _ = owner
    assert client.post("/v1/push-tokens", json={"token": "fcm-abc", "platform": "android"}, headers=auth(ut)).status_code == 204
    # idempotent
    assert client.post("/v1/push-tokens", json={"token": "fcm-abc", "platform": "android"}, headers=auth(ut)).status_code == 204
    assert client.post("/v1/push-tokens", json={"token": "x", "platform": "symbian"}, headers=auth(ut)).status_code == 422
