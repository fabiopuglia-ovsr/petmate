"""pm-core: Petmate host service (Raspberry Pi 5).

Implements the host side of docs/INTERFACES.md: ZeroMQ IPC (§3), WebRTC
DataChannel/media (§4), configuration store (§5), cloud REST + signaling
(§6), BLE provisioning (§7) and on-robot paths (§8).

Hardware-specific modules (picamera2, Hailo, pygame, bless) are imported
lazily so that the package imports and its pure logic is testable on a
development machine.
"""

__version__ = "0.1.0"
MODEL = "PM-EVT1"
