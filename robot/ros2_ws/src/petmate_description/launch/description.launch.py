"""robot_state_publisher for Petmate."""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import Command, LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    pkg = get_package_share_directory('petmate_description')
    xacro_file = os.path.join(pkg, 'urdf', 'petmate.urdf.xacro')
    use_sim_time = LaunchConfiguration('use_sim_time')
    robot_description = ParameterValue(
        Command(['xacro ', xacro_file,
                 ' lidar_roll:=', LaunchConfiguration('lidar_roll'),
                 ' lidar_pitch:=', LaunchConfiguration('lidar_pitch'),
                 ' lidar_yaw:=', LaunchConfiguration('lidar_yaw')]),
        value_type=str)
    return LaunchDescription([
        DeclareLaunchArgument('use_sim_time', default_value='false'),
        DeclareLaunchArgument('lidar_roll', default_value='0.0'),
        DeclareLaunchArgument('lidar_pitch', default_value='0.0'),
        DeclareLaunchArgument('lidar_yaw', default_value='0.0'),
        Node(
            package='robot_state_publisher',
            executable='robot_state_publisher',
            name='robot_state_publisher',
            output='screen',
            parameters=[{'robot_description': robot_description,
                         'use_sim_time': use_sim_time,
                         'publish_frequency': 20.0}],
        ),
        # Wheel joints are continuous: publish zero joint states so TF for the
        # wheel links exists (purely cosmetic; odometry comes from the MCU).
        Node(
            package='joint_state_publisher',
            executable='joint_state_publisher',
            name='joint_state_publisher',
            output='screen',
            parameters=[{'use_sim_time': use_sim_time}],
        ),
    ])
