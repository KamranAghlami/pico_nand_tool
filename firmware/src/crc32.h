/* IEEE 802.3 / zlib CRC-32 (reflected poly 0xEDB88320, init and final XOR 0xFFFFFFFF). docs/PROTOCOL.md. */
#ifndef CRC32_H
#define CRC32_H

#include <stddef.h>
#include <stdint.h>

#define CRC32_INIT 0xFFFFFFFFu

/* Incremental use: c = CRC32_INIT; c = crc32_update(c, a, n); ...; crc = crc32_final(c). */
uint32_t crc32_update(uint32_t crc, const void *data, size_t len);

static inline uint32_t crc32_final(uint32_t crc) { return crc ^ 0xFFFFFFFFu; }

static inline uint32_t crc32(const void *data, size_t len) {
    return crc32_final(crc32_update(CRC32_INIT, data, len));
}

#endif
