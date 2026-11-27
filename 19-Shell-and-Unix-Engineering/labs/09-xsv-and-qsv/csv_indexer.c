/*
 * csv_indexer.c - Reference RFC 4180 CSV parser and RandomAccessSimple indexer.
 * Implements the BurntSushi csv-index binary format:
 *   - Sequence of (N + 1) big-endian uint64_t values.
 *   - Entries 0 to N-1: Byte offset of record i in the CSV file.
 *   - Entry N: Total number of records (N).
 */

#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>

#if defined(__linux__)
#include <endian.h>
#elif defined(__APPLE__)
#include <libkern/OSByteOrder.h>
#define htobe64(x) OSSwapHostToBigInt64(x)
#define be64toh(x) OSSwapBigToHostInt64(x)
#else
static inline uint64_t htobe64(uint64_t x) {
    uint64_t ret = 0;
    uint8_t *src = (uint8_t *)&x;
    uint8_t *dst = (uint8_t *)&ret;
    dst[0] = src[7]; dst[1] = src[6]; dst[2] = src[5]; dst[3] = src[4];
    dst[4] = src[3]; dst[5] = src[2]; dst[6] = src[1]; dst[7] = src[0];
    return ret;
}
#define be64toh(x) htobe64(x)
#endif

typedef enum {
    STATE_GROUND,
    STATE_IN_FIELD,
    STATE_IN_QUOTED_FIELD,
    STATE_QUOTE_IN_QUOTED
} parser_state_t;

static int write_all(int fd, const void *buf, size_t count) {
    const uint8_t *p = (const uint8_t *)buf;
    while (count > 0) {
        ssize_t n = write(fd, p, count);
        if (n <= 0) {
            return -1;
        }
        p += n;
        count -= (size_t)n;
    }
    return 0;
}

