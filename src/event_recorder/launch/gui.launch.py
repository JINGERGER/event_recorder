"""Start the independent recorder and HTTP GUI with one writable configuration."""
from pathlib import Path
import shutil

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def launch_nodes(context):
    config = Path(LaunchConfiguration('config').perform(context)).expanduser().resolve()
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(get_package_share_directory('event_recorder')) / 'config/recorder.yaml', config)
    return [
        Node(package='event_recorder', executable='recorder', name='event_recorder',
             parameters=[str(config)], output='screen'),
        Node(package='event_recorder', executable='web_gui', output='screen',
             arguments=['--config', str(config), '--host', LaunchConfiguration('host'),
                        '--port', LaunchConfiguration('port')]),
    ]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('config', default_value=str(Path.cwd() / 'recorder.local.yaml')),
        DeclareLaunchArgument('host', default_value='127.0.0.1'),
        DeclareLaunchArgument('port', default_value='8080'),
        OpaqueFunction(function=launch_nodes),
    ])
