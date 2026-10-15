from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from .conftest import DEVICE_ID, auth, claim_device, register_user

DEVICE_PEER = f"device:{DEVICE_ID}"


def _connect(client, token):
    return client.websocket_connect(f"/v1/signal?token={token}")


def _recv_until(ws, mtype, limit=10):
    for _ in range(limit):
        m = ws.receive_json()
        if m.get("type") == mtype:
            return m
    raise AssertionError(f"no {mtype} received")


def test_hello_ping_pong(client, owner):
    ut, dt = owner
    with _connect(client, dt) as dws:
        assert dws.receive_json() == {"type": "hello", "peer": DEVICE_PEER}
        dws.send_json({"type": "ping"})
        assert dws.receive_json() == {"type": "pong"}
    with _connect(client, ut) as uws:
        hello = uws.receive_json()
        assert hello["type"] == "hello"
        kind, uid, sess = hello["peer"].split(":")
        assert kind == "user" and uid.isdigit() and len(sess) >= 8
        # initial presence snapshot for owned devices
        assert uws.receive_json() == {"type": "presence", "device_id": DEVICE_ID, "online": False}
        uws.send_json({"type": "ping"})
        assert uws.receive_json() == {"type": "pong"}


def test_bad_token_rejected(client):
    with _connect(client, "garbage") as ws:
        assert ws.receive_json() == {"type": "error", "error": "unauthorized"}
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == 4401


def test_presence_and_online_status(client, owner):
    ut, dt = owner
    with _connect(client, ut) as uws:
        uws.receive_json()  # hello
        assert uws.receive_json()["online"] is False
        with _connect(client, dt) as dws:
            dws.receive_json()
            assert uws.receive_json() == {"type": "presence", "device_id": DEVICE_ID, "online": True}
            devs = client.get("/v1/devices", headers=auth(ut)).json()
            assert devs[0]["online"] is True and devs[0]["last_seen"] is not None
        assert uws.receive_json() == {"type": "presence", "device_id": DEVICE_ID, "online": False}
        assert client.get("/v1/devices", headers=auth(ut)).json()[0]["online"] is False


def test_offer_answer_ice_bye_relay(client, owner):
    ut, dt = owner
    with _connect(client, dt) as dws, _connect(client, ut) as uws:
        dws.receive_json()
        user_peer = uws.receive_json()["peer"]
        _recv_until(uws, "presence")

        uws.send_json({"type": "offer", "to": DEVICE_PEER, "sdp": "v=0 offer"})
        m = dws.receive_json()
        assert m == {"type": "offer", "to": DEVICE_PEER, "sdp": "v=0 offer", "from": user_peer}

        dws.send_json({"type": "answer", "to": user_peer, "sdp": "v=0 answer"})
        m = _recv_until(uws, "answer")
        assert m["sdp"] == "v=0 answer" and m["from"] == DEVICE_PEER

        cand = {"candidate": "candidate:1 1 udp 2122260223 192.168.1.5 50000 typ host", "sdpMid": "0", "sdpMLineIndex": 0}
        uws.send_json({"type": "ice", "to": DEVICE_PEER, "candidate": cand})
        m = dws.receive_json()
        assert m["type"] == "ice" and m["candidate"] == cand and m["from"] == user_peer

        uws.send_json({"type": "bye", "to": DEVICE_PEER})
        m = dws.receive_json()
        assert m["type"] == "bye" and m["from"] == user_peer


def test_user_disconnect_sends_bye_to_device(client, owner):
    ut, dt = owner
    with _connect(client, dt) as dws:
        dws.receive_json()
        with _connect(client, ut) as uws:
            user_peer = uws.receive_json()["peer"]
            uws.send_json({"type": "offer", "to": DEVICE_PEER, "sdp": "x"})
            assert dws.receive_json()["type"] == "offer"
        m = dws.receive_json()
        assert m == {"type": "bye", "to": DEVICE_PEER, "from": user_peer}


