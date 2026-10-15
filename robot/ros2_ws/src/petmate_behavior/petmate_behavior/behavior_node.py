"""petmate_behavior: behavior engine for Petmate.

Interfaces (internal ROS topics, JSON in std_msgs/String, shared with
petmate_bridge):
  sub  /petmate/cmd            §4.2 command objects (with "id"), teleop excluded
  sub  /petmate/detections     §3 detections payload
  sub  /petmate/config         §5 config (TRANSIENT_LOCAL)
  pub  /petmate/events         §4.4 event objects
  pub  /petmate/ack            {"id","ok","error"}
  pub  /petmate/behavior_state {mode, activity, today_g, last_meal, nav, pet, pois}
Robot side:
  /petmate/hw_state, /petmate/feed_status, /petmate/launch_status, /petmate/mcu_event
  services /petmate/{feed,feed_abort,launch,intake,tease,led,estop}
  NavigateToPose action (Nav2), TF map->base_footprint, /scan
  /cmd_vel_docking (twist_mux "docking" priority), /petmate/safety_lock (twist_mux lock)
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from typing import Callable, Optional

import rclpy
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.time import Time

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, Twist
from nav2_msgs.action import NavigateToPose
from nav_msgs.msg import Odometry
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformListener

from petmate_interfaces.msg import FeedStatus, HwState, LaunchStatus, McuEvent
from petmate_interfaces.srv import Estop, Feed, Intake, Launch, Led, Tease

from petmate_behavior import events as E
from petmate_behavior.geometry import (SeekParams, dock_reverse_cmd, predock_pose,
                                       quat_from_yaw, range_at_bearing, seek_cmd,
                                       wrap, yaw_from_quat)
from petmate_behavior.pois import PoiStore
from petmate_behavior.scheduler import Scheduler, SchedulerState
from petmate_behavior.slam_manager import SlamManager

INTAKE = {'off': 0, 'in': 1, 'out': 2}
TEASE = {'stop': 0, 'wiggle': 1, 'sweep': 2, 'random': 3}
LED = {'off': 0, 'solid': 1, 'breathe': 2, 'rainbow': 3, 'chase': 4}


class Cancelled(Exception):
    pass


class TaskFailed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BehaviorNode(Node):
    def __init__(self):
        super().__init__('petmate_behavior')
        dp = self.declare_parameter
        dp('config_path', '/var/lib/petmate/config.json')
        dp('pois_path', '/var/lib/petmate/pois.yaml')
        dp('state_path', '/var/lib/petmate/behavior_state.json')
        dp('map_base', '/var/lib/petmate/maps/home')
        dp('slam_managed', True)
        dp('slam_mode', 'localization')    # initial mode: mapping|localization
        dp('slam_extra_args', [''])
        dp('global_frame', 'map')
        dp('base_frame', 'base_footprint')
        dp('initial_mode', 'auto')
        dp('predock_dist', 0.45)
        dp('dock_reverse_speed', 0.05)
        dp('dock_timeout_extra_s', 6.0)
        dp('dock_retries', 2)
        dp('undock_dist', 0.25)
        dp('undock_speed', 0.10)
        dp('auto_save_dock_pose', True)
        dp('return_to_dock_after_task', True)
        dp('idle_return_s', 120.0)
        dp('battery_low_soc', 20)
        dp('feed_timeout_s', 120.0)
        dp('meal_stay_s', 1200.0)   # stay still at the feeding spot after a meal (open bowl: no driving with food)
        dp('seek_timeout_s', 60.0)
        dp('seek_target_dist', 0.80)
        dp('seek_v_max', 0.25)
        dp('pet_event_cooldown_s', 300.0)
        dp('scheduler_grace_s', 900.0)
        dp('stuck_timeout_s', 20.0)
        dp('use_opennav_docking', False)   # hook: delegate to opennav_docking DockRobot action

        gp = lambda n: self.get_parameter(n).value  # noqa: E731
        self.global_frame = gp('global_frame')
        self.base_frame = gp('base_frame')
        self.cbg = ReentrantCallbackGroup()
        self._lock = threading.RLock()

        # ---------------- state
        self.mode = gp('initial_mode')
        self.activity = 'idle'
        self.nav = {'status': 'idle', 'goal': None, 'mapping': False}
        self.hw: Optional[HwState] = None
        self.feed_status: Optional[FeedStatus] = None
        self.feed_seq = 0
        self.launch_status: Optional[LaunchStatus] = None
        self.launch_seq = 0
        self.scan: Optional[LaserScan] = None
        self.odom_yaw = 0.0
        self.last_teleop = 0.0
        self.last_activity_ts = time.time()
        self.last_patrol_ts = time.time()
        self.flag_edges = E.FlagEdges()
        self.pets = E.PetSightings(cooldown_s=float(gp('pet_event_cooldown_s')))
        self.pet_yaw_at_det: Optional[float] = None
        self.battery = E.BatteryMonitor(low_soc=int(gp('battery_low_soc')))
        self.rate = E.RateLimiter(5.0)
        self.pois = PoiStore(gp('pois_path'))
        self.sched = Scheduler(self._load_config_file(), state=self._load_sched_state(),
                               grace_s=float(gp('scheduler_grace_s')))

        # ---------------- task runner
        self._task_thread: Optional[threading.Thread] = None
        self._task_cancel = threading.Event()
        self._task_name: Optional[str] = None

        # ---------------- ROS I/O
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.pub_event = self.create_publisher(String, 'petmate/events', 50)
        self.pub_ack = self.create_publisher(String, 'petmate/ack', 50)
        self.pub_state = self.create_publisher(String, 'petmate/behavior_state', latched)
        self.pub_cmd = self.create_publisher(Twist, 'cmd_vel_docking', 10)
        self.pub_lock = self.create_publisher(Bool, 'petmate/safety_lock', 10)

        cb = self.cbg
        self.create_subscription(String, 'petmate/cmd', self._on_cmd, 50, callback_group=cb)
        self.create_subscription(String, 'petmate/detections', self._on_detections, 10,
                                 callback_group=cb)
        self.create_subscription(String, 'petmate/config', self._on_config, latched,
                                 callback_group=cb)
        self.create_subscription(HwState, 'petmate/hw_state', self._on_hw, 10, callback_group=cb)
        self.create_subscription(FeedStatus, 'petmate/feed_status', self._on_feed_status, latched,
                                 callback_group=cb)
        self.create_subscription(LaunchStatus, 'petmate/launch_status', self._on_launch_status,
                                 latched, callback_group=cb)
        self.create_subscription(McuEvent, 'petmate/mcu_event', self._on_mcu_event, 20,
                                 callback_group=cb)
        scan_qos = QoSProfile(depth=5, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.create_subscription(LaserScan, 'scan', self._on_scan, scan_qos, callback_group=cb)
        self.create_subscription(Odometry, 'odometry/filtered', self._on_odom, 20,
                                 callback_group=cb)

        self.cli = {
            'feed': self.create_client(Feed, 'petmate/feed', callback_group=cb),
            'feed_abort': self.create_client(Trigger, 'petmate/feed_abort', callback_group=cb),
            'launch': self.create_client(Launch, 'petmate/launch', callback_group=cb),
            'intake': self.create_client(Intake, 'petmate/intake', callback_group=cb),
            'tease': self.create_client(Tease, 'petmate/tease', callback_group=cb),
            'led': self.create_client(Led, 'petmate/led', callback_group=cb),
            'estop': self.create_client(Estop, 'petmate/estop', callback_group=cb),
        }
        self.nav_client = ActionClient(self, NavigateToPose, 'navigate_to_pose', callback_group=cb)
        self._nav_goal_handle = None

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self, spin_thread=False)

        self.slam: Optional[SlamManager] = None
        if gp('slam_managed'):
            extra = [a for a in gp('slam_extra_args') if a]
            self.slam = SlamManager(gp('map_base'), extra_args=extra, logger=self.get_logger())
            self._start_slam(gp('slam_mode'))

        self.create_timer(0.5, self._publish_state, callback_group=cb)
        self.create_timer(0.2, self._tick_safety, callback_group=cb)
        self.create_timer(1.0, self._tick_auto, callback_group=cb)
        self.get_logger().info(f'petmate_behavior started, mode={self.mode}')

    # ================================================================ helpers
    def now(self) -> float:
        return time.time()

    def emit(self, t: str, **kw):
        ev = E.ev(t, self.now(), **kw)
        self.pub_event.publish(String(data=json.dumps(ev)))
        self.get_logger().info(f'event {ev}')

    def ack(self, cid: Optional[str], ok: bool, error: Optional[str] = None):
        if cid is None:
            return
        self.pub_ack.publish(String(data=json.dumps({'id': cid, 'ok': ok, 'error': error})))

    def _load_config_file(self) -> dict:
        try:
            with open(self.get_parameter('config_path').value, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _load_sched_state(self) -> SchedulerState:
        try:
            with open(self.get_parameter('state_path').value, 'r', encoding='utf-8') as f:
                return SchedulerState.from_dict(json.load(f))
        except (OSError, ValueError):
            return SchedulerState()

    def _save_sched_state(self):
        path = self.get_parameter('state_path').value
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.sched.state.to_dict(), f)
            os.replace(tmp, path)
        except OSError as e:
            self.get_logger().warn(f'cannot persist scheduler state: {e}')

    def pose(self, max_age: float = 2.0) -> Optional[dict]:
        try:
            t = self.tf_buffer.lookup_transform(self.global_frame, self.base_frame, Time())
        except Exception:  # noqa: BLE001
            return None
        age = (self.get_clock().now() - Time.from_msg(t.header.stamp)).nanoseconds * 1e-9
        q = t.transform.rotation
        return {'x': t.transform.translation.x, 'y': t.transform.translation.y,
                'th': yaw_from_quat(q.x, q.y, q.z, q.w), 'localized': age < max_age}

    @property
    def docked(self) -> bool:
        return bool(self.hw and self.hw.docked)

    @property
    def estopped(self) -> bool:
        return bool(self.hw and (self.hw.estop or self.hw.wheel_drop))

    def teleop_recent(self, s: float = 10.0) -> bool:
        return self.now() - self.last_teleop < s

    def call(self, name: str, req, timeout: float = 2.0):
        cli = self.cli[name]
        if not cli.wait_for_service(timeout_sec=timeout):
            raise TaskFailed(f'{name}_unavailable')
        fut = cli.call_async(req)
        ev = threading.Event()
        fut.add_done_callback(lambda _f: ev.set())
        if not ev.wait(timeout):
            raise TaskFailed(f'{name}_timeout')
        res = fut.result()
        if hasattr(res, 'success') and not res.success:
            raise TaskFailed(f'{name}:{res.message}')
        return res

    def led(self, mode: str, color=(0xF0, 0xA8, 0x30)):
        try:
            self.call('led', Led.Request(mode=LED[mode], r=color[0], g=color[1], b=color[2]))
        except TaskFailed as e:
            self.get_logger().warn(f'led: {e.reason}')

    def drive(self, v: float, w: float):
        t = Twist()
        t.linear.x = float(v)
        t.angular.z = float(w)
        self.pub_cmd.publish(t)

    def sleep(self, s: float):
        if self._task_cancel.wait(s):
            raise Cancelled()

    def check(self):
        if self._task_cancel.is_set():
            raise Cancelled()
        if self.estopped:
            raise TaskFailed('estop')

    # ================================================================ task runner
    def busy(self) -> bool:
        return self._task_thread is not None and self._task_thread.is_alive()

    def start_task(self, name: str, activity: str, fn: Callable[[], None],
                   on_fail: Optional[Callable[[str], None]] = None,
                   then_dock: bool = False) -> None:
        self.cancel_task()
        self._task_cancel = threading.Event()
        cancel = self._task_cancel

        def runner():
            self.set_activity(activity)
            try:
                fn()
            except Cancelled:
                self.get_logger().info(f'task {name} cancelled')
            except TaskFailed as e:
                self.get_logger().warn(f'task {name} failed: {e.reason}')
                if on_fail:
                    on_fail(e.reason)
            except Exception as e:  # noqa: BLE001
                self.get_logger().error(f'task {name} crashed: {e!r}')
                if on_fail:
                    on_fail('internal_error')
            finally:
                self.drive(0.0, 0.0)
                self._cancel_nav()
                self.last_activity_ts = self.now()
                with self._lock:
                    if self._task_cancel is cancel:
                        self._task_name = None
                        self.set_activity('sleeping' if self.mode == 'sleep' else
                                          ('mapping' if self.nav['mapping'] else 'idle'))
            if then_dock and not cancel.is_set() and self.mode in ('auto', 'sleep') \
                    and not self.docked and self.pois.get('dock') is not None:
                self.start_task('dock', 'docking', self.task_dock,
                                on_fail=lambda r: self.emit('nav_failed', target='dock'))

        with self._lock:
            self._task_name = name
            self._task_thread = threading.Thread(target=runner, name=f'task_{name}', daemon=True)
            self._task_thread.start()

    def cancel_task(self, wait: float = 3.0):
        th = self._task_thread
        self._task_cancel.set()
        self._cancel_nav()
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(wait)
        self.drive(0.0, 0.0)

    def set_activity(self, a: str):
        self.activity = a
        self.last_activity_ts = self.now()

    # ================================================================ navigation
    def _cancel_nav(self):
        gh = self._nav_goal_handle
        self._nav_goal_handle = None
        if gh is not None:
            try:
                gh.cancel_goal_async()
            except Exception:  # noqa: BLE001
                pass

    def navigate(self, x: float, y: float, th: float, goal_name: str,
                 timeout: float = 300.0):
        self.check()
        p = self.pose()
        if p is None or not p['localized']:
            raise TaskFailed('not_localized')
        if self.docked:
            self.undock()
        if not self.nav_client.wait_for_server(timeout_sec=5.0):
            raise TaskFailed('nav2_unavailable')
        goal = NavigateToPose.Goal()
        ps = PoseStamped()
        ps.header.frame_id = self.global_frame
        ps.header.stamp = self.get_clock().now().to_msg()
        ps.pose.position.x, ps.pose.position.y = float(x), float(y)
        _, _, qz, qw = quat_from_yaw(th)
        ps.pose.orientation.z, ps.pose.orientation.w = qz, qw
        goal.pose = ps
        self.nav.update(status='active', goal=goal_name)
        done = threading.Event()
        send_fut = self.nav_client.send_goal_async(goal)
        send_fut.add_done_callback(lambda _f: done.set())
        if not done.wait(10.0):
            self.nav['status'] = 'failed'
            raise TaskFailed('nav_send_timeout')
        gh = send_fut.result()
        if not gh.accepted:
            self.nav['status'] = 'failed'
            raise TaskFailed('nav_rejected')
        self._nav_goal_handle = gh
        res_done = threading.Event()
        res_fut = gh.get_result_async()
        res_fut.add_done_callback(lambda _f: res_done.set())
        t0 = self.now()
        last_move_t, last_p = t0, self.pose()
        stuck_t = float(self.get_parameter('stuck_timeout_s').value)
        while not res_done.wait(0.2):
            if self._task_cancel.is_set():
                self.nav['status'] = 'canceled'
                raise Cancelled()
            if self.estopped:
                self.nav['status'] = 'failed'
                raise TaskFailed('estop')
            if self.now() - t0 > timeout:
                self.nav['status'] = 'failed'
                raise TaskFailed('nav_timeout')
            cp = self.pose()
            moved = bool(cp and last_p and (
                math.hypot(cp['x'] - last_p['x'], cp['y'] - last_p['y']) > 0.05
                or abs(wrap(cp['th'] - last_p['th'])) > 0.2))
            if moved or last_p is None:
                last_move_t, last_p = self.now(), cp
            elif self.now() - last_move_t > stuck_t:
                if self.rate.allow('stuck', self.now()):
                    self.emit('stuck')
                last_move_t = self.now()
        self._nav_goal_handle = None
        status = res_fut.result().status
        if status == GoalStatus.STATUS_SUCCEEDED:
            self.nav['status'] = 'succeeded'
            return
        if status == GoalStatus.STATUS_CANCELED:
            self.nav['status'] = 'canceled'
            raise Cancelled()
        self.nav['status'] = 'failed'
        raise TaskFailed('nav_failed')

    def undock(self):
        """Open-loop forward exit from the dock."""
        prev = self.activity
        self.set_activity('undocking')
        dist = float(self.get_parameter('undock_dist').value)
        v = float(self.get_parameter('undock_speed').value)
        t_end = self.now() + dist / v
        while self.now() < t_end:
            self.check()
            self.drive(v, 0.0)
            self.sleep(0.05)
        self.drive(0.0, 0.0)
        self.set_activity(prev)

    def rotate_by(self, angle: float, w: float = 0.8, timeout: float = 15.0):
        start = self.odom_yaw
        acc, last = 0.0, start
        t0 = self.now()
        while abs(acc) < abs(angle) and self.now() - t0 < timeout:
            self.check()
            self.drive(0.0, math.copysign(w, angle))
            self.sleep(0.05)
            acc += wrap(self.odom_yaw - last)
            last = self.odom_yaw
        self.drive(0.0, 0.0)

    # ================================================================ tasks
    def task_dock(self):
        """Stored dock pose + open-loop reverse until flags bit4 (docked).

        HOOK: when use_opennav_docking is true, replace this whole method with
        a nav2 DockRobot action call (opennav_docking, dock_type e.g.
        'petmate_dock' using the rear AprilTag). Interface kept identical:
        raise TaskFailed on failure, return on docked.
        """
        if self.docked:
            return
        if self.get_parameter('use_opennav_docking').value:
            raise TaskFailed('opennav_docking_not_implemented')
        dock = self.pois.get('dock')
        if dock is None:
            raise TaskFailed('no_dock_pose')
        d = float(self.get_parameter('predock_dist').value)
        pre = predock_pose(dock, d)
        self.navigate(pre['x'], pre['y'], pre['th'], 'dock')
        self.nav['goal'] = 'dock'
        self.set_activity('docking')
        v = float(self.get_parameter('dock_reverse_speed').value)
        retries = int(self.get_parameter('dock_retries').value)
        for attempt in range(retries + 1):
            t_end = self.now() + (d + 0.15) / v + float(self.get_parameter('dock_timeout_extra_s').value)
            bumped_t = None
            while self.now() < t_end:
                if self._task_cancel.is_set():
                    raise Cancelled()
                if self.hw and self.hw.estop:
                    raise TaskFailed('estop')
                if self.docked:
                    self.drive(0.0, 0.0)
                    self.nav['status'] = 'succeeded'
                    return
                if self.hw and (self.hw.bump_mask & 0x02):
                    # rear bumper: MCU blocks v<0. Give contacts 1 s to close.
                    bumped_t = bumped_t or self.now()
                    if self.now() - bumped_t > 1.0:
                        break
                p = self.pose()
                yaw = p['th'] if p else self.odom_yaw
                vv, ww = dock_reverse_cmd(yaw, dock['th'] if p else self.odom_yaw, v=v)
                self.drive(vv, ww)
                self.sleep(0.05)
            self.drive(0.0, 0.0)
            if attempt < retries:
                self.get_logger().warn(f'dock attempt {attempt + 1} failed, retrying')
                t_end = self.now() + 0.20 / 0.08
                while self.now() < t_end:
                    self.check()
                    self.drive(0.08, 0.0)
                    self.sleep(0.05)
                self.drive(0.0, 0.0)
                self.navigate(pre['x'], pre['y'], pre['th'], 'dock')
                self.set_activity('docking')
        self.nav['status'] = 'failed'
        raise TaskFailed('dock_timeout')

    def task_goto_poi(self, name: str):
        p = self.pois.get(name)
        if p is None:
            raise TaskFailed('unknown_poi')
        self.navigate(p['x'], p['y'], p['th'], name)

    def task_seek_pet(self, timeout: Optional[float] = None, searched: bool = False):
        timeout = timeout or float(self.get_parameter('seek_timeout_s').value)
        params = SeekParams(target_dist=float(self.get_parameter('seek_target_dist').value),
                            v_max=min(0.25, float(self.get_parameter('seek_v_max').value)))
        self.nav.update(status='active', goal='pet')
        if self.docked:
            self.undock()
        self.set_activity('seeking_pet')
        t0 = self.now()
        while self.now() - t0 < timeout:
            self.check()
            if not self.pets.seen_within(self.now(), 3.0):
                if not self.pets.seen_within(self.now(), 10.0) and not searched:
                    # one slow 360 deg search
                    searched = True
                    self.drive(0.0, 0.0)
                    t_s = self.now()
                    acc, last = 0.0, self.odom_yaw
                    while abs(acc) < 2 * math.pi and not self.pets.seen_within(self.now(), 0.5):
                        self.check()
                        if self.now() - t_s > 20.0:
                            break
                        self.drive(0.0, 0.5)
                        self.sleep(0.05)
                        acc += wrap(self.odom_yaw - last)
                        last = self.odom_yaw
                    continue
                if not self.pets.seen_within(self.now(), 10.0):
                    break
                self.drive(0.0, 0.0)
                self.sleep(0.1)
                continue
            bearing = self.pets.bearing
            if bearing is not None and self.pet_yaw_at_det is not None:
                bearing = wrap(bearing - wrap(self.odom_yaw - self.pet_yaw_at_det))
            dist = None
            if self.scan is not None and bearing is not None:
                s = self.scan
                dist = range_at_bearing(list(s.ranges), s.angle_min, s.angle_increment, bearing,
                                        range_min=max(0.05, s.range_min), range_max=s.range_max)
            v, w, arrived = seek_cmd(bearing, dist, params)
            # generic obstacle guard in the driving direction (other obstacles)
            if v > 0 and self.scan is not None:
                s = self.scan
                fwd = range_at_bearing(list(s.ranges), s.angle_min, s.angle_increment, 0.0,
                                       half_width=0.35, range_min=max(0.05, s.range_min),
                                       range_max=s.range_max)
                if fwd is not None and fwd < 0.35:
                    v = 0.0
            if arrived:
                self.drive(0.0, 0.0)
                self.nav['status'] = 'succeeded'
                return
            self.drive(v, w)
            self.sleep(0.1)
        self.drive(0.0, 0.0)
        self.nav['status'] = 'failed'
        raise TaskFailed('pet_not_found')

    def wait_feed(self, seq0: int, timeout: float) -> FeedStatus:
        t0 = self.now()
        while self.now() - t0 < timeout:
            self.sleep(0.1)
            fs = self.feed_status
            if self.feed_seq != seq0 and fs is not None and fs.state in (2, 3, 4, 5):
                return fs
        raise TaskFailed('feed_timeout')

    def task_feed(self, grams: float, slot_key: Optional[str], go_to_poi: bool):
        meal_done = False
        try:
            if go_to_poi:
                poi = (self.sched.feeding.get('feeding_poi') or '').strip()
                if poi and self.pois.get(poi):
                    try:
                        self.task_goto_poi(poi)
                    except TaskFailed as e:
                        if e.reason != 'not_localized':
                            self.emit('nav_failed', target=poi)
                        raise
            self.set_activity('feeding')
            self.drive(0.0, 0.0)
            self.sleep(0.7)                    # MCU: auger only if still > 500 ms
            seq0 = self.feed_seq
            self.call('feed', Feed.Request(grams=float(grams)))
            fs = self.wait_feed(seq0, float(self.get_parameter('feed_timeout_s').value))
            dispensed = float(fs.dispensed_g)
            if dispensed > 0.0:
                self.sched.record_meal(dispensed, self.now())
            if fs.state == 2:
                self.emit('meal_dispensed', grams=round(dispensed, 1))
                meal_done = True
                # The bowl is open at the front: stay parked while the pet eats.
                # Any new command cancels the task (Cancelled from sleep()).
                stay = float(self.get_parameter('meal_stay_s').value)
                t_stay = self.now()
                while self.now() - t_stay < stay:
                    self.drive(0.0, 0.0)
                    self.sleep(1.0)
            else:
                reason = {3: 'jam', 4: 'hopper_empty', 5: 'aborted'}[fs.state]
                self.emit('meal_failed', reason=reason)
        except Cancelled:
            if not meal_done:          # canceled during the post-meal stay is not a failure
                try:
                    self.call('feed_abort', Trigger.Request())
                except TaskFailed:
                    pass
                self.emit('meal_failed', reason='canceled')
            raise
        finally:
            if slot_key:
                self.sched.mark_handled(slot_key)
            self._save_sched_state()

    def _launch_wait(self, power: int, count: int, timeout: float = 15.0) -> LaunchStatus:
        seq0 = self.launch_seq
        self.call('launch', Launch.Request(power_pct=int(power), count=int(count)))
        t0 = self.now()
        while self.now() - t0 < timeout:
            self.sleep(0.1)
            ls = self.launch_status
            if self.launch_seq != seq0 and ls is not None and ls.state in (3, 4):
                return ls
        raise TaskFailed('launch_timeout')

    def _face_pet(self, gain: float = 1.2):
        """Rotate in place towards the last pet bearing (no translation)."""
        if self.pets.seen_within(self.now(), 2.0) and self.pets.bearing is not None:
            b = self.pets.bearing
            if self.pet_yaw_at_det is not None:
                b = wrap(b - wrap(self.odom_yaw - self.pet_yaw_at_det))
            if abs(b) > 0.15:
                self.drive(0.0, max(-0.8, min(0.8, gain * b)))
                return
        self.drive(0.0, 0.0)

    def task_play(self, game: str, duration_s: float):
        self.set_activity('playing')
        t0 = self.now()
        active = 0.0
        self.emit('play_started', game=game)
        power = int((self.sched.play.get('launch_power') or 40))
        tease_cycle = ['wiggle', 'sweep', 'random']
        try:
            if self.docked:
                self.undock()
                self.set_activity('playing')
            if game == 'screen_birds':
                self.emit('display_game', game='birds')
            elif game == 'laser_dots':
                # NO real laser (animal eye safety): LED chase + on-screen game
                self.emit('display_game', game='mouse')
                self.led('chase', (255, 40, 40))
            i = 0
            next_action = 0.0
            last = self.now()
            while self.now() - t0 < duration_s:
                self.check()
                now = self.now()
                if self.pets.seen_within(now, 3.0):
                    active += now - last
                last = now
                if game == 'chase_ball' and now >= next_action:
                    self.drive(0.0, 0.0)
                    self.sleep(0.5)            # MCU: flywheel only with v=0
                    ls = self._launch_wait(power, 1)
                    if ls.state == 4 or ls.balls == 0:
                        self.get_logger().info('launcher empty: ending chase_ball')
                        break
                    next_action = self.now() + 20.0    # then watch
                elif game == 'teaser' and now >= next_action:
                    pat = tease_cycle[i % len(tease_cycle)]
                    i += 1
                    self.call('tease', Tease.Request(pattern=TEASE[pat], duration_ms=15000))
                    next_action = self.now() + 15.0
                elif game == 'laser_dots' and now >= next_action:
                    colors = [(255, 40, 40), (40, 255, 40), (40, 120, 255), (255, 200, 0)]
                    self.led('chase', colors[i % len(colors)])
                    i += 1
                    next_action = self.now() + 4.0
                if game == 'teaser':
                    self.drive(0.0, 0.0)       # keep base still while the arm moves
                else:
                    self._face_pet()
                self.sleep(0.1)
        finally:
            self.drive(0.0, 0.0)
            if game == 'teaser':
                try:
                    self.call('tease', Tease.Request(pattern=TEASE['stop'], duration_ms=0))
                except TaskFailed:
                    pass
            if game in ('screen_birds', 'laser_dots'):
                self.emit('display_game', game=None)  # §9.3: null = back to face
            if game == 'laser_dots':
                self.led('breathe')
            self.emit('play_ended', game=game, duration_s=round(self.now() - t0, 1),
                      active_s=round(active, 1))

    def task_scheduled_play(self, duration_s: float, slot_key: str):
        try:
            game = self.sched.next_game()
            if game is None:
                return
            if not self.pets.seen_within(self.now(), 10.0):
                try:
                    self.task_seek_pet(timeout=30.0)
                except TaskFailed:
                    pass                       # play anyway: the pet may come
            self.task_play(game, duration_s)
        finally:
            self.sched.mark_handled(slot_key)
            self._save_sched_state()

    def task_patrol(self):
        names = [n for n in self.pois.names() if n != 'dock']
        self.last_patrol_ts = self.now()
        for n in names:
            self.check()
            try:
                self.task_goto_poi(n)
            except TaskFailed as e:
                self.emit('nav_failed', target=n)
                if e.reason in ('not_localized', 'nav2_unavailable', 'estop'):
                    raise
            self.sleep(3.0)

    def task_sleep(self):
        if not self.docked and self.pois.get('dock'):
            self.task_dock()
        self.led('off')

    # ================================================================ commands
    def _on_cmd(self, msg: String):
        try:
            cmd = json.loads(msg.data)
        except ValueError:
            return
        cid = cmd.get('id')
        try:
            self.handle_cmd(cmd)
        except TaskFailed as e:
            self.ack(cid, False, e.reason)
        except Exception as e:  # noqa: BLE001
            self.get_logger().error(f'command {cmd.get("t")} failed: {e!r}')
            self.ack(cid, False, 'internal_error')

    def handle_cmd(self, cmd: dict):
        t = cmd.get('t')
        cid = cmd.get('id')
        no_ack = bool(cmd.get('_no_ack'))
        ok = (lambda: None) if no_ack else (lambda: self.ack(cid, True))

        if t == 'teleop_activity':                 # internal hint from the bridge
            self.last_teleop = self.now()
            if self.busy() and self._task_name not in ('feed',):
                self.cancel_task()
            if not self.busy():
                self.set_activity('teleop')
            return
        if t == 'estop':
            if cmd.get('engage'):
                self.cancel_task()
            if not cmd.get('_bridge_handled'):
                self.call('estop', Estop.Request(engage=bool(cmd.get('engage'))))
            return ok()
        if self.estopped and t in ('goto', 'play', 'feed', 'launch', 'map'):
            raise TaskFailed('estop_engaged')

        if t == 'feed':
            g, reason = self.sched.clamp_portion(float(cmd['grams']))
            if reason:
                raise TaskFailed(reason)
            if self.busy() and self._task_name == 'feed':
                raise TaskFailed('busy_feeding')
            self.start_task('feed', 'feeding', lambda: self.task_feed(g, None, go_to_poi=False))
            return ok()
        if t == 'feed_abort':
            self.call('feed_abort', Trigger.Request())
            if self._task_name == 'feed':
                self.cancel_task()
            return ok()
        if t == 'launch':
            if self.busy() and self._task_name not in ('play',):
                raise TaskFailed('busy')
            power = int(max(20, min(70, cmd.get('power', 40))))
            self.call('launch', Launch.Request(power_pct=power, count=int(cmd.get('count', 1))))
            return ok()
        if t == 'intake':
            self.call('intake', Intake.Request(mode=INTAKE[cmd['mode']]))
            return ok()
        if t == 'tease':
            ms = int(float(cmd.get('duration_s', 20)) * 1000)
            self.call('tease', Tease.Request(pattern=TEASE[cmd['pattern']], duration_ms=min(ms, 65535)))
            return ok()
        if t == 'led':
            col = cmd.get('color', '#FFFFFF').lstrip('#')
            v = int(col, 16)
            self.call('led', Led.Request(mode=LED[cmd['mode']], r=(v >> 16) & 255,
                                         g=(v >> 8) & 255, b=v & 255))
            return ok()
        if t == 'goto':
            tgt = cmd['target']
            if tgt == 'dock':
                if self.pois.get('dock') is None:
                    raise TaskFailed('no_dock_pose')
                self.start_task('dock', 'docking', self.task_dock,
                                on_fail=lambda r: self.emit('nav_failed', target='dock'))
            elif tgt == 'pet':
                self.start_task('seek_pet', 'seeking_pet', self.task_seek_pet,
                                on_fail=lambda r: self.emit('nav_failed', target='pet'))
            else:
                name = cmd['poi']
                if self.pois.get(name) is None:
                    raise TaskFailed('unknown_poi')
                self.start_task('goto', 'patrolling', lambda: self.task_goto_poi(name),
                                on_fail=lambda r: self.emit('nav_failed', target=name))
            return ok()
        if t == 'mode':
            self.set_mode(cmd['mode'])
            return ok()
        if t == 'play':
            game = cmd['game']
            dur = float(cmd.get('duration_s', 300))
            self.start_task('play', 'playing', lambda: self.task_play(game, dur),
                            then_dock=bool(self.get_parameter('return_to_dock_after_task').value))
            return ok()
        if t == 'save_poi':
            p = self.pose()
            if p is None or not p['localized']:
                raise TaskFailed('not_localized')
            self.pois.set(cmd['name'], p['x'], p['y'], p['th'])
            return ok()
        if t == 'map':
            self.handle_map(cmd['action'])
            return ok()
        raise TaskFailed(f'unsupported:{t}')

    def set_mode(self, mode: str):
        prev = self.mode
        self.mode = mode
        if mode == 'sleep':
            self.start_task('sleep', 'docking' if not self.docked else 'sleeping', self.task_sleep)
        elif mode == 'manual':
            self.cancel_task()
            self.set_activity('idle')
        elif mode == 'auto' and prev == 'sleep':
            self.led('breathe')
            self.set_activity('idle')

    # ---------------------------------------------------------------- mapping
    def _start_slam(self, mode: str):
        if self.slam is None:
            return
        eff = self.slam.start(mode)
        self.nav['mapping'] = eff == 'mapping'

    def _slam_call(self, srv_type, name: str, req, timeout: float = 30.0):
        cli = self.create_client(srv_type, name, callback_group=self.cbg)
        try:
            if not cli.wait_for_service(timeout_sec=5.0):
                raise TaskFailed(f'{name}_unavailable')
            fut = cli.call_async(req)
            ev = threading.Event()
            fut.add_done_callback(lambda _f: ev.set())
            if not ev.wait(timeout):
                raise TaskFailed(f'{name}_timeout')
            return fut.result()
        finally:
            self.destroy_client(cli)

    def save_map(self):
        from slam_toolbox.srv import SaveMap, SerializePoseGraph
        base = self.get_parameter('map_base').value
        os.makedirs(os.path.dirname(base), exist_ok=True)
        r = self._slam_call(SerializePoseGraph, '/slam_toolbox/serialize_map',
                            SerializePoseGraph.Request(filename=base))
        if getattr(r, 'result', 0) != 0:
            raise TaskFailed('serialize_failed')
        req = SaveMap.Request()
        req.name.data = base
        self._slam_call(SaveMap, '/slam_toolbox/save_map', req)   # .pgm/.yaml for viewers

    def handle_map(self, action: str):
        if action == 'start_mapping':
            self.cancel_task()
            self._start_slam('mapping')
            self.set_activity('mapping')
        elif action == 'save':
            self.save_map()
        elif action == 'stop_mapping':
            if not self.nav['mapping']:
                raise TaskFailed('not_mapping')
            self.save_map()
            self._start_slam('localization')
            self.set_activity('idle')
        elif action == 'clear':
            self.cancel_task()
            if self.slam:
                self.slam.stop()
                self.slam.delete_map()
            self.pois.clear()          # POIs are meaningless without their map
            self._start_slam('mapping')
            self.set_activity('mapping')

    # ================================================================ inputs
    def _on_config(self, msg: String):
        try:
            cfg = json.loads(msg.data)
        except ValueError:
            return
        if isinstance(cfg, dict):
            self.sched.set_config(cfg)
            self.get_logger().info('config updated')

    def _on_detections(self, msg: String):
        try:
            det = json.loads(msg.data)
        except ValueError:
            return
        prev_seen = self.pets.last_seen_ts
        events = self.pets.update(det, self.now())
        if self.pets.last_seen_ts != prev_seen:
            # remember robot yaw at detection time to compensate bearing later
            self.pet_yaw_at_det = self.odom_yaw
        # pet_detected is raised by pm-core's detector (rate-limited, also drives
        # auto clips); re-emitting it here would duplicate it on app/cloud.
        del events

    def _on_hw(self, msg: HwState):
        prev_docked = self.docked
        self.hw = msg
        now = self.now()
        for e in self.flag_edges.update(msg.flags, now):
            self.pub_event.publish(String(data=json.dumps(e)))
        for e in self.battery.update(msg.soc_pct, msg.charging, msg.docked, now):
            self.pub_event.publish(String(data=json.dumps(e)))
            self._on_battery_low()
        if msg.docked and not prev_docked and self.get_parameter('auto_save_dock_pose').value:
            p = self.pose()
            if p and p['localized']:
                self.pois.set('dock', p['x'], p['y'], p['th'])
                self.get_logger().info('dock pose updated from contact')

    def _on_battery_low(self):
        if self.teleop_recent() or self.docked or self.pois.get('dock') is None:
            return
        if self._task_name == 'dock':
            return
        self.start_task('dock', 'docking', self.task_dock,
                        on_fail=lambda r: self.emit('nav_failed', target='dock'))

    def _on_mcu_event(self, msg: McuEvent):
        now = self.now()
        for e in E.mcu_event_to_events(msg.code, msg.data, now):
            if e['t'] in ('cliff', 'stuck') and not self.rate.allow(e['t'], now):
                continue
            self.pub_event.publish(String(data=json.dumps(e)))
        if msg.code in (E.BATTERY_LOW, E.BATTERY_CRITICAL):
            soc = self.hw.soc_pct if self.hw else int(msg.data)
            for e in self.battery.update(soc, False, self.docked, now, mcu_low=True):
                self.pub_event.publish(String(data=json.dumps(e)))
            self._on_battery_low()
        elif msg.code == E.BUTTON_SHORT:
            if self.mode != 'sleep' and not self.busy():
                game = self.sched.next_game() or 'teaser'
                self.start_task('play', 'playing', lambda: self.task_play(game, 120.0),
                                then_dock=bool(self.get_parameter('return_to_dock_after_task').value))
        elif msg.code == E.BUTTON_LONG:
            if self.busy():
                self.cancel_task()
            else:
                self.set_mode('auto' if self.mode == 'sleep' else 'sleep')
        elif msg.code in (E.CLIFF, E.WHEEL_DROP, E.MOTOR_FAULT):
            if self.busy() and self._task_name not in ('feed', 'dock'):
                self.cancel_task()

    def _on_scan(self, msg: LaserScan):
        self.scan = msg

    def _on_odom(self, msg: Odometry):
        q = msg.pose.pose.orientation
        self.odom_yaw = yaw_from_quat(q.x, q.y, q.z, q.w)

    # ================================================================ timers
    def _tick_safety(self):
        lock = bool(self.hw and (self.hw.estop or self.hw.wheel_drop or self.hw.motor_fault))
        self.pub_lock.publish(Bool(data=lock))
        if self.activity == 'teleop' and not self.teleop_recent(1.0) and not self.busy():
            self.set_activity('sleeping' if self.mode == 'sleep' else
                              ('mapping' if self.nav['mapping'] else 'idle'))
        if self.hw is not None and not self.hw.mcu_connected and not self.busy():
            self.activity = 'error'
        elif self.activity == 'error' and self.hw is not None and self.hw.mcu_connected:
            self.activity = 'idle'

    def _tick_auto(self):
        try:
            self._auto_step()
        except Exception as e:  # noqa: BLE001
            self.get_logger().error(f'auto step: {e!r}')

    def _auto_step(self):
        # Scheduler: meals in every mode (sleep wakes for meals), play only in auto.
        for due in self.sched.poll():
            if due.kind == 'meal':
                if not due.runnable:
                    if due.skip_reason != 'invalid_grams':
                        self.emit('meal_failed', reason=due.skip_reason)
                    self.sched.mark_handled(due.key)
                    self._save_sched_state()
                    continue
                if self.estopped or self.teleop_recent(30.0) or self.nav['mapping'] or \
                        (self.busy() and self._task_name == 'feed'):
                    continue                    # retry while inside grace window
                g, key = due.grams, due.key
                self.start_task('feed', 'feeding',
                                lambda g=g, key=key, nav=(self.mode != 'manual'):
                                self.task_feed(g, key, go_to_poi=nav),
                                then_dock=bool(self.get_parameter('return_to_dock_after_task').value))
                return
            if due.kind == 'play':
                if not due.runnable or self.mode != 'auto':
                    self.sched.mark_handled(due.key)
                    self._save_sched_state()
                    continue
                if self.busy() or self.estopped or self.nav['mapping'] or self.battery.latched:
                    continue
                d, key = due.duration_s, due.key
                self.start_task('play', 'playing',
                                lambda d=d, key=key: self.task_scheduled_play(d, key),
                                then_dock=bool(self.get_parameter('return_to_dock_after_task').value))
                return

        if self.mode != 'auto' or self.busy() or self.estopped or self.nav['mapping']:
            return
        if self.teleop_recent(60.0) or self.battery.latched:
            return
        if self.sched.patrol_due(self.last_patrol_ts) and self.pois.names():
            self.start_task('patrol', 'patrolling', self.task_patrol,
                            then_dock=True)
            return
        idle_s = float(self.get_parameter('idle_return_s').value)
        if not self.docked and self.pois.get('dock') and self.now() - self.last_activity_ts > idle_s:
            self.start_task('dock', 'docking', self.task_dock,
                            on_fail=lambda r: self.emit('nav_failed', target='dock'))

    def _publish_state(self):
        pet = {'last_seen_ts': self.pets.last_seen_ts, 'camera': self.pets.camera,
               'bearing_rad': self.pets.bearing}
        st = {'mode': self.mode, 'activity': self.activity,
              'today_g': self.sched.today_g, 'last_meal': self.sched.state.last_meal,
              'nav': dict(self.nav), 'pet': pet, 'pois': self.pois.names()}
        self.pub_state.publish(String(data=json.dumps(st)))

    def destroy_node(self):
        self.cancel_task(1.0)
        if self.slam:
            self.slam.stop()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = BehaviorNode()
    ex = MultiThreadedExecutor(num_threads=4)
    ex.add_node(node)
    try:
        ex.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
