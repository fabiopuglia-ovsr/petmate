# Raw hardware state from MCU STATE packet (0x81), INTERFACES.md §2
std_msgs/Header header
uint32 mcu_t_ms
int32 enc_l
int32 enc_r
uint8 cliff_mask        # bit0 FL, bit1 FR, bit2 RL, bit3 RR
uint8 bump_mask         # bit0 front, bit1 rear
uint8 flags             # raw flags byte
bool estop              # flags bit0
bool wheel_drop         # flags bit1
bool motor_fault        # flags bit2
bool charging           # flags bit3
bool docked             # flags bit4
bool button_pressed     # flags bit5
bool watchdog_tripped   # flags bit6
float32 batt_v
float32 batt_a          # negative = discharging
uint8 soc_pct
string fw               # "major.minor.patch" from PONG, empty if unknown
bool mcu_connected
