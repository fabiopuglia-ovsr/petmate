#!/bin/bash
set -e
source "/opt/ros/${ROS_DISTRO}/setup.bash"
source /opt/drivers/setup.bash
source /opt/petmate_ws/install/setup.bash
mkdir -p /var/lib/petmate/maps
exec "$@"
