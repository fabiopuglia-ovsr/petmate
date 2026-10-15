"""Nav2 servers for Petmate (no AMCL/map_server: slam_toolbox provides map->odom and /map).

Velocity chain: controller_server / behavior_server -> cmd_vel_nav_raw ->
velocity_smoother -> cmd_vel_nav -> twist_mux (priority 'navigation').
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

LIFECYCLE_NODES = ['controller_server', 'smoother_server', 'planner_server',
                   'behavior_server', 'velocity_smoother', 'bt_navigator']


def _setup(context):
    cfg = os.path.join(get_package_share_directory('petmate_bringup'), 'config')
    params = [os.path.join(cfg, 'nav2_params.yaml')]
    if LaunchConfiguration('controller').perform(context) == 'rpp':
        params.append(os.path.join(cfg, 'controller_rpp.yaml'))
    log = LaunchConfiguration('log_level').perform(context)
    common = dict(output='screen', respawn=True, respawn_delay=2.0,
                  arguments=['--ros-args', '--log-level', log])
    return [
        Node(package='nav2_controller', executable='controller_server', parameters=params,
             remappings=[('cmd_vel', 'cmd_vel_nav_raw')], **common),
        Node(package='nav2_smoother', executable='smoother_server', parameters=params, **common),
        Node(package='nav2_planner', executable='planner_server', parameters=params, **common),
        Node(package='nav2_behaviors', executable='behavior_server', parameters=params,
             remappings=[('cmd_vel', 'cmd_vel_nav_raw')], **common),
        Node(package='nav2_velocity_smoother', executable='velocity_smoother', parameters=params,
             remappings=[('cmd_vel', 'cmd_vel_nav_raw'), ('cmd_vel_smoothed', 'cmd_vel_nav')],
             **common),
        Node(package='nav2_bt_navigator', executable='bt_navigator', parameters=params, **common),
        Node(package='nav2_lifecycle_manager', executable='lifecycle_manager',
             name='lifecycle_manager_navigation', output='screen',
             parameters=[{'autostart': True, 'node_names': LIFECYCLE_NODES,
                          'bond_timeout': 4.0, 'attempt_respawn_reconnection': True}]),
    ]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('controller', default_value='mppi', choices=['mppi', 'rpp']),
        DeclareLaunchArgument('log_level', default_value='info'),
        OpaqueFunction(function=_setup),
    ])
