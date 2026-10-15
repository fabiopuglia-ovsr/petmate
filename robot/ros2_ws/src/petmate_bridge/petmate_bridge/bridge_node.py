"""petmate_bridge: ZeroMQ <-> ROS 2 bridge (INTERFACES.md §3).

  SUB  tcp://127.0.0.1:5560  (pm-core binds PUB)   topics: cmd, detections, config
  PUB  tcp://127.0.0.1:5561  (this node binds)      topics: state (2 Hz), event, ack
Multipart frames: [topic utf-8, JSON utf-8].

Teleop is handled here (low latency): {"t":"teleop"} -> /cmd_vel_teleop with a
300 ms deadman. E-stop is sent straight to petmate_base as well. All other
commands are forwarded to petmate_behavior on /petmate/cmd; behavior acks.
"""
from __future__ import annotations

import json
import math
import threading
import time
from typing import Optional

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.time import Time

from geometry_msgs.msg import Twist
from std_msgs.msg import String
from tf2_ros import Buffer, TransformListener

from petmate_interfaces.msg import FeedStatus, HwState, LaunchStatus
from petmate_interfaces.srv import Estop

from petmate_bridge import commands as C
from petmate_bridge.state_builder import build_state

try:
    import zmq
except ImportError:  # pragma: no cover
    zmq = None


