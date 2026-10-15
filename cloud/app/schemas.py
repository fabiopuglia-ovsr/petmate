from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class LoginBody(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class AccessToken(BaseModel):
    access_token: str


class ClaimTokenOut(BaseModel):
    claim_token: str
    expires_at: str  # ISO 8601 UTC


class DeviceRegisterIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    device_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    device_secret: str = Field(min_length=1, max_length=256)
    claim_token: str = Field(min_length=1, max_length=128)
    fw: Optional[str] = Field(default=None, max_length=32)  # optional extension


class DeviceRegisterOut(BaseModel):
    device_token: str
    owner_user_id: int


class DeviceTokenIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    device_id: str = Field(min_length=1, max_length=64)
    device_secret: str = Field(min_length=1, max_length=256)
    fw: Optional[str] = Field(default=None, max_length=32)  # optional extension


class DeviceTokenOut(BaseModel):
    device_token: str


class DeviceOut(BaseModel):
    device_id: str
    name: str
    online: bool
    last_seen: Optional[str]
    fw: Optional[str]


class DevicePatch(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class PushTokenIn(BaseModel):
    token: str = Field(min_length=1, max_length=4096)
    platform: Literal["android", "ios", "web"]


# §4.2 command types (both ZMQ-forwarded and DataChannel-only ones).
COMMAND_TYPES = {
    "teleop", "feed", "feed_abort", "launch", "intake", "tease", "led", "goto", "mode",
    "estop", "play", "save_poi", "map",
    "camera", "display", "volume", "snapshot", "record_clip", "config_get", "config_set",
}

# §4.4 event types.
EVENT_TYPES = {
    "meal_dispensed", "meal_failed", "feeder_jam", "hopper_low", "pet_detected",
    "play_started", "play_ended", "battery_low", "docked", "undocked", "stuck", "cliff",
    "estop", "nav_failed",
}


class EventIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    t: str = Field(min_length=1, max_length=64)
    ts: Optional[float] = None

    def payload(self) -> dict[str, Any]:
        return dict(self.model_extra or {})
