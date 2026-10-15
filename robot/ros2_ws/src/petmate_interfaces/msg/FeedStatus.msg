# MCU FEED_STATUS (0x82)
uint8 IDLE=0
uint8 DISPENSING=1
uint8 DONE=2
uint8 JAM=3
uint8 HOPPER_EMPTY=4
uint8 ABORTED=5
std_msgs/Header header
uint8 state
string state_name
float32 dispensed_g
float32 hopper_g
