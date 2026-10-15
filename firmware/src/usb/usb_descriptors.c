// USB descriptors for the Petmate MCU: one CDC-ACM interface.
// VID 0x2E8A (Raspberry Pi) as required by the udev rule in INTERFACES.md §2.
// PID 0x0009 is the Pico SDK's generic CDC PID; the product string
// "Petmate MCU" and the unique serial number identify the board.
#include <string.h>

#include "pico/unique_id.h"
#include "tusb.h"

#define USBD_VID 0x2E8A
#define USBD_PID 0x0009
#define USBD_BCD 0x0100  // device release 1.00

enum { ITF_NUM_CDC = 0, ITF_NUM_CDC_DATA, ITF_NUM_TOTAL };
enum { STR_LANGID = 0, STR_MANUF, STR_PRODUCT, STR_SERIAL, STR_CDC };

#define EPNUM_CDC_NOTIF 0x81
#define EPNUM_CDC_OUT   0x02
#define EPNUM_CDC_IN    0x82
#define CONFIG_TOTAL_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN)

static const tusb_desc_device_t desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    // IAD required for CDC (composite-style class codes)
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = USBD_VID,
    .idProduct = USBD_PID,
    .bcdDevice = USBD_BCD,
    .iManufacturer = STR_MANUF,
    .iProduct = STR_PRODUCT,
    .iSerialNumber = STR_SERIAL,
    .bNumConfigurations = 1,
};

static const uint8_t desc_configuration[] = {
    TUD_CONFIG_DESCRIPTOR(1, ITF_NUM_TOTAL, 0, CONFIG_TOTAL_LEN, 0, 100),
    TUD_CDC_DESCRIPTOR(ITF_NUM_CDC, STR_CDC, EPNUM_CDC_NOTIF, 8, EPNUM_CDC_OUT, EPNUM_CDC_IN, 64),
};

uint8_t const* tud_descriptor_device_cb(void) { return (uint8_t const*)&desc_device; }

uint8_t const* tud_descriptor_configuration_cb(uint8_t index) {
    (void)index;
    return desc_configuration;
}

static const char* const string_desc[] = {
    [STR_LANGID] = NULL,  // handled specially
    [STR_MANUF] = "Petmate",
    [STR_PRODUCT] = "Petmate MCU",
    [STR_SERIAL] = NULL,  // board unique id
    [STR_CDC] = "Petmate CDC",
};

static uint16_t desc_str[33];

uint16_t const* tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;
    char serial[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];
    const char* s;
    uint8_t n;
    if (index == STR_LANGID) {
        desc_str[1] = 0x0409;  // English (US)
        n = 1;
    } else {
        if (index >= sizeof(string_desc) / sizeof(string_desc[0])) return NULL;
        if (index == STR_SERIAL) {
            pico_get_unique_board_id_string(serial, sizeof serial);
            s = serial;
        } else {
            s = string_desc[index];
        }
        n = (uint8_t)strlen(s);
        if (n > 32) n = 32;
        for (uint8_t i = 0; i < n; ++i) desc_str[1 + i] = (uint8_t)s[i];
    }
    desc_str[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * n + 2));
    return desc_str;
}
