import asyncio
import threading
import time

import numpy as np
import pytest

av = pytest.importorskip("av")

from pmcore.camera import MainFrame  # noqa: E402
from pmcore.clips import ClipRecorder, encode_jpeg  # noqa: E402
from pmcore.video import SharedH264Encoder  # noqa: E402

W, H = 320, 240


def frame():
    y = np.random.randint(0, 255, (H, W), np.uint8)
    return MainFrame("front", time.monotonic(), np.vstack([y, np.full((H // 2, W), 128, np.uint8)]), W, H)


def test_jpeg():
    data = encode_jpeg(frame(), 80)
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"


def test_clip_record_without_viewers_and_upload(tmp_path):
    async def go():
        enc = SharedH264Encoder(W, H, 25, 500_000)
        enc.start()
        uploads = []

        async def uploader(path, meta):
            uploads.append((path, meta))
            return "clip-1"

        rec = ClipRecorder(enc, tmp_path, uploader, pre_roll_s=1, max_seconds=5)
        stop = threading.Event()

        def feed():
            while not stop.is_set():
                enc.push(frame())
                time.sleep(0.04)

        th = threading.Thread(target=feed, daemon=True)
        th.start()
        try:
            info = await rec.record(2)
            await asyncio.sleep(0.05)
        finally:
            stop.set()
            th.join(1)
            enc.stop()
        assert not enc.active  # recording released the encoder
        path, meta = uploads[0]
        assert path.exists() and meta["camera"] == "front"
        c = av.open(str(path))
        s = c.streams.video[0]
        n = sum(1 for _ in c.decode(video=0))
        assert s.codec_context.name == "h264" and (s.width, s.height) == (W, H)
        assert 1.2 < info["duration_s"] < 3.0 and n > 25

    asyncio.run(go())
