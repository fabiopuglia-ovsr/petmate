from __future__ import annotations

import json

from .conftest import DEVICE_ID, auth, register_user


def _wait_push(push, n):
    # BackgroundTasks run before TestClient returns the response, so this is immediate.
    assert len(push.sent) == n, push.sent


def test_events_store_and_push(client, owner, push):
    ut, dt = owner
    client.post("/v1/push-tokens", json={"token": "fcm-1", "platform": "android"}, headers=auth(ut))
    base = f"/v1/devices/{DEVICE_ID}/events"

    r = client.post(base, json={"t": "meal_dispensed", "ts": 1760000000.0, "grams": 25}, headers=auth(dt))
    assert r.status_code == 201
    _wait_push(push, 1)
    tokens, note = push.sent[0]
    assert tokens == [("fcm-1", "android")]
    assert note.body == "Pappa servita: 25 g"
    assert note.data["event"] == "meal_dispensed" and note.data["device_id"] == DEVICE_ID

    client.post(base, json={"t": "hopper_low", "ts": 1760000001.0, "grams": 40}, headers=auth(dt))
    client.post(base, json={"t": "battery_low", "ts": 1760000002.0, "soc": 15}, headers=auth(dt))
    client.post(base, json={"t": "feeder_jam", "ts": 1760000003.0}, headers=auth(dt))
    _wait_push(push, 4)
    assert [n.body for _, n in push.sent[1:]] == [
        "Croccantini in esaurimento: restano 40 g", "Batteria scarica: 15%", "Dispenser inceppato: controlla la coclea"]

    # pet_detected is off by default (§5 example) → no push
    client.post(base, json={"t": "pet_detected", "ts": 1760000004.0, "camera": "front", "cls": "cat"}, headers=auth(dt))
    _wait_push(push, 4)

    events = client.get(base + "?limit=50", headers=auth(ut)).json()
    assert [e["t"] for e in events] == ["pet_detected", "feeder_jam", "battery_low", "hopper_low", "meal_dispensed"]
    assert events[-1]["grams"] == 25 and events[-1]["ts"] == "2025-10-09T08:53:20Z"
    assert len(client.get(base + "?limit=2", headers=auth(ut)).json()) == 2


def test_push_respects_notification_flags(client, owner, push):
    ut, dt = owner
    client.post("/v1/push-tokens", json={"token": "fcm-1", "platform": "ios"}, headers=auth(ut))
    cfg = {"version": 1, "pet": {"name": "Micio"},
           "notifications": {"meals": False, "activity": True, "battery": True, "pet_detected": True}}
    client.put(f"/v1/devices/{DEVICE_ID}/config", json=cfg, headers=auth(ut))
    base = f"/v1/devices/{DEVICE_ID}/events"
    client.post(base, json={"t": "meal_dispensed", "grams": 25}, headers=auth(dt))
    _wait_push(push, 0)
    client.post(base, json={"t": "pet_detected", "camera": "rear", "cls": "cat"}, headers=auth(dt))
    _wait_push(push, 1)
    assert push.sent[0][1].body == "Ho visto Micio (camera posteriore)"


def test_no_push_without_tokens(client, owner, push):
    _, dt = owner
    client.post(f"/v1/devices/{DEVICE_ID}/events", json={"t": "meal_dispensed", "grams": 25}, headers=auth(dt))
    _wait_push(push, 0)


def test_clips_upload_list_download(client, owner):
    ut, dt = owner
    data = b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000
    meta = {"ts": 1760000100.0, "duration_s": 15, "camera": "front"}
    r = client.post(
        f"/v1/devices/{DEVICE_ID}/clips",
        files={"file": ("clip.mp4", data, "video/mp4")},
        data={"meta": json.dumps(meta)},
        headers=auth(dt),
    )
    assert r.status_code == 201, r.text
    clip_id = r.json()["clip_id"]

    clips = client.get(f"/v1/devices/{DEVICE_ID}/clips", headers=auth(ut)).json()
    assert len(clips) == 1
    c = clips[0]
    assert set(c) == {"clip_id", "ts", "duration_s", "thumb_url", "url"}
    assert c["clip_id"] == clip_id and c["ts"] == "2025-10-09T08:55:00Z" and c["duration_s"] == 15
    assert c["url"] == f"/v1/clips/{clip_id}"

    r = client.get(c["url"], headers=auth(ut))
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.content == data

    bob = register_user(client, "bob@example.com")
    assert client.get(c["url"], headers=auth(bob)).status_code == 403
    assert client.get(c["url"]).status_code == 401
    assert client.get("/v1/clips/does-not-exist", headers=auth(ut)).status_code == 404
    # users can't upload clips
    r = client.post(f"/v1/devices/{DEVICE_ID}/clips", files={"file": ("c.mp4", b"x", "video/mp4")}, headers=auth(ut))
    assert r.status_code == 403


def test_clip_with_thumb_and_bad_meta(client, owner):
    ut, dt = owner
    r = client.post(
        f"/v1/devices/{DEVICE_ID}/clips",
        files={"file": ("clip.mp4", b"abc", "video/mp4"), "thumb": ("t.jpg", b"\xff\xd8jpg", "image/jpeg")},
        data={"meta": "{}"},
        headers=auth(dt),
    )
    assert r.status_code == 201
    c = client.get(f"/v1/devices/{DEVICE_ID}/clips", headers=auth(ut)).json()[0]
    assert c["thumb_url"].endswith("/thumb")
    assert client.get(c["thumb_url"], headers=auth(ut)).content == b"\xff\xd8jpg"

    r = client.post(f"/v1/devices/{DEVICE_ID}/clips", files={"file": ("c.mp4", b"x", "video/mp4")},
                    data={"meta": "not json"}, headers=auth(dt))
    assert r.status_code == 422


def test_clip_too_large(client, owner, app):
    _, dt = owner
    app.state.settings.max_clip_bytes = 10
    r = client.post(f"/v1/devices/{DEVICE_ID}/clips", files={"file": ("c.mp4", b"x" * 100, "video/mp4")},
                    headers=auth(dt))
    assert r.status_code == 413
