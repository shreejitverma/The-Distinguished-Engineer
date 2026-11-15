#include <string.h>
#include <zlib.h>
#include "mylib.h"

const char *mylib_zlib_version(void) { return zlibVersion(); }

int mylib_roundtrip(void) {
    const char msg[] = "meson wraps are pinned by sha256";
    unsigned char packed[128];
    unsigned char back[128];
    uLongf plen = sizeof packed;
    uLongf blen = sizeof back;
    if (compress(packed, &plen, (const Bytef *)msg, sizeof msg) != Z_OK) return 0;
    if (uncompress(back, &blen, packed, plen) != Z_OK) return 0;
    return blen == sizeof msg && memcmp(back, msg, sizeof msg) == 0;
}
