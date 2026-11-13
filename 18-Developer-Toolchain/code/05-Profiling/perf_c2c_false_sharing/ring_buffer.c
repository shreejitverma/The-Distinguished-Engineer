/*
 * ring_buffer.c - the perf_event_open(2) sampling contract, without the perf tool.
 *
 * Phase 1 opens a software task-clock event on the calling thread, maps
 * 1 metadata page + 8 data pages, burns a fixed amount of CPU time, and walks
 * the records between data_tail and data_head.
 *
 * Phase 2 maps a single data page, lets the kernel fill it without consuming
 * anything, then consumes everything and samples a little more. The mapping is
 * writable, so the kernel must not overwrite unread records: it drops them and
 * reports the count in a PERF_RECORD_LOST record once there is room again.
 *
 * Only derived facts are printed, so the output is the same on every run.
 */
#define _GNU_SOURCE
#include <linux/perf_event.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

struct ring {
	int fd;
	struct perf_event_mmap_page *meta;
	unsigned char *data;
	size_t map_len;
	uint64_t data_size;
};

struct walk {
	uint64_t bytes;
	uint64_t records;
	uint64_t samples;
	uint64_t lost_records;
	uint64_t lost_count;
	uint64_t other;
	int first_type;
	int sizes_ok;
	int ids_ok;
	int tiles;
};

/* Layout of one sample for sample_type = IP | TID | TIME. */
struct sample {
	struct perf_event_header header;
	uint64_t ip;
	uint32_t pid, tid;
	uint64_t time;
};

/* Layout of PERF_RECORD_LOST without sample_id_all. */
struct lost {
	struct perf_event_header header;
	uint64_t id;
	uint64_t lost;
};

static volatile uint64_t sink;

static uint64_t thread_cpu_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_THREAD_CPUTIME_ID, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* Burn CPU time, measured on the same clock the event counts. */
static void burn_cpu_ms(unsigned int ms)
{
	uint64_t end = thread_cpu_ns() + (uint64_t)ms * 1000000ULL;
	uint64_t x = 1;

	while (thread_cpu_ns() < end) {
		/* Long enough that the clock_gettime system call is a negligible share
		 * of the time: the event excludes the kernel, so a timer that fires
		 * inside the system call produces no sample. */
		for (int i = 0; i < 200000; i++)
			x = x * 6364136223846793005ULL + 1442695040888963407ULL;
	}
	sink = x;
}

static int ring_open(struct ring *r, uint64_t period_ns, unsigned int data_pages)
{
	struct perf_event_attr attr;
	size_t page = (size_t)sysconf(_SC_PAGESIZE);
	void *base;

	memset(&attr, 0, sizeof(attr));
	attr.type = PERF_TYPE_SOFTWARE;
	attr.size = sizeof(attr);
	attr.config = PERF_COUNT_SW_TASK_CLOCK;
	attr.sample_period = period_ns;
	attr.sample_type = PERF_SAMPLE_IP | PERF_SAMPLE_TID | PERF_SAMPLE_TIME;
	attr.disabled = 1;
	attr.exclude_kernel = 1;
	attr.exclude_hv = 1;

	/* pid 0, cpu -1: this thread, on whatever CPU it runs. */
	r->fd = (int)syscall(SYS_perf_event_open, &attr, 0, -1, -1, PERF_FLAG_FD_CLOEXEC);
	if (r->fd < 0) {
		perror("perf_event_open");
		return -1;
	}

	r->map_len = (1 + (size_t)data_pages) * page;
	base = mmap(NULL, r->map_len, PROT_READ | PROT_WRITE, MAP_SHARED, r->fd, 0);
	if (base == MAP_FAILED) {
		perror("mmap");
		close(r->fd);
		return -1;
	}
	r->meta = base;
	r->data = (unsigned char *)base + r->meta->data_offset;
	r->data_size = r->meta->data_size;
	return 0;
}

static void ring_close(struct ring *r)
{
	munmap(r->meta, r->map_len);
	close(r->fd);
}

/* Copy len bytes that start at ring position pos, handling the wrap. */
static void ring_copy(const struct ring *r, uint64_t pos, void *out, size_t len)
{
	uint64_t off = pos & (r->data_size - 1);
	size_t first = (size_t)(r->data_size - off);

	if (first > len)
		first = len;
	memcpy(out, r->data + off, first);
	memcpy((unsigned char *)out + first, r->data, len - first);
}

