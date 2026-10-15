"""Push notifications. `FcmV1Sender` talks to FCM HTTP v1; `LoggingPushSender` is used
when credentials are absent. Event → Italian text mapping honours the device config
`notifications` flags (§5)."""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger("petmate.push")


@dataclass
class Notification:
    title: str
    body: str
    data: dict[str, str] = field(default_factory=dict)


# event type → notifications flag (§5 "notifications") ; None = always sent
EVENT_CATEGORY: dict[str, Optional[str]] = {
    "meal_dispensed": "meals",
    "meal_failed": "meals",
    "feeder_jam": "meals",
    "hopper_low": "meals",
    "pet_detected": "pet_detected",
    "play_started": "activity",
    "play_ended": "activity",
    "docked": "activity",
    "undocked": "activity",
    "nav_failed": "activity",
    "stuck": "activity",
    "cliff": "activity",
    "estop": "activity",
    "battery_low": "battery",
}

# Defaults when the device has no desired config yet (mirrors §5 example).
DEFAULT_FLAGS = {"meals": True, "activity": True, "battery": True, "pet_detected": False}

# Events that are too chatty to push even if "activity" is on.
SILENT_EVENTS = {"docked", "undocked", "play_started"}

_CAMERA_IT = {"front": "anteriore", "rear": "posteriore"}
_CLS_IT = {"cat": "gatto", "dog": "cane", "person": "persona"}
_GAME_IT = {
    "chase_ball": "Insegui la pallina",
    "teaser": "Bacchetta",
    "screen_birds": "Uccellini sullo schermo",
    "laser_dots": "Puntini laser",
}


def _fmt_num(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:.1f}".replace(".", ",")


def render_event(t: str, data: dict[str, Any], device_name: str, pet_name: Optional[str]) -> Optional[Notification]:
    pet = pet_name or "il tuo animale"
    title = device_name or "Petmate"
    body: Optional[str] = None
    if t == "meal_dispensed":
        body = f"Pappa servita: {_fmt_num(data.get('grams', '?'))} g"
    elif t == "meal_failed":
        reason = data.get("reason")
        body = "Pappa non servita" + (f" ({reason})" if reason else "")
    elif t == "feeder_jam":
        body = "Dispenser inceppato: controlla la coclea"
    elif t == "hopper_low":
        g = data.get("grams")
        body = "Croccantini in esaurimento" + (f": restano {_fmt_num(g)} g" if g is not None else "")
    elif t == "battery_low":
        soc = data.get("soc")
        body = "Batteria scarica" + (f": {_fmt_num(soc)}%" if soc is not None else "")
    elif t == "pet_detected":
        cls = _CLS_IT.get(str(data.get("cls")), None)
        cam = _CAMERA_IT.get(str(data.get("camera")), None)
        who = pet if cls in (None, "gatto", "cane") else cls
        body = f"Ho visto {who}" + (f" (camera {cam})" if cam else "")
    elif t == "play_ended":
        game = _GAME_IT.get(str(data.get("game")), data.get("game") or "gioco")
        dur = data.get("duration_s")
        mins = f" di {round(float(dur) / 60)} min" if isinstance(dur, (int, float)) else ""
        body = f"Sessione di gioco{mins} terminata ({game})"
    elif t == "nav_failed":
        body = f"Non riesco a raggiungere: {data.get('target', 'destinazione')}"
    elif t == "stuck":
        body = "Sono bloccato, serve aiuto"
    elif t == "cliff":
        body = "Rilevato un gradino: mi sono fermato"
    elif t == "estop":
        body = "Arresto di emergenza attivato"
    if body is None:
        return None
    return Notification(title=title, body=body, data={"type": "event", "event": t})


def should_notify(t: str, config: Optional[dict[str, Any]]) -> bool:
    if t in SILENT_EVENTS:
        return False
    cat = EVENT_CATEGORY.get(t)
    if cat is None:
        return False
    flags = dict(DEFAULT_FLAGS)
    if config and isinstance(config.get("notifications"), dict):
        flags.update({k: bool(v) for k, v in config["notifications"].items()})
    return flags.get(cat, False)


class PushSender(abc.ABC):
    @abc.abstractmethod
    async def send(self, tokens: list[tuple[str, str]], n: Notification) -> list[str]:
        """Send to [(token, platform)]. Returns the tokens reported invalid (to delete)."""


class LoggingPushSender(PushSender):
    def __init__(self) -> None:
        self.sent: list[tuple[list[tuple[str, str]], Notification]] = []

    async def send(self, tokens, n):
        self.sent.append((tokens, n))
        log.info("push (noop) to %d token(s): %s — %s", len(tokens), n.title, n.body)
        return []


class FcmV1Sender(PushSender):
    """FCM HTTP v1 using a service-account JSON (needs `google-auth`)."""

    SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

    def __init__(self, project_id: str, credentials_file: str):
        from google.oauth2 import service_account  # type: ignore

        self.project_id = project_id
        self._creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=[self.SCOPE]
        )
        self._url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    def _access_token(self) -> str:
        from google.auth.transport.requests import Request  # type: ignore

        if not self._creds.valid or (self._creds.expiry and self._creds.expiry.timestamp() - time.time() < 60):
            self._creds.refresh(Request())
        return self._creds.token

    async def send(self, tokens, n):
        if not tokens:
            return []
        token = await asyncio.to_thread(self._access_token)
        invalid: list[str] = []
        async with httpx.AsyncClient(timeout=10) as client:
            for tok, platform in tokens:
                msg: dict[str, Any] = {
                    "token": tok,
                    "notification": {"title": n.title, "body": n.body},
                    "data": n.data,
                    "android": {"priority": "HIGH", "notification": {"channel_id": "petmate_events"}},
                    "apns": {"payload": {"aps": {"sound": "default"}}},
                }
                try:
                    r = await client.post(
                        self._url,
                        headers={"Authorization": f"Bearer {token}"},
                        content=json.dumps({"message": msg}),
                    )
                except httpx.HTTPError as exc:
                    log.warning("FCM send error: %s", exc)
                    continue
                if r.status_code == 404 or "UNREGISTERED" in r.text:
                    invalid.append(tok)
                elif r.status_code >= 300:
                    log.warning("FCM %s: %s", r.status_code, r.text[:300])
        return invalid


def build_push_sender(project_id: str, credentials_file: str) -> PushSender:
    if project_id and credentials_file:
        try:
            return FcmV1Sender(project_id, credentials_file)
        except Exception as exc:
            log.warning("FCM disabled (%s); falling back to logging sender", exc)
    return LoggingPushSender()