static int do_index(const char *csv_path, const char *idx_path) {
    int csv_fd = open(csv_path, O_RDONLY);
    if (csv_fd < 0) {
        perror("open csv");
        return 1;
    }

    size_t capacity = 1024;
    size_t count = 0;
    uint64_t *offsets = malloc(capacity * sizeof(uint64_t));
    if (!offsets) {
        close(csv_fd);
        return 1;
    }

    /* Record 0 starts at offset 0 */
    offsets[count++] = 0;

    uint8_t buffer[65536];
    uint64_t current_offset = 0;
    parser_state_t state = STATE_GROUND;
    ssize_t nread;

    while ((nread = read(csv_fd, buffer, sizeof(buffer))) > 0) {
        for (ssize_t i = 0; i < nread; i++) {
            uint8_t b = buffer[i];
            uint64_t byte_pos = current_offset + (uint64_t)i;

            switch (state) {
            case STATE_GROUND:
                if (b == '"') {
                    state = STATE_IN_QUOTED_FIELD;
                } else if (b == '\n') {
                    /* End of record */
                    if (count >= capacity) {
                        capacity *= 2;
                        uint64_t *next = realloc(offsets, capacity * sizeof(uint64_t));
                        if (!next) { free(offsets); close(csv_fd); return 1; }
                        offsets = next;
                    }
                    offsets[count++] = byte_pos + 1;
                    state = STATE_GROUND;
                } else if (b == '\r') {
                    /* CR in CRLF or bare CR */
                } else if (b == ',') {
                    state = STATE_GROUND;
                } else {
                    state = STATE_IN_FIELD;
                }
                break;

            case STATE_IN_FIELD:
                if (b == ',') {
                    state = STATE_GROUND;
                } else if (b == '\n') {
                    if (count >= capacity) {
                        capacity *= 2;
                        uint64_t *next = realloc(offsets, capacity * sizeof(uint64_t));
                        if (!next) { free(offsets); close(csv_fd); return 1; }
                        offsets = next;
                    }
                    offsets[count++] = byte_pos + 1;
                    state = STATE_GROUND;
                }
                break;

            case STATE_IN_QUOTED_FIELD:
                if (b == '"') {
                    state = STATE_QUOTE_IN_QUOTED;
                }
                break;

            case STATE_QUOTE_IN_QUOTED:
                if (b == '"') {
                    /* Doubled quote: "" -> literal quote */
                    state = STATE_IN_QUOTED_FIELD;
                } else if (b == ',') {
                    state = STATE_GROUND;
                } else if (b == '\n') {
                    if (count >= capacity) {
                        capacity *= 2;
                        uint64_t *next = realloc(offsets, capacity * sizeof(uint64_t));
                        if (!next) { free(offsets); close(csv_fd); return 1; }
                        offsets = next;
                    }
                    offsets[count++] = byte_pos + 1;
                    state = STATE_GROUND;
                } else if (b == '\r') {
                    /* Wait for LF */
                } else {
                    state = STATE_IN_FIELD;
                }
                break;
            }
        }
        current_offset += (uint64_t)nread;
    }

    close(csv_fd);

    /* If file ends with newline, the last offset is at EOF; remove trailing empty record offset if at EOF */
    if (count > 1 && offsets[count - 1] == current_offset) {
        count--;
    }

    /* Write index file: N offsets followed by total count N */
    int idx_fd = open(idx_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (idx_fd < 0) {
        perror("open index for writing");
        free(offsets);
        return 1;
    }

    uint64_t total_records = count;
    for (size_t i = 0; i < total_records; i++) {
        uint64_t be_val = htobe64(offsets[i]);
        if (write_all(idx_fd, &be_val, sizeof(be_val)) < 0) {
            perror("write offset");
            free(offsets);
            close(idx_fd);
            return 1;
        }
    }

    /* Append total record count as (N+1)-th integer */
    uint64_t be_count = htobe64(total_records);
    if (write_all(idx_fd, &be_count, sizeof(be_count)) < 0) {
        perror("write count");
        free(offsets);
        close(idx_fd);
        return 1;
    }

    close(idx_fd);
    free(offsets);
    printf("Indexed %llu records into %s\n", (unsigned long long)total_records, idx_path);
    return 0;
}

static int do_count(const char *idx_path) {
    int idx_fd = open(idx_path, O_RDONLY);
    if (idx_fd < 0) {
        perror("open index");
        return 1;
    }

    struct stat st;
    if (fstat(idx_fd, &st) < 0) {
        perror("fstat");
        close(idx_fd);
        return 1;
    }

    if (st.st_size < 8 || (st.st_size % 8) != 0) {
        fprintf(stderr, "Error: invalid index file size (%lld bytes)\n", (long long)st.st_size);
        close(idx_fd);
        return 1;
    }

    /* Seek to the last 8 bytes: O(1) record counting */
    if (lseek(idx_fd, -8, SEEK_END) < 0) {
        perror("lseek");
        close(idx_fd);
        return 1;
    }

    uint64_t be_count;
    if (read(idx_fd, &be_count, sizeof(be_count)) != sizeof(be_count)) {
        perror("read count");
        close(idx_fd);
        return 1;
    }
    close(idx_fd);

    uint64_t count = be64toh(be_count);
    printf("%llu\n", (unsigned long long)count);
    return 0;
}

static int do_slice(const char *csv_path, const char *idx_path, uint64_t start, uint64_t length) {
    int idx_fd = open(idx_path, O_RDONLY);
    if (idx_fd < 0) { perror("open index"); return 1; }

    struct stat st_idx;
    if (fstat(idx_fd, &st_idx) < 0) { perror("fstat index"); close(idx_fd); return 1; }

    uint64_t total_entries = (uint64_t)st_idx.st_size / 8;
    if (total_entries < 1) { close(idx_fd); return 1; }
    uint64_t total_records = total_entries - 1;

    if (start >= total_records) {
        close(idx_fd);
        return 0;
    }

    uint64_t end = start + length;
    if (end > total_records) {
        end = total_records;
    }

    /* Read start offset */
    if (lseek(idx_fd, (off_t)(start * 8), SEEK_SET) < 0) {
        perror("lseek start");
        close(idx_fd);
        return 1;
    }
    uint64_t be_start_off;
    if (read(idx_fd, &be_start_off, sizeof(be_start_off)) != sizeof(be_start_off)) {
        close(idx_fd);
        return 1;
    }
    uint64_t start_off = be64toh(be_start_off);

    /* Read end offset */
    uint64_t end_off;
    if (end < total_records) {
        if (lseek(idx_fd, (off_t)(end * 8), SEEK_SET) < 0) {
            close(idx_fd);
            return 1;
        }
        uint64_t be_end_off;
        if (read(idx_fd, &be_end_off, sizeof(be_end_off)) != sizeof(be_end_off)) {
            close(idx_fd);
            return 1;
        }
        end_off = be64toh(be_end_off);
    } else {
        /* End is EOF of CSV */
        struct stat st_csv;
        int csv_fd_tmp = open(csv_path, O_RDONLY);
        if (csv_fd_tmp < 0) { close(idx_fd); return 1; }
        fstat(csv_fd_tmp, &st_csv);
        close(csv_fd_tmp);
        end_off = (uint64_t)st_csv.st_size;
    }
    close(idx_fd);

    /* Open CSV and stream bytes [start_off, end_off) */
    int csv_fd = open(csv_path, O_RDONLY);
    if (csv_fd < 0) { perror("open csv"); return 1; }

    if (lseek(csv_fd, (off_t)start_off, SEEK_SET) < 0) {
        perror("lseek csv");
        close(csv_fd);
        return 1;
    }

    uint64_t bytes_to_copy = end_off - start_off;
    uint8_t buffer[65536];
    while (bytes_to_copy > 0) {
        size_t chunk = sizeof(buffer);
        if (chunk > bytes_to_copy) chunk = (size_t)bytes_to_copy;
        ssize_t n = read(csv_fd, buffer, chunk);
        if (n <= 0) break;
        if (write_all(STDOUT_FILENO, buffer, (size_t)n) < 0) {
            break;
        }
        bytes_to_copy -= (uint64_t)n;
    }

    close(csv_fd);
    return 0;
}

static int do_validate(const char *csv_path, const char *idx_path) {
    struct stat st_csv, st_idx;
    if (stat(csv_path, &st_csv) < 0) {
        perror("stat csv");
        return 1;
    }
    if (stat(idx_path, &st_idx) < 0) {
        perror("stat idx");
        return 1;
    }

    /* Check staleness: index mtime must be >= csv mtime */
    if (st_idx.st_mtime < st_csv.st_mtime) {
        fprintf(stderr, "Error: index is stale (idx_mtime < csv_mtime)\n");
        return 2;
    }

    /* Check alignment */
    if ((st_idx.st_size % 8) != 0 || st_idx.st_size < 8) {
        fprintf(stderr, "Error: index size %lld is not a multiple of 8 bytes\n", (long long)st_idx.st_size);
        return 3;
    }

    int idx_fd = open(idx_path, O_RDONLY);
    if (idx_fd < 0) return 1;
    if (lseek(idx_fd, -8, SEEK_END) < 0) { close(idx_fd); return 1; }
    uint64_t be_count;
    if (read(idx_fd, &be_count, sizeof(be_count)) != sizeof(be_count)) { close(idx_fd); return 1; }
    close(idx_fd);

    uint64_t count = be64toh(be_count);
    uint64_t expected_size = (count + 1) * 8;
    if ((uint64_t)st_idx.st_size != expected_size) {
        fprintf(stderr, "Error: index size %llu does not match (N+1)*8 = %llu for N=%llu\n",
                (unsigned long long)st_idx.st_size, (unsigned long long)expected_size, (unsigned long long)count);
        return 4;
    }

    printf("Valid index: %llu records, %llu bytes\n", (unsigned long long)count, (unsigned long long)st_idx.st_size);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <index|count|slice|validate> [args...]\n", argv[0]);
        return 1;
    }

    if (strcmp(argv[1], "index") == 0 && argc == 4) {
        return do_index(argv[2], argv[3]);
    } else if (strcmp(argv[1], "count") == 0 && argc == 3) {
        return do_count(argv[2]);
    } else if (strcmp(argv[1], "slice") == 0 && argc == 6) {
        return do_slice(argv[2], argv[3], (uint64_t)atoll(argv[4]), (uint64_t)atoll(argv[5]));
    } else if (strcmp(argv[1], "validate") == 0 && argc == 4) {
        return do_validate(argv[2], argv[3]);
    }

    fprintf(stderr, "Invalid arguments\n");
    return 1;
}
