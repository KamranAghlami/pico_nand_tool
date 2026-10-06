/* USB descriptors: single CDC-ACM interface. Adapted from pico-sdk pico_stdio_usb/stdio_usb_descriptors.c.
 *
 * Background: when a USB device is plugged in, the host asks it to describe itself (enumeration). These tables are
 * the answers: the device descriptor (VID:PID, strings), the configuration descriptor (one CDC-ACM "virtual serial
 * port" = 2 interfaces, a control/notification endpoint and a bulk OUT + bulk IN pair for the data), and the string
 * descriptors. TinyUSB calls the tud_descriptor_*_cb() functions below to fetch them. The product string
 * "Pico NAND Tool" is what the host tool's auto-detection looks for (host/nand_tool/transport.py). */
#include "pico/unique_id.h"
#include "tusb.h"

#define USBD_VID 0x2E8A /* Raspberry Pi */
#define USBD_PID 0x000A /* Raspberry Pi Pico SDK CDC (RP2040); docs/PROTOCOL.md */
#define USBD_MANUFACTURER "Raspberry Pi"
#define USBD_PRODUCT "Pico NAND Tool"

#define USBD_ITF_CDC 0 /* CDC needs 2 interfaces */
#define USBD_ITF_MAX 2
#define USBD_DESC_LEN (TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN)

#define USBD_CDC_EP_CMD 0x81
#define USBD_CDC_EP_OUT 0x02
#define USBD_CDC_EP_IN 0x82
#define USBD_CDC_CMD_MAX_SIZE 8
#define USBD_CDC_IN_OUT_MAX_SIZE 64

enum { STR_LANG, STR_MANUF, STR_PRODUCT, STR_SERIAL, STR_CDC, STR_COUNT };

static const tusb_desc_device_t desc_device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = USBD_VID,
    .idProduct = USBD_PID,
    .bcdDevice = 0x0100,
    .iManufacturer = STR_MANUF,
    .iProduct = STR_PRODUCT,
    .iSerialNumber = STR_SERIAL,
    .bNumConfigurations = 1,
};

static const uint8_t desc_cfg[USBD_DESC_LEN] = {
    TUD_CONFIG_DESCRIPTOR(1, USBD_ITF_MAX, 0, USBD_DESC_LEN, 0, 100),
    TUD_CDC_DESCRIPTOR(USBD_ITF_CDC, STR_CDC, USBD_CDC_EP_CMD, USBD_CDC_CMD_MAX_SIZE, USBD_CDC_EP_OUT,
                       USBD_CDC_EP_IN, USBD_CDC_IN_OUT_MAX_SIZE),
};

static char serial_str[PICO_UNIQUE_BOARD_ID_SIZE_BYTES * 2 + 1];

static const char *const desc_str[STR_COUNT] = {
    [STR_MANUF] = USBD_MANUFACTURER,
    [STR_PRODUCT] = USBD_PRODUCT,
    [STR_SERIAL] = serial_str,
    [STR_CDC] = "NAND data",
};

const uint8_t *tud_descriptor_device_cb(void) { return (const uint8_t *)&desc_device; }

const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
    (void)index;
    return desc_cfg;
}

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;
    static uint16_t buf[32];
    uint8_t len;

    if (!serial_str[0])
        pico_get_unique_board_id_string(serial_str, sizeof(serial_str));

    if (index == STR_LANG) {
        buf[1] = 0x0409; /* English (US) */
        len = 1;
    } else {
        if (index >= STR_COUNT)
            return NULL;
        const char *s = desc_str[index];
        for (len = 0; len < 31 && s[len]; len++)
            buf[1 + len] = (uint8_t)s[len];
    }
    /* USB strings are UTF-16LE; the ASCII chars above were widened to 16 bits. Word 0 = descriptor type and the
     * total length in bytes (2-byte header + 2 bytes per char). */
    buf[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * len + 2));
    return buf;
}
