"""petmate_base: ROS 2 driver for the Petmate MCU (RP2350) over USB CDC.

Protocol: INTERFACES.md §2 (see protocol.py for the pure codec).
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy

from geometry_msgs.msg import Twist, TwistStamped, TransformStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Imu, BatteryState
from std_srvs.srv import Trigger
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from tf2_ros import TransformBroadcaster

from petmate_interfaces.msg import HwState, FeedStatus, LaunchStatus, McuEvent
from petmate_interfaces.srv import Feed, Launch, Intake, Tease, Led, Estop

from petmate_base import protocol as P
from petmate_base.odometry import DiffDriveOdometry

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    serial = None


class PetmateBase(Node):
    def __init__(self):
        super().__init__('petmate_base')
        dp = self.declare_parameter
        dp('port', '/dev/petmate_mcu')
        dp('base_frame', 'base_footprint')
        dp('odom_frame', 'odom')
        dp('imu_frame', 'imu_link')
        dp('publish_tf', False)            # EKF publishes odom->base_footprint
        dp('odom_source', 'mcu')           # 'mcu' (MCU-integrated pose) | 'encoders'
        dp('cmd_vel_stamped', False)       # True if /cmd_vel is TwistStamped
        dp('cmd_rate_hz', 20.0)            # CMD_VEL refresh (MCU watchdog 250 ms)
        dp('cmd_timeout_s', 0.5)           # zero velocity if /cmd_vel stale
        dp('ping_period_s', 1.0)
        dp('reconnect_period_s', 2.0)
        dp('push_params_on_connect', True)
        dp('ticks_per_rev', 1320.0)
        dp('wheel_radius_m', 0.036)
        dp('track_m', 0.260)
        dp('v_max', 0.50)                  # hard cap (teleop limit, §1)
        dp('w_max', 2.0)
        dp('a_max', 0.6)
        dp('battery_design_capacity_ah', 0.0)
        dp('hw_state_rate_hz', 10.0)

        gp = lambda n: self.get_parameter(n).value  # noqa: E731
        self.port = gp('port')
        self.base_frame = gp('base_frame')
        self.odom_frame = gp('odom_frame')
        self.imu_frame = gp('imu_frame')
        self.publish_tf = bool(gp('publish_tf'))
        self.odom_source = gp('odom_source')
        self.v_max = float(gp('v_max'))
        self.w_max = float(gp('w_max'))
        self.cmd_timeout = float(gp('cmd_timeout_s'))

        self._enc_odom = DiffDriveOdometry(gp('ticks_per_rev'), gp('wheel_radius_m'),
                                           gp('track_m'))
        self._seq = P.SeqCounter()
        self._ser = None
        self._ser_lock = threading.Lock()
        self._cmd = (0.0, 0.0)
        self._cmd_t = 0.0
        self._fw = ''
        self._last_state: Optional[P.State] = None
        self._last_state_wall = 0.0
        self._reader = P.FrameReader()
        self._stop = threading.Event()

        # Publishers
        sensor_qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT,
                                history=HistoryPolicy.KEEP_LAST)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self.pub_odom = self.create_publisher(Odometry, 'odom', 10)
        self.pub_imu = self.create_publisher(Imu, 'imu/data_raw', sensor_qos)
        self.pub_batt = self.create_publisher(BatteryState, 'battery_state', 10)
        self.pub_hw = self.create_publisher(HwState, 'petmate/hw_state', 10)
        self.pub_feed = self.create_publisher(FeedStatus, 'petmate/feed_status', latched)
        self.pub_launch = self.create_publisher(LaunchStatus, 'petmate/launch_status', latched)
        self.pub_event = self.create_publisher(McuEvent, 'petmate/mcu_event', 20)
        self.pub_diag = self.create_publisher(DiagnosticArray, 'diagnostics', 10)
        self.tf_br = TransformBroadcaster(self) if self.publish_tf else None

        # Subscribers
        if gp('cmd_vel_stamped'):
            self.create_subscription(TwistStamped, 'cmd_vel',
                                     lambda m: self._on_cmd(m.twist), 10)
        else:
            self.create_subscription(Twist, 'cmd_vel', self._on_cmd, 10)

        # Services
        ns = 'petmate/'
        self.create_service(Feed, ns + 'feed', self._srv_feed)
        self.create_service(Trigger, ns + 'feed_abort', self._srv_simple(P.FEED_ABORT))
        self.create_service(Launch, ns + 'launch', self._srv_launch)
        self.create_service(Intake, ns + 'intake', self._srv_intake)
        self.create_service(Tease, ns + 'tease', self._srv_tease)
        self.create_service(Led, ns + 'led', self._srv_led)
        self.create_service(Estop, ns + 'estop', self._srv_estop)
        self.create_service(Trigger, ns + 'reset_odom', self._srv_reset_odom)
        self.create_service(Trigger, ns + 'tare', self._srv_simple(P.TARE_SCALE))

        # Timers
        self.create_timer(1.0 / float(gp('cmd_rate_hz')), self._tick_cmd)
        self.create_timer(float(gp('ping_period_s')), self._tick_ping)
        self.create_timer(1.0 / float(gp('hw_state_rate_hz')), self._tick_hw)
        self.create_timer(1.0, self._tick_diag)

        self._thread = threading.Thread(target=self._io_loop, daemon=True)
        self._thread.start()
        self.get_logger().info(f'petmate_base started, port={self.port}')

    # ------------------------------------------------------------ serial I/O
    def _open(self) -> bool:
        if serial is None:
            self.get_logger().error('pyserial not installed')
            return False
        try:
            s = serial.Serial(self.port, 115200, timeout=0.05)
            s.reset_input_buffer()
        except Exception as e:  # noqa: BLE001
            self.get_logger().warn(f'cannot open {self.port}: {e}',
                                   throttle_duration_sec=10.0)
            return False
        with self._ser_lock:
            self._ser = s
        self._reader = P.FrameReader()
        self.get_logger().info(f'MCU connected on {self.port}')
        self.send(P.PING)
        if self.get_parameter('push_params_on_connect').value:
            self._push_params()
        return True

    def _close(self):
        with self._ser_lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:  # noqa: BLE001
                    pass
            self._ser = None

    def _push_params(self):
        gp = lambda n: float(self.get_parameter(n).value)  # noqa: E731
        for pid, name in ((P.PARAM_TICKS_PER_REV, 'ticks_per_rev'),
                          (P.PARAM_WHEEL_RADIUS_M, 'wheel_radius_m'),
                          (P.PARAM_TRACK_M, 'track_m'),
                          (P.PARAM_V_MAX, 'v_max'),
                          (P.PARAM_W_MAX, 'w_max'),
                          (P.PARAM_A_MAX, 'a_max')):
            self.send(P.SET_PARAM, P.enc_set_param(pid, gp(name)))

    def connected(self) -> bool:
        return self._ser is not None

    def send(self, ptype: int, payload: bytes = b'') -> bool:
        frame = P.build_frame(ptype, self._seq.next(), payload)
        with self._ser_lock:
            if self._ser is None:
                return False
            try:
                self._ser.write(frame)
                return True
            except Exception as e:  # noqa: BLE001
                self.get_logger().error(f'serial write failed: {e}')
                try:
                    self._ser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._ser = None
                return False

    def _io_loop(self):
        period = float(self.get_parameter('reconnect_period_s').value)
        while not self._stop.is_set() and rclpy.ok():
            if self._ser is None:
                if not self._open():
                    time.sleep(period)
                    continue
            try:
                s = self._ser
                data = s.read(s.in_waiting or 1) if s is not None else b''
            except Exception as e:  # noqa: BLE001
                self.get_logger().error(f'serial read failed: {e}')
                self._close()
                continue
            if not data:
                continue
            for ptype, _seq, payload in self._reader.feed(data):
                try:
                    obj = P.decode_payload(ptype, payload)
                except ValueError as e:
                    self.get_logger().debug(f'decode error: {e}')
                    self._reader.errors += 1
                    continue
                try:
                    self._dispatch(ptype, obj)
                except Exception as e:  # noqa: BLE001
                    self.get_logger().error(f'dispatch error: {e}')

    def _dispatch(self, ptype, obj):
        if ptype == P.STATE:
            self._on_state(obj)
        elif ptype == P.FEED_STATUS:
            m = FeedStatus()
            m.header.stamp = self.get_clock().now().to_msg()
            m.state, m.state_name = obj.state, obj.state_name
            m.dispensed_g, m.hopper_g = float(obj.dispensed_g), float(obj.hopper_g)
            self.pub_feed.publish(m)
        elif ptype == P.LAUNCH_STATUS:
            m = LaunchStatus()
            m.header.stamp = self.get_clock().now().to_msg()
            m.state, m.state_name, m.balls = obj.state, obj.state_name, obj.balls
            self.pub_launch.publish(m)
        elif ptype == P.EVENT:
            m = McuEvent()
            m.header.stamp = self.get_clock().now().to_msg()
            m.code, m.name, m.data = obj.code, obj.name, obj.data
            self.pub_event.publish(m)
            self.get_logger().info(f'MCU event {obj.name} data={obj.data}')
        elif ptype == P.PONG:
            self._fw = obj.fw
        elif ptype == P.LOG:
            self.get_logger().info(f'[mcu] {obj}')

    # ------------------------------------------------------------ STATE
    def _on_state(self, s: P.State):
        now = self.get_clock().now()
        stamp = now.to_msg()
        self._last_state = s
        self._last_state_wall = time.monotonic()

        if self.odom_source == 'encoders':
            x, y, th, v, w = self._enc_odom.update(s.enc_l, s.enc_r, s.t_ms / 1000.0)
        else:
            x, y, th, v, w = s.x, s.y, s.th, s.v, s.w

        qx, qy, qz, qw = P.yaw_to_quat(th)
        od = Odometry()
        od.header.stamp = stamp
        od.header.frame_id = self.odom_frame
        od.child_frame_id = self.base_frame
        od.pose.pose.position.x = float(x)
        od.pose.pose.position.y = float(y)
        od.pose.pose.orientation.z = qz
        od.pose.pose.orientation.w = qw
        od.twist.twist.linear.x = float(v)
        od.twist.twist.angular.z = float(w)
        pc = [0.0] * 36
        pc[0] = pc[7] = 0.01
        pc[14] = pc[21] = pc[28] = 1e6     # z, roll, pitch unobserved
        pc[35] = 0.05
        od.pose.covariance = pc
        tc = [0.0] * 36
        tc[0] = 0.002
        tc[7] = 1e-6                        # non-holonomic: vy ~ 0
        tc[14] = tc[21] = tc[28] = 1e6
        tc[35] = 0.01
        od.twist.covariance = tc
        self.pub_odom.publish(od)

        if self.tf_br is not None:
            t = TransformStamped()
            t.header.stamp = stamp
            t.header.frame_id = self.odom_frame
            t.child_frame_id = self.base_frame
            t.transform.translation.x = float(x)
            t.transform.translation.y = float(y)
            t.transform.rotation.z = qz
            t.transform.rotation.w = qw
            self.tf_br.sendTransform(t)

        imu = Imu()
        imu.header.stamp = stamp
        imu.header.frame_id = self.imu_frame
        imu.orientation_covariance[0] = -1.0   # no orientation estimate
        ax, ay, az = s.accel_m_s2()
        gx, gy, gz = s.gyro_rad_s()
        imu.linear_acceleration.x, imu.linear_acceleration.y, imu.linear_acceleration.z = ax, ay, az
        imu.angular_velocity.x, imu.angular_velocity.y, imu.angular_velocity.z = gx, gy, gz
        imu.angular_velocity_covariance = [2e-4, 0.0, 0.0, 0.0, 2e-4, 0.0, 0.0, 0.0, 2e-4]
        imu.linear_acceleration_covariance = [0.02, 0.0, 0.0, 0.0, 0.02, 0.0, 0.0, 0.0, 0.02]
        self.pub_imu.publish(imu)

    def _tick_hw(self):
        s = self._last_state
        now = self.get_clock().now().to_msg()
        hw = HwState()
        hw.header.stamp = now
        hw.header.frame_id = self.base_frame
        hw.fw = self._fw
        hw.mcu_connected = self.connected() and (time.monotonic() - self._last_state_wall) < 0.5
        if s is not None:
            hw.mcu_t_ms = s.t_ms
            hw.enc_l, hw.enc_r = s.enc_l, s.enc_r
            hw.cliff_mask, hw.bump_mask, hw.flags = s.cliff_mask, s.bump_mask, s.flags
            hw.estop, hw.wheel_drop, hw.motor_fault = s.estop, s.wheel_drop, s.motor_fault
            hw.charging, hw.docked = s.charging, s.docked
            hw.button_pressed, hw.watchdog_tripped = s.button_pressed, s.watchdog_tripped
            hw.batt_v = s.batt_mv / 1000.0
            hw.batt_a = s.batt_ma / 1000.0
            hw.soc_pct = s.soc_pct
            b = BatteryState()
            b.header.stamp = now
            b.voltage = hw.batt_v
            b.current = hw.batt_a
            b.percentage = max(0.0, min(1.0, s.soc_pct / 100.0))
            b.charge = float('nan')
            b.capacity = float('nan')
            cap = float(self.get_parameter('battery_design_capacity_ah').value)
            b.design_capacity = cap if cap > 0 else float('nan')
            b.temperature = float('nan')
            if s.charging:
                b.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_CHARGING
            elif s.docked and s.soc_pct >= 99:
                b.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_FULL
            else:
                b.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
            b.power_supply_technology = BatteryState.POWER_SUPPLY_TECHNOLOGY_LION
            b.present = True
            self.pub_batt.publish(b)
        self.pub_hw.publish(hw)

    def _tick_diag(self):
        d = DiagnosticArray()
        d.header.stamp = self.get_clock().now().to_msg()
        st = DiagnosticStatus()
        st.name = 'petmate_base: MCU link'
        st.hardware_id = self.port
        ok = self.connected() and (time.monotonic() - self._last_state_wall) < 0.5
        st.level = DiagnosticStatus.OK if ok else DiagnosticStatus.ERROR
        st.message = 'ok' if ok else 'no STATE from MCU'
        st.values = [KeyValue(key='fw', value=self._fw),
                     KeyValue(key='frames', value=str(self._reader.frames)),
                     KeyValue(key='frame_errors', value=str(self._reader.errors))]
        d.status.append(st)
        self.pub_diag.publish(d)

    # ------------------------------------------------------------ cmd_vel
    def _on_cmd(self, tw: Twist):
        self._cmd = P.limit_twist(tw.linear.x, tw.angular.z, self.v_max, self.w_max)
        self._cmd_t = time.monotonic()

    def _tick_cmd(self):
        # Always send CMD_VEL to keep the 250 ms MCU watchdog fed; zero when stale.
        if time.monotonic() - self._cmd_t > self.cmd_timeout:
            v, w = 0.0, 0.0
        else:
            v, w = self._cmd
        self.send(P.CMD_VEL, P.enc_cmd_vel(v, w))

    def _tick_ping(self):
        self.send(P.PING)

    # ------------------------------------------------------------ services
    def _result(self, resp, ok: bool, msg: str = ''):
        resp.success = ok
        resp.message = msg if msg else ('ok' if ok else 'MCU not connected')
        return resp

    def _srv_simple(self, ptype):
        def cb(_req, resp):
            return self._result(resp, self.send(ptype))
        return cb

    def _srv_feed(self, req, resp):
        if not (0.0 < req.grams <= 60.0):
            return self._result(resp, False, 'grams must be in (0, 60]')
        return self._result(resp, self.send(P.FEED, P.enc_feed(req.grams)))

    def _srv_launch(self, req, resp):
        return self._result(resp, self.send(P.LAUNCH, P.enc_launch(req.power_pct, req.count)))

    def _srv_intake(self, req, resp):
        try:
            payload = P.enc_intake(req.mode)
        except ValueError as e:
            return self._result(resp, False, str(e))
        return self._result(resp, self.send(P.INTAKE, payload))

    def _srv_tease(self, req, resp):
        try:
            payload = P.enc_tease(req.pattern, req.duration_ms)
        except ValueError as e:
            return self._result(resp, False, str(e))
        return self._result(resp, self.send(P.TEASE, payload))

    def _srv_led(self, req, resp):
        try:
            payload = P.enc_led(req.mode, req.r, req.g, req.b)
        except ValueError as e:
            return self._result(resp, False, str(e))
        return self._result(resp, self.send(P.LED, payload))

    def _srv_estop(self, req, resp):
        if req.engage:
            self._cmd = (0.0, 0.0)
        return self._result(resp, self.send(P.ESTOP, P.enc_estop(req.engage)))

    def _srv_reset_odom(self, _req, resp):
        self._enc_odom.reset()
        return self._result(resp, self.send(P.RESET_ODOM))

    def destroy_node(self):
        self._stop.set()
        # best-effort stop
        self.send(P.CMD_VEL, P.enc_cmd_vel(0.0, 0.0))
        self._close()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = PetmateBase()
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
