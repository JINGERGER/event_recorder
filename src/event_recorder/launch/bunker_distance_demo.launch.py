"""Recorder + GUI + a one-shot cumulative odometry distance trigger. Replay separately."""
from pathlib import Path
import shutil

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, OpaqueFunction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def launch_nodes(context):
    share = Path(get_package_share_directory('event_recorder'))
    config = Path(LaunchConfiguration('config').perform(context)).expanduser().resolve()
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(share / 'config/bunker_distance_demo.yaml', config)
    return [
        IncludeLaunchDescription(PythonLaunchDescriptionSource(str(share / 'launch/gui.launch.py')),
            launch_arguments={'config': str(config), 'host': LaunchConfiguration('host').perform(context),
                              'port': LaunchConfiguration('port').perform(context)}.items()),
        Node(package='event_recorder', executable='odom_distance_trigger', output='screen',
             parameters=[{'odom_topic': '/odom', 'robot_type': 'bunker_mini',
                          'threshold_m': float(LaunchConfiguration('threshold_m').perform(context))}]),
    ]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('config', default_value=str(Path.cwd() / 'bunker_distance_demo.local.yaml')),
        DeclareLaunchArgument('threshold_m', default_value='20.0'),
        DeclareLaunchArgument('host', default_value='127.0.0.1'),
        DeclareLaunchArgument('port', default_value='8080'),
        OpaqueFunction(function=launch_nodes),
    ])
