# MCU LAUNCH_STATUS (0x83)
uint8 IDLE=0
uint8 SPINNING_UP=1
uint8 LAUNCHING=2
uint8 DONE=3
uint8 EMPTY=4
std_msgs/Header header
uint8 state
string state_name
uint8 balls
