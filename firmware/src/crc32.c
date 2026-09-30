#include "crc32.h"

#include <stdbool.h>

/* Byte-wise table in RAM, built on first use (PROPOSAL §4.3 budget: ~100-150 us per 2112-byte page). */
static uint32_t crc32_table[256];
static bool crc32_table_ready;

static void crc32_build_table(void) {
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++)
            c = (c & 1u) ? (c >> 1) ^ 0xEDB88320u : c >> 1;
        crc32_table[i] = c;
    }
    crc32_table_ready = true;
}

uint32_t crc32_update(uint32_t crc, const void *data, size_t len) {
    if (!crc32_table_ready)
        crc32_build_table();
    const uint8_t *p = data;
    while (len--)
        crc = (crc >> 8) ^ crc32_table[(crc ^ *p++) & 0xFFu];
    return crc;
}
