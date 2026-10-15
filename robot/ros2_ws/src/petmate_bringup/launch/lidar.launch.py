"""3D LiDAR driver. lidar:=l2 (Unitree 4D LiDAR L2, default) | mid360 (Livox Mid-360).

Both drivers are remapped to publish the cloud on /points (frame lidar_link)
and the LiDAR IMU on /lidar/imu (not fused by the EKF; available for LIO).

L2: Ethernet UDP, LiDAR 192.168.1.62:6101 -> host 192.168.1.2:6201 (§8,
factory defaults). NEGA work mode (360x96 deg) is the factory default and is
configured on the sensor itself. The parameter names below follow the
upstream unitree_lidar_ros2 launch.py; verify them against the pinned
unilidar_sdk2 commit (see docker/Dockerfile.ros) when bumping it.
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import LaunchConfigurationEquals
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    pkg = get_package_share_directory('petmate_bringup')
    lidar_ip = LaunchConfiguration('lidar_ip')
    host_ip = LaunchConfiguration('host_ip')

    l2 = Node(
        condition=LaunchConfigurationEquals('lidar', 'l2'),
        package='unitree_lidar_ros2',
        executable='unitree_lidar_ros2_node',
        name='unitree_lidar',
        output='screen',
        parameters=[{
            'initialize_type': 2,          # 1 = serial (UART TTL), 2 = UDP (Ethernet)
            'work_mode': 0,
            'use_system_timestamp': True,
            'range_min': 0.0,
            'range_max': 30.0,
            'cloud_scan_num': 18,          # vertical scans per published cloud (216 Hz / 18 = 12 Hz)
            'serial_port': '/dev/ttyACM0',
            'baudrate': 4000000,
            'lidar_port': 6101,
            'lidar_ip': lidar_ip,
            'local_port': 6201,
            'local_ip': host_ip,
            'cloud_frame': 'lidar_link',
            'cloud_topic': '/points',
            'imu_frame': 'lidar_link',
            'imu_topic': '/lidar/imu',
        }],
    )

    mid360 = Node(
        condition=LaunchConfigurationEquals('lidar', 'mid360'),
        package='livox_ros_driver2',
        executable='livox_ros_driver2_node',
        name='livox_lidar_publisher',
        output='screen',
        parameters=[{
            'xfer_format': 0,              # 0 = sensor_msgs/PointCloud2 (PointXYZRTL)
            'multi_topic': 0,
            'data_src': 0,
            'publish_freq': 10.0,
            'output_data_type': 0,
            'frame_id': 'lidar_link',
            'user_config_path': os.path.join(pkg, 'config', 'mid360_config.json'),
            'cmdline_input_bd_code': 'livox0000000001',
        }],
        remappings=[('/livox/lidar', '/points'), ('/livox/imu', '/lidar/imu')],
    )

    return LaunchDescription([
        DeclareLaunchArgument('lidar', default_value='l2', choices=['l2', 'mid360']),
        DeclareLaunchArgument('lidar_ip', default_value='192.168.1.62'),
        DeclareLaunchArgument('host_ip', default_value='192.168.1.2'),
        l2,
        mid360,
    ])
