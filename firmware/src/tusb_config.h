/* TinyUSB configuration: one raw CDC-ACM interface as the binary data channel (docs/PROTOCOL.md "Transport"). */
#ifndef TUSB_CONFIG_H
#define TUSB_CONFIG_H

#define CFG_TUD_ENABLED 1
#define CFG_TUSB_RHPORT0_MODE OPT_MODE_DEVICE
#define CFG_TUD_ENDPOINT0_SIZE 64

#define CFG_TUD_CDC 1
#define CFG_TUD_MSC 0
#define CFG_TUD_HID 0
#define CFG_TUD_MIDI 0
#define CFG_TUD_VENDOR 0

/* Large transfer buffer: the RP2040 DCD runs multi-packet transfers from the ISR, so USB keeps draining while the
 * CPU bit-bangs the next page (PROPOSAL §3.1). The RX FIFO must be >= EP buffer (cdc_device.c prepares OUT
 * transfers only when that much space is free). */
#define CFG_TUD_CDC_EP_BUFSIZE 4096
#define CFG_TUD_CDC_TX_BUFSIZE 8192
#define CFG_TUD_CDC_RX_BUFSIZE 4096

#endif