/* Consume every record in [data_tail, data_head) and publish the new tail. */
static void ring_walk(struct ring *r, struct walk *w, uint32_t pid, uint32_t tid)
{
	/* Load the head, then read the data: the kernel published the head after
	 * storing the records, so an acquire load is the reader's half. */
	uint64_t head = __atomic_load_n(&r->meta->data_head, __ATOMIC_ACQUIRE);
	uint64_t tail = r->meta->data_tail;

	memset(w, 0, sizeof(*w));
	w->first_type = -1;
	w->sizes_ok = 1;
	w->ids_ok = 1;

	while (tail < head) {
		struct perf_event_header h;

		ring_copy(r, tail, &h, sizeof(h));
		if (h.size < sizeof(h) || h.size % 8 != 0 || tail + h.size > head) {
			w->sizes_ok = 0;
			break;
		}
		if (w->first_type < 0)
			w->first_type = (int)h.type;
		w->records++;

		if (h.type == PERF_RECORD_SAMPLE && h.size == sizeof(struct sample)) {
			struct sample s;

			ring_copy(r, tail, &s, sizeof(s));
			w->samples++;
			if (s.pid != pid || s.tid != tid)
				w->ids_ok = 0;
		} else if (h.type == PERF_RECORD_LOST && h.size == sizeof(struct lost)) {
			struct lost l;

			ring_copy(r, tail, &l, sizeof(l));
			w->lost_records++;
			w->lost_count += l.lost;
		} else {
			w->other++;
		}
		tail += h.size;
		w->bytes += h.size;
	}
	w->tiles = (tail == head);

	/* Publish the tail only after the data has been read. */
	__atomic_store_n(&r->meta->data_tail, tail, __ATOMIC_SEQ_CST);
}

/* True when the unread bytes leave no room for one more sample record. */
static int ring_full(const struct ring *r)
{
	uint64_t head = __atomic_load_n(&r->meta->data_head, __ATOMIC_ACQUIRE);

	return head - r->meta->data_tail + sizeof(struct sample) >= r->data_size;
}

static const char *yes_no(int v)
{
	return v ? "yes" : "no";
}

int main(void)
{
	uint64_t page = (uint64_t)sysconf(_SC_PAGESIZE);
	uint32_t pid = (uint32_t)getpid();
	uint32_t tid = (uint32_t)syscall(SYS_gettid);
	struct ring r;
	struct walk w;
	int full;

	/* Phase 1: 1 ms period, 200 ms of CPU, a buffer far larger than needed. */
	if (ring_open(&r, 1000000ULL, 8) != 0)
		return 1;
	printf("phase 1: task-clock, period 1 ms, 1 metadata page + 8 data pages\n");
	printf("  data_offset equals one page: %s\n", yes_no(r.meta->data_offset == page));
	printf("  data_size equals eight pages: %s\n", yes_no(r.meta->data_size == 8 * page));

	ioctl(r.fd, PERF_EVENT_IOC_ENABLE, 0);
	burn_cpu_ms(200);
	ioctl(r.fd, PERF_EVENT_IOC_DISABLE, 0);
	ring_walk(&r, &w, pid, tid);

	printf("  records tile [data_tail, data_head) with sizes that are multiples of 8: %s\n",
	       yes_no(w.sizes_ok && w.tiles));
	printf("  sample records after 200 ms of CPU time, expected about 200: %s\n",
	       (w.samples >= 150 && w.samples <= 250) ? "150 to 250" : "OUT OF RANGE");
	printf("  every sample carries this pid and tid: %s\n", yes_no(w.ids_ok && w.samples > 0));
	printf("  PERF_RECORD_LOST records: %llu\n", (unsigned long long)w.lost_records);
	ring_close(&r);

	/* Phase 2: 100 us period into one data page that nobody consumes. */
	if (ring_open(&r, 100000ULL, 1) != 0)
		return 1;
	printf("phase 2: task-clock, period 100 us, 1 metadata page + 1 data page, reader idle\n");

	ioctl(r.fd, PERF_EVENT_IOC_ENABLE, 0);
	/* Burn until no further sample fits (bounded), then keep going so that
	 * the kernel has samples it must drop. */
	for (int i = 0; i < 300 && !ring_full(&r); i++)
		burn_cpu_ms(10);
	full = ring_full(&r);
	burn_cpu_ms(50);
	ioctl(r.fd, PERF_EVENT_IOC_DISABLE, 0);
	ring_walk(&r, &w, pid, tid);

	printf("  buffer filled until one more sample did not fit: %s\n",
	       yes_no(full && w.bytes + sizeof(struct sample) >= r.data_size));
	printf("  unread records were kept, none overwritten: %s\n",
	       yes_no(w.sizes_ok && w.tiles && w.ids_ok && w.samples > 0 && w.bytes < r.data_size));
	printf("  PERF_RECORD_LOST records while the buffer stayed full: %llu\n",
	       (unsigned long long)w.lost_records);

	/* Room again: the next record the kernel writes reports what it dropped. */
	ioctl(r.fd, PERF_EVENT_IOC_ENABLE, 0);
	burn_cpu_ms(5);
	ioctl(r.fd, PERF_EVENT_IOC_DISABLE, 0);
	ring_walk(&r, &w, pid, tid);

	printf("  first record after the reader caught up is PERF_RECORD_LOST: %s\n",
	       yes_no(w.first_type == PERF_RECORD_LOST));
	printf("  it reports a nonzero count of dropped records: %s\n", yes_no(w.lost_count > 0));
	ring_close(&r);
	return 0;
}
