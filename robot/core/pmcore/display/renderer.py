"""pygame renderer for the round 1080x1080 HDMI display (KMS/DRM).

Runs in its own thread (pygame/SDL is not asyncio-friendly). Reads a
:class:`DisplayState` snapshot every frame.

Modes:
* face  — cat eyes: idle blink + saccades, happy (squinted arcs), sleepy
  (half-closed, slow), alert (wide, red tint); pupils follow pet bearing.
* owner — incoming WebRTC video of the owner, centre-cropped and circularly
  masked.
* game  — cat-oriented animations: high contrast blue/yellow (cats are
  dichromats, best at blue/yellow), small fast prey with erratic,
  stop-and-go motion (birds / fish / mouse).
Privacy: when anyone is watching and ``privacy.stream_led`` is on, a pulsing
ring is drawn on the rim of the round display in every mode.
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
from typing import Any, Optional

from ..log import get_logger, kv
from .state import DisplayState, FaceExpression

log = get_logger(__name__)

BLACK = (0, 0, 0)
BLUE = (20, 60, 255)
YELLOW = (255, 220, 0)
EYE = (120, 220, 90)
EYE_ALERT = (255, 90, 60)
RING = (255, 40, 40)


class Prey:
    """Erratic prey: dart → freeze → dart, bouncing within a circle."""

    def __init__(self, kind: str, r_arena: float, rng: random.Random):
        self.kind = kind
        self.R = r_arena
        self.rng = rng
        a = rng.uniform(0, 2 * math.pi)
        d = rng.uniform(0, r_arena * 0.7)
        self.x, self.y = d * math.cos(a), d * math.sin(a)
        self.vx = self.vy = 0.0
        self.t_next = 0.0
        self.phase = rng.uniform(0, 10)

    def step(self, t: float, dt: float) -> None:
        if t >= self.t_next:
            if self.rng.random() < 0.3:  # freeze (cats pounce on stop-and-go)
                self.vx = self.vy = 0.0
                self.t_next = t + self.rng.uniform(0.3, 1.2)
            else:
                speed = {"birds": 900, "fish": 520, "mouse": 1100}[self.kind] * self.rng.uniform(0.6, 1.3)
                a = self.rng.uniform(0, 2 * math.pi)
                self.vx, self.vy = speed * math.cos(a), speed * math.sin(a)
                self.t_next = t + self.rng.uniform(0.15, 0.7)
        if self.kind == "fish":  # sinuous
            self.vy += math.sin(t * 6 + self.phase) * 600 * dt
        self.x += self.vx * dt
        self.y += self.vy * dt
        r = math.hypot(self.x, self.y)
        lim = self.R * 0.85
        if r > lim:  # reflect at the round edge
            nx, ny = self.x / r, self.y / r
            dot = self.vx * nx + self.vy * ny
            self.vx -= 2 * dot * nx
            self.vy -= 2 * dot * ny
            self.x, self.y = nx * lim, ny * lim


class Renderer:
    def __init__(self, state: DisplayState, size: int = 1080, fps: int = 30, driver: str = "kmsdrm"):
        self.state = state
        self.size = size
        self.fps = fps
        self.driver = driver
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._rng = random.Random()
        self._blink_next = time.monotonic() + 3
        self._blink_t = -1.0
        self._sacc = (0.0, 0.0)
        self._sacc_next = 0.0
        self._prey: list[Prey] = []
        self._prey_kind = ""
        self.ok = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run_guard, name="display", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def _run_guard(self) -> None:
        try:
            self._run()
        except Exception:  # noqa: BLE001
            log.exception("display thread crashed")
        finally:
            try:
                import pygame

                pygame.quit()
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        if self.driver:
            os.environ.setdefault("SDL_VIDEODRIVER", self.driver)
        os.environ.setdefault("SDL_NOMOUSE", "1")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")  # audio is ALSA-direct
        import pygame

        pygame.display.init()
        pygame.font.init()
        flags = pygame.FULLSCREEN if self.driver in ("kmsdrm", "fbcon") else 0
        try:
            screen = pygame.display.set_mode((self.size, self.size), flags)
        except pygame.error as e:
            log.error("display init failed", extra=kv(driver=self.driver, error=str(e)))
            return
        pygame.mouse.set_visible(False)
        self.ok = True
        log.info("display started", extra=kv(size=self.size, driver=pygame.display.get_driver()))
        clock = pygame.time.Clock()
        mask = self._circle_mask(pygame)
        t_prev = time.monotonic()
        while self._running:
            for _ in pygame.event.get():
                pass
            t = time.monotonic()
            dt = min(0.1, t - t_prev)
            t_prev = t
            s = self.state.snapshot()
            screen.fill(BLACK)
            if s["mode"] == "owner" and s["owner_frame"] is not None:
                self._draw_owner(pygame, screen, s["owner_frame"], mask)
            elif s["mode"] == "game":
                self._draw_game(pygame, screen, s["game"], t, dt)
            else:
                self._draw_face(pygame, screen, s["expression"], s["gaze"], t, dt)
            if s["streaming"] and s["stream_led"]:
                self._draw_ring(pygame, screen, t)
            pygame.display.flip()
            clock.tick(self.fps)

    # ---------------------------------------------------------------- helpers
    def _circle_mask(self, pygame):
        m = pygame.Surface((self.size, self.size), pygame.SRCALPHA)
        m.fill((0, 0, 0, 255))
        pygame.draw.circle(m, (0, 0, 0, 0), (self.size // 2, self.size // 2), self.size // 2)
        return m

    def _draw_ring(self, pygame, screen, t: float) -> None:
        c = self.size // 2
        a = 0.55 + 0.45 * math.sin(t * 2 * math.pi * 0.7)
        col = tuple(int(v * a) for v in RING)
        pygame.draw.circle(screen, col, (c, c), c - 6, width=14)

    def _draw_owner(self, pygame, screen, rgb, mask) -> None:
        import numpy as np

        h, w = rgb.shape[:2]
        side = min(h, w)
        y0, x0 = (h - side) // 2, (w - side) // 2
        crop = np.ascontiguousarray(rgb[y0:y0 + side, x0:x0 + side])
        surf = pygame.image.frombuffer(crop.tobytes(), (side, side), "RGB")
        surf = pygame.transform.smoothscale(surf, (self.size, self.size))
        screen.blit(surf, (0, 0))
        screen.blit(mask, (0, 0))

    # ---------------------------------------------------------------- face
    def _draw_face(self, pygame, screen, expr: FaceExpression, gaze: float, t: float, dt: float) -> None:
        S = self.size
        c = S // 2
        # blink scheduling (slower when sleepy)
        if t >= self._blink_next:
            self._blink_t = t
            base = 6.0 if expr == FaceExpression.SLEEPY else 3.5
            self._blink_next = t + self._rng.uniform(base * 0.6, base * 1.6)
        blink_d = 0.35 if expr == FaceExpression.SLEEPY else 0.16
        bp = (t - self._blink_t) / blink_d if self._blink_t >= 0 else 2
        lid = 1.0 - abs(1 - 2 * bp) if 0 <= bp <= 1 else 0.0  # 0 open → 1 closed
        if expr == FaceExpression.SLEEPY:
            lid = max(lid, 0.55 + 0.1 * math.sin(t * 0.8))
        # idle saccades when no pet
        if t >= self._sacc_next:
            self._sacc = (self._rng.uniform(-0.4, 0.4), self._rng.uniform(-0.25, 0.25))
            self._sacc_next = t + self._rng.uniform(1.0, 3.0)
        gx = gaze if gaze != 0.0 else self._sacc[0]
        gy = 0.0 if gaze != 0.0 else self._sacc[1]
        col = EYE_ALERT if expr == FaceExpression.ALERT else EYE
        ew, eh = int(S * 0.27), int(S * 0.36)
        if expr == FaceExpression.ALERT:
            ew, eh = int(ew * 1.1), int(eh * 1.1)
        for side in (-1, 1):
            ex = c + side * int(S * 0.19)
            ey = c - int(S * 0.04)
            rect = pygame.Rect(0, 0, ew, eh)
            rect.center = (ex, ey)
            if expr == FaceExpression.HAPPY:
                # ^ ^ squint arcs
                arc = pygame.Rect(0, 0, ew, int(eh * 0.7))
                arc.center = (ex, ey + int(eh * 0.15))
                pygame.draw.arc(screen, col, arc, math.radians(20), math.radians(160), width=int(S * 0.035))
                continue
            pygame.draw.ellipse(screen, col, rect)
            # slit pupil, wider when alert/happy-ish
            pw = int(ew * (0.32 if expr == FaceExpression.ALERT else 0.18))
            ph = int(eh * 0.8)
            prect = pygame.Rect(0, 0, pw, ph)
            prect.center = (ex + int(gx * ew * 0.28), ey + int(gy * eh * 0.15))
            pygame.draw.ellipse(screen, BLACK, prect)
            hl = (ex + int(gx * ew * 0.28) + int(ew * 0.12), ey - int(eh * 0.2))
            pygame.draw.circle(screen, (255, 255, 255), hl, int(S * 0.018))
            if lid > 0:
                lid_rect = pygame.Rect(rect.left - 4, rect.top - 4, rect.width + 8, int((rect.height + 8) * lid))
                pygame.draw.rect(screen, BLACK, lid_rect)
                if lid > 0.05:
                    pygame.draw.line(screen, col, (rect.left + 10, lid_rect.bottom), (rect.right - 10, lid_rect.bottom), 6)
        # small nose + mouth
        ny = c + int(S * 0.2)
        pygame.draw.polygon(screen, (255, 140, 170), [(c - 22, ny), (c + 22, ny), (c, ny + 22)])
        if expr == FaceExpression.HAPPY:
            for side in (-1, 1):
                r = pygame.Rect(0, 0, 60, 40)
                r.center = (c + side * 30, ny + 40)
                pygame.draw.arc(screen, (255, 140, 170), r, math.radians(200), math.radians(340), 5)

    # ---------------------------------------------------------------- games
    def _draw_game(self, pygame, screen, game: str, t: float, dt: float) -> None:
        S = self.size
        c = S // 2
        n = {"birds": 3, "fish": 4, "mouse": 1}[game]
        if self._prey_kind != game:
            self._prey = [Prey(game, c, self._rng) for _ in range(n)]
            self._prey_kind = game
        screen.fill(BLUE if game != "mouse" else (10, 20, 90))
        for p in self._prey:
            p.step(t, dt)
            x, y = int(c + p.x), int(c + p.y)
            ang = math.atan2(p.vy, p.vx) if (p.vx or p.vy) else 0.0
            if game == "birds":
                flap = math.sin(t * 30) * 0.6
                for side in (-1, 1):
                    wx = x + int(math.cos(ang + side * (1.6 + flap)) * 70)
                    wy = y + int(math.sin(ang + side * (1.6 + flap)) * 70)
                    pygame.draw.line(screen, YELLOW, (x, y), (wx, wy), 16)
                pygame.draw.circle(screen, YELLOW, (x, y), 28)
                pygame.draw.circle(screen, BLACK, (x + int(math.cos(ang) * 14), y + int(math.sin(ang) * 14)), 6)
            elif game == "fish":
                body = pygame.Rect(0, 0, 110, 50)
                body.center = (x, y)
                pygame.draw.ellipse(screen, YELLOW, body)
                tx, ty = x - int(math.cos(ang) * 60), y - int(math.sin(ang) * 60)
                wig = math.sin(t * 20) * 20
                pygame.draw.polygon(screen, YELLOW, [
                    (tx, ty),
                    (tx - int(math.cos(ang) * 40 - math.sin(ang) * (30 + wig)), ty - int(math.sin(ang) * 40 + math.cos(ang) * (30 + wig))),
                    (tx - int(math.cos(ang) * 40 + math.sin(ang) * (30 - wig)), ty - int(math.sin(ang) * 40 - math.cos(ang) * (30 - wig))),
                ])
            else:  # mouse: grey-yellow body with long tail, fast scurry
                pygame.draw.circle(screen, YELLOW, (x, y), 34)
                hx, hy = x + int(math.cos(ang) * 38), y + int(math.sin(ang) * 38)
                pygame.draw.circle(screen, YELLOW, (hx, hy), 20)
                for k in range(8):
                    tt = k / 8
                    tx = x - int(math.cos(ang) * (40 + tt * 120)) + int(math.sin(t * 15 + k) * 8)
                    ty = y - int(math.sin(ang) * (40 + tt * 120)) + int(math.cos(t * 15 + k) * 8)
                    pygame.draw.circle(screen, YELLOW, (tx, ty), max(2, int(8 - tt * 6)))
