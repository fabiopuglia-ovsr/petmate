"""Petmate full bringup.

ros2 launch petmate_bringup bringup.launch.py lidar:=l2|mid360 mode:=mapping|localization \
    map:=/var/lib/petmate/maps/home controller:=mppi|rpp

Graph:
  petmate_base (MCU) --/odom,/imu/data_raw--> ekf (odom->base_footprint)
  LiDAR --/points--> pointcloud_to_laserscan --/scan--> slam_toolbox (map->odom), Nav2 costmaps
  /points --> Nav2 voxel layers
  twist_mux: cmd_vel_safety(200) > cmd_vel_teleop(100) > cmd_vel_docking(50) > cmd_vel_nav(10)
             + lock petmate/safety_lock(255)  --> /cmd_vel --> petmate_base
  petmate_bridge <-ZMQ-> pm-core ; petmate_behavior (scheduler, games, docking, SLAM manager)
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, TimerAction
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


def generate_launch_description():
    bringup = get_package_share_directory('petmate_bringup')
    desc = get_package_share_directory('petmate_description')
    cfg = os.path.join(bringup, 'config')
    L = LaunchConfiguration

    args = [
        DeclareLaunchArgument('lidar', default_value='l2', choices=['l2', 'mid360']),
        DeclareLaunchArgument('mode', default_value='localization',
                              choices=['mapping', 'localization']),
        DeclareLaunchArgument('map', default_value='/var/lib/petmate/maps/home',
                              description='slam_toolbox serialized map base path (no extension)'),
        DeclareLaunchArgument('controller', default_value='mppi', choices=['mppi', 'rpp']),
        DeclareLaunchArgument('use_nav', default_value='true'),
        DeclareLaunchArgument('slam_managed', default_value='true',
                              description='true: petmate_behavior owns slam_toolbox (runtime '
                                          'map commands); false: launched here statically'),
        DeclareLaunchArgument('lidar_ip', default_value='192.168.1.62'),
        DeclareLaunchArgument('host_ip', default_value='192.168.1.2'),
        DeclareLaunchArgument('lidar_roll', default_value='0.0'),
        DeclareLaunchArgument('lidar_pitch', default_value='0.0'),
        DeclareLaunchArgument('lidar_yaw', default_value='0.0'),
        DeclareLaunchArgument('cmd_vel_stamped', default_value='false'),
    ]

    description = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(desc, 'launch', 'description.launch.py')),
        launch_arguments={'lidar_roll': L('lidar_roll'), 'lidar_pitch': L('lidar_pitch'),
                          'lidar_yaw': L('lidar_yaw')}.items())

    lidar = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(bringup, 'launch', 'lidar.launch.py')),
        launch_arguments={'lidar': L('lidar'), 'lidar_ip': L('lidar_ip'),
                          'host_ip': L('host_ip')}.items())

    petmate_params = os.path.join(cfg, 'petmate.yaml')

    base = Node(package='petmate_base', executable='base_node', name='petmate_base',
                output='screen', respawn=True, respawn_delay=1.0,
                parameters=[petmate_params, {'cmd_vel_stamped': PythonExpression(
                    ["'", L('cmd_vel_stamped'), "' == 'true'"])}])

    ekf = Node(package='robot_localization', executable='ekf_node', name='ekf_filter_node',
               output='screen', parameters=[os.path.join(cfg, 'ekf.yaml')])

    p2l = Node(package='pointcloud_to_laserscan', executable='pointcloud_to_laserscan_node',
               name='pointcloud_to_laserscan', output='screen',
               parameters=[os.path.join(cfg, 'pointcloud_to_laserscan.yaml')],
               remappings=[('cloud_in', '/points'), ('scan', '/scan')])

    mux = Node(package='twist_mux', executable='twist_mux', name='twist_mux', output='screen',
               parameters=[os.path.join(cfg, 'twist_mux.yaml')],
               remappings=[('cmd_vel_out', 'cmd_vel')])

    bridge = Node(package='petmate_bridge', executable='bridge_node', name='petmate_bridge',
                  output='screen', respawn=True, respawn_delay=1.0, parameters=[petmate_params])

    behavior = Node(package='petmate_behavior', executable='behavior_node',
                    name='petmate_behavior', output='screen', respawn=True, respawn_delay=2.0,
                    parameters=[petmate_params, {
                        'slam_managed': PythonExpression(["'", L('slam_managed'), "' == 'true'"]),
                        'slam_mode': L('mode'),
                        'map_base': L('map'),
                    }])

    slam_static = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(bringup, 'launch', 'slam.launch.py')),
        launch_arguments={'mode': L('mode'), 'map_file': L('map')}.items(),
        condition=UnlessCondition(L('slam_managed')))

    nav2 = TimerAction(period=5.0, actions=[IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(bringup, 'launch', 'nav2.launch.py')),
        launch_arguments={'controller': L('controller')}.items(),
        condition=IfCondition(L('use_nav')))])

    return LaunchDescription(args + [description, lidar, base, ekf, p2l, mux, bridge,
                                     behavior, slam_static, nav2])