def test_unauthorized_relay_rejected(client, owner):
    ut, dt = owner
    bob = register_user(client, "bob@example.com")
    bob_dt = claim_device(client, bob, device_id="PM-000999", secret="other-secret")
    with _connect(client, dt) as dws, _connect(client, bob) as bws, _connect(client, bob_dt) as bdws, \
            _connect(client, ut) as uws:
        dws.receive_json()
        bob_peer = bws.receive_json()["peer"]
        bdws.receive_json()
        alice_peer = uws.receive_json()["peer"]

        # Bob → Alice's device: forbidden
        bws.send_json({"type": "offer", "to": DEVICE_PEER, "sdp": "evil"})
        m = _recv_until(bws, "error")
        assert m["error"] == "forbidden"
        # Alice's device → Bob's session: forbidden
        dws.send_json({"type": "answer", "to": bob_peer, "sdp": "leak"})
        assert dws.receive_json()["error"] == "forbidden"
        # Bob's device → Alice: forbidden
        bdws.send_json({"type": "answer", "to": alice_peer, "sdp": "leak"})
        assert bdws.receive_json()["error"] == "forbidden"
        # device → device / user → user not allowed
        dws.send_json({"type": "offer", "to": "device:PM-000999", "sdp": "x"})
        assert dws.receive_json()["error"] == "forbidden"
        uws.send_json({"type": "offer", "to": bob_peer, "sdp": "x"})
        assert _recv_until(uws, "error")["error"] == "forbidden"
        # malformed messages
        uws.send_text("not json")
        assert uws.receive_json() == {"type": "error", "error": "invalid json"}
        uws.send_json({"type": "command", "cmd": {"t": "feed"}})
        assert uws.receive_json()["type"] == "error"

        # the legitimate path still works and nothing leaked to Alice's device
        uws.send_json({"type": "offer", "to": DEVICE_PEER, "sdp": "good"})
        m = dws.receive_json()
        assert m["sdp"] == "good" and m["from"] == alice_peer


def test_relay_to_offline_peer(client, owner):
    ut, _ = owner
    with _connect(client, ut) as uws:
        uws.receive_json()
        _recv_until(uws, "presence")
        uws.send_json({"type": "offer", "to": DEVICE_PEER, "sdp": "x"})
        m = uws.receive_json()
        assert m["type"] == "error" and m["error"] == "peer_offline"


def test_command_delivered_when_online(client, owner):
    ut, dt = owner
    with _connect(client, dt) as dws:
        dws.receive_json()
        r = client.post(f"/v1/devices/{DEVICE_ID}/commands", json={"t": "feed", "grams": 20}, headers=auth(ut))
        assert r.status_code == 202, r.text
        cid = r.json()["id"]
        m = dws.receive_json()
        assert m == {"type": "command", "cmd": {"t": "feed", "grams": 20, "id": cid}}
        # client-supplied id is preserved
        r = client.post(f"/v1/devices/{DEVICE_ID}/commands", json={"t": "estop", "engage": True, "id": "abc"}, headers=auth(ut))
        assert dws.receive_json()["cmd"]["id"] == "abc"
    r = client.post(f"/v1/devices/{DEVICE_ID}/commands", json={"t": "feed", "grams": 20}, headers=auth(ut))
    assert r.status_code == 409


def test_config_put_forwarded_to_device(client, owner):
    ut, dt = owner
    cfg = {"version": 1, "audio": {"speaker_volume": 40}}
    with _connect(client, dt) as dws:
        dws.receive_json()
        r = client.put(f"/v1/devices/{DEVICE_ID}/config", json=cfg, headers=auth(ut))
        assert r.status_code == 200 and r.headers["X-Petmate-Delivered"] == "true"
        assert dws.receive_json() == {"type": "config_set", "config": cfg}


def test_device_reconnect_replaces_old_socket(client, owner):
    _, dt = owner
    with _connect(client, dt) as old:
        old.receive_json()
        with _connect(client, dt) as new:
            assert new.receive_json()["peer"] == DEVICE_PEER
            m = old.receive_json()
            assert m["type"] == "error"
            with pytest.raises(WebSocketDisconnect):
                old.receive_json()
            new.send_json({"type": "ping"})
            assert new.receive_json() == {"type": "pong"}


def test_delete_device_kicks_ws(client, owner):
    ut, dt = owner
    with _connect(client, dt) as dws:
        dws.receive_json()
        assert client.delete(f"/v1/devices/{DEVICE_ID}", headers=auth(ut)).status_code == 204
        assert dws.receive_json() == {"type": "error", "error": "device unclaimed"}
        with pytest.raises(WebSocketDisconnect):
            dws.receive_json()
    # unclaimed device token can't reconnect
    with _connect(client, dt) as dws:
        assert dws.receive_json() == {"type": "error", "error": "device_unclaimed"}
