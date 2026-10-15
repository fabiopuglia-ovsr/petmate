"""slam_toolbox in mapping or localization mode (lifecycle node, auto-activated).

Normally started/stopped at runtime by petmate_behavior (SlamManager) so that
pm-core map commands can switch modes; can also be included directly.
"""
import os

import launch
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, EmitEvent, RegisterEventHandler
from launch.conditions import LaunchConfigurationEquals
from launch.events import matches_action
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import LifecycleNode
from launch_ros.event_handlers import OnStateTransition
from launch_ros.events.lifecycle import ChangeState
from lifecycle_msgs.msg import Transition


def _slam_node(mode: str, executable: str, params_file: str, map_file):
    extra = {'use_lifecycle_manager': False, 'use_sim_time': False}
    if mode == 'localization':
        extra['map_file_name'] = map_file
    node = LifecycleNode(
        condition=LaunchConfigurationEquals('mode', mode),
        package='slam_toolbox',
        executable=executable,
        name='slam_toolbox',
        namespace='',
        output='screen',
        parameters=[params_file, extra],
    )
    configure = EmitEvent(
        condition=LaunchConfigurationEquals('mode', mode),
        event=ChangeState(lifecycle_node_matcher=matches_action(node),
                          transition_id=Transition.TRANSITION_CONFIGURE))
    activate = RegisterEventHandler(
        OnStateTransition(
            target_lifecycle_node=node, start_state='configuring', goal_state='inactive',
            entities=[launch.actions.LogInfo(msg='[slam] activating'),
                      EmitEvent(event=ChangeState(
                          lifecycle_node_matcher=matches_action(node),
                          transition_id=Transition.TRANSITION_ACTIVATE))]),
        condition=LaunchConfigurationEquals('mode', mode))
    return [node, configure, activate]


def generate_launch_description():
    cfg = os.path.join(get_package_share_directory('petmate_bringup'), 'config')
    map_file = LaunchConfiguration('map_file')
    ents = [
        DeclareLaunchArgument('mode', default_value='localization',
                              choices=['mapping', 'localization']),
        DeclareLaunchArgument('map_file', default_value='/var/lib/petmate/maps/home',
                              description='serialized pose-graph base path (no extension)'),
    ]
    ents += _slam_node('mapping', 'async_slam_toolbox_node',
                       os.path.join(cfg, 'slam_toolbox_mapping.yaml'), map_file)
    ents += _slam_node('localization', 'localization_slam_toolbox_node',
                       os.path.join(cfg, 'slam_toolbox_localization.yaml'), map_file)
    return LaunchDescription(ents)
