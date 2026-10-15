cmake_minimum_required(VERSION 3.8)
project(petmate_interfaces)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/HwState.msg"
  "msg/FeedStatus.msg"
  "msg/LaunchStatus.msg"
  "msg/McuEvent.msg"
  "srv/Feed.srv"
  "srv/Launch.srv"
  "srv/Intake.srv"
  "srv/Tease.srv"
  "srv/Led.srv"
  "srv/Estop.srv"
  DEPENDENCIES std_msgs
)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