class Bridge(Node):
    def __init__(self):
        super().__init__('petmate_bridge')
        dp = self.declare_parameter
        dp('sub_endpoint', 'tcp://127.0.0.1:5560')
        dp('pub_endpoint', 'tcp://127.0.0.1:5561')
        dp('state_rate_hz', 2.0)
        dp('teleop_deadman_s', 0.30)
        dp('teleop_rate_hz', 20.0)
        dp('global_frame', 'map')
        dp('base_frame', 'base_footprint')
        dp('localized_max_age_s', 2.0)
        gp = lambda n: self.get_parameter(n).value  # noqa: E731

        if zmq is None:
            raise RuntimeError('python3-zmq not installed')
        self.ctx = zmq.Context.instance()
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.setsockopt(zmq.LINGER, 0)
        self.pub.setsockopt(zmq.SNDHWM, 1000)
        self.pub.bind(gp('pub_endpoint'))
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.setsockopt(zmq.RCVTIMEO, 100)
        for t in (b'cmd', b'detections', b'config'):
            self.sub.setsockopt(zmq.SUBSCRIBE, t)
        self.sub.connect(gp('sub_endpoint'))
        self._pub_lock = threading.Lock()

        self.deadman = float(gp('teleop_deadman_s'))
        self.global_frame = gp('global_frame')
        self.base_frame = gp('base_frame')

        # cached inputs
        self.hw: Optional[HwState] = None
        self.hw_t = 0.0
        self.feed: Optional[FeedStatus] = None
        self.launch: Optional[LaunchStatus] = None
        self.behavior: dict = {}
        self.teleop = (0.0, 0.0)
        self.teleop_t = 0.0
        self.teleop_was_active = False
        self.last_teleop_hint = 0.0

        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.pub_cmd = self.create_publisher(String, 'petmate/cmd', 50)
        self.pub_det = self.create_publisher(String, 'petmate/detections', 10)
        self.pub_cfg = self.create_publisher(String, 'petmate/config', latched)
        self.pub_teleop = self.create_publisher(Twist, 'cmd_vel_teleop', 10)

        self.create_subscription(HwState, 'petmate/hw_state', self._on_hw, 10)
        self.create_subscription(FeedStatus, 'petmate/feed_status', self._on_feed, latched)
        self.create_subscription(LaunchStatus, 'petmate/launch_status', self._on_launch, latched)
        self.create_subscription(String, 'petmate/behavior_state', self._on_beh, latched)
        self.create_subscription(String, 'petmate/events', self._on_event, 50)
        self.create_subscription(String, 'petmate/ack', self._on_ack, 50)

        self.estop_cli = self.create_client(Estop, 'petmate/estop')

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        self.create_timer(1.0 / float(gp('state_rate_hz')), self._publish_state)
        self.create_timer(1.0 / float(gp('teleop_rate_hz')), self._tick_teleop)

        self._stop = threading.Event()
        self._rx = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx.start()
        self.get_logger().info(f'bridge: SUB {gp("sub_endpoint")}  PUB {gp("pub_endpoint")}')

    # ------------------------------------------------------------ ZMQ out
    def zsend(self, topic: str, obj: dict):
        data = json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        with self._pub_lock:
            try:
                self.pub.send_multipart([topic.encode('utf-8'), data], flags=zmq.NOBLOCK)
            except zmq.Again:
                self.get_logger().warn('ZMQ PUB queue full, dropping', throttle_duration_sec=5.0)

    def ack(self, cid: Optional[str], ok: bool, error: Optional[str] = None):
        if cid:
            self.zsend('ack', {'id': cid, 'ok': bool(ok), 'error': error})

    # ------------------------------------------------------------ ZMQ in
    def _rx_loop(self):
        while not self._stop.is_set() and rclpy.ok():
            try:
                parts = self.sub.recv_multipart()
            except zmq.Again:
                continue
            except zmq.ZMQError as e:
                if self._stop.is_set():
                    return
                self.get_logger().error(f'zmq recv: {e}')
                time.sleep(0.1)
                continue
            if len(parts) != 2:
                continue
            topic = parts[0].decode('utf-8', 'replace')
            try:
                obj = json.loads(parts[1].decode('utf-8'))
            except (UnicodeDecodeError, ValueError):
                self.get_logger().warn(f'bad JSON on {topic}', throttle_duration_sec=5.0)
                continue
            try:
                if topic == 'cmd':
                    self._on_cmd(obj)
                elif topic == 'detections':
                    self.pub_det.publish(String(data=json.dumps(obj)))
                elif topic == 'config':
                    self.pub_cfg.publish(String(data=json.dumps(obj)))
            except Exception as e:  # noqa: BLE001
                self.get_logger().error(f'handling {topic}: {e!r}')

    def _on_cmd(self, cmd):
        if not isinstance(cmd, dict):
            return
        cid = C.ensure_id(cmd)
        ok, err = C.validate(cmd)
        if not ok:
            self.ack(cid, False, err)
            return
        t = cmd['t']
        if t == 'teleop':
            self.teleop = C.clamp_teleop(cmd['v'], cmd['w'])
            self.teleop_t = time.monotonic()
            now = time.monotonic()
            if now - self.last_teleop_hint > 0.5:
                self.last_teleop_hint = now
                self.pub_cmd.publish(String(data=json.dumps({'t': 'teleop_activity'})))
            self.ack(cid, True)        # every cmd is acked (§3), teleop included
            return
        if t == 'estop':
            # direct path to the MCU, independent of the behavior engine
            if self.estop_cli.service_is_ready():
                self.estop_cli.call_async(Estop.Request(engage=bool(cmd['engage'])))
                cmd['_bridge_handled'] = True
            if cmd['engage']:
                self.teleop = (0.0, 0.0)
                self.teleop_t = 0.0
        self.pub_cmd.publish(String(data=json.dumps(cmd)))

    # ------------------------------------------------------------ teleop
    def _tick_teleop(self):
        active = (time.monotonic() - self.teleop_t) <= self.deadman
        if active:
            v, w = self.teleop
        elif self.teleop_was_active:
            v, w = 0.0, 0.0            # one explicit stop, then go silent so
        else:                          # twist_mux releases the teleop input
            return
        self.teleop_was_active = active
        t = Twist()
        t.linear.x, t.angular.z = float(v), float(w)
        self.pub_teleop.publish(t)

    # ------------------------------------------------------------ ROS in
    def _on_hw(self, m: HwState):
        self.hw = m
        self.hw_t = time.monotonic()

    def _on_feed(self, m: FeedStatus):
        self.feed = m

    def _on_launch(self, m: LaunchStatus):
        self.launch = m

    def _on_beh(self, m: String):
        try:
            self.behavior = json.loads(m.data)
        except ValueError:
            pass

    def _on_event(self, m: String):
        try:
            ev = json.loads(m.data)
        except ValueError:
            return
        if isinstance(ev, dict) and 't' in ev:
            ev.setdefault('ts', time.time())
            self.zsend('event', ev)

    def _on_ack(self, m: String):
        try:
            a = json.loads(m.data)
        except ValueError:
            return
        self.ack(a.get('id'), bool(a.get('ok')), a.get('error'))

    # ------------------------------------------------------------ state
    def _pose(self) -> dict:
        try:
            t = self.tf_buffer.lookup_transform(self.global_frame, self.base_frame, Time())
        except Exception:  # noqa: BLE001
            return {'x': 0.0, 'y': 0.0, 'th': 0.0, 'localized': False}
        q = t.transform.rotation
        th = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        age = (self.get_clock().now() - Time.from_msg(t.header.stamp)).nanoseconds * 1e-9
        max_age = float(self.get_parameter('localized_max_age_s').value)
        return {'x': t.transform.translation.x, 'y': t.transform.translation.y, 'th': th,
                'localized': age < max_age}

    def _publish_state(self):
        hw = None
        if self.hw is not None:
            h = self.hw
            hw = {'soc': h.soc_pct, 'voltage': h.batt_v, 'current': h.batt_a,
                  'charging': h.charging, 'docked': h.docked, 'estop': h.estop,
                  'cliff_mask': h.cliff_mask, 'bump_mask': h.bump_mask,
                  'wheel_drop': h.wheel_drop, 'motor_fault': h.motor_fault, 'fw': h.fw,
                  'connected': h.mcu_connected and (time.monotonic() - self.hw_t) < 2.0}
        feed = None
        if self.feed is not None:
            feed = {'state': self.feed.state_name, 'hopper_g': self.feed.hopper_g}
        launch = None
        if self.launch is not None:
            launch = {'state': self.launch.state_name, 'balls': self.launch.balls}
        teleop_active = (time.monotonic() - self.teleop_t) <= 1.0
        st = build_state(time.time(), hw=hw, pose=self._pose(), behavior=self.behavior,
                         feed=feed, launch=launch, teleop_active=teleop_active)
        self.zsend('state', st)

    def destroy_node(self):
        self._stop.set()
        self._rx.join(0.5)
        self.sub.close(0)
        self.pub.close(0)
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = Bridge()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
