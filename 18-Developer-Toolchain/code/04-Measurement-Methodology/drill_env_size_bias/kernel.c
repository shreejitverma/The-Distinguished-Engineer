/*
 * kernel.c - the benchmark under test, plus the harness that measures it.
 *
 * The loop is Figure 1(a) of Mytkowicz, Diwan, Hauswirth and Sweeney, "Producing Wrong Data
 * Without Doing Anything Obviously Wrong!" (ASPLOS 2009): three static counters and two
 * stack locals. The paper built it at -O0 so that every variable lives in memory. Here the
 * five variables are volatile, which keeps the same loads and stores at -O2.
 *
 * Modes (argv[1]):
 *   naive   measure once, at whatever stack address the environment size produced
 *   sweep   measure at 256 stack offsets, 16 bytes apart, covering one 4096-byte period
 *
 * Every measurement is taken with perf_event_open(2) counters opened on this thread, user
 * mode only, enabled around the loop and nothing else. Each output line is one measurement:
 *
 *   sample static=<hex> slot=<hex> cycles=<n> instructions=<n> l1d_miss=<n> alias=<n>
 *
 * static and slot are the low 12 bits of &statics.i and &frame.g. A counter that this CPU
 * does not offer prints -1. Exit status 2 means the host cannot run the drill at all.
 */
#define _GNU_SOURCE
#include <alloca.h>
#include <errno.h>
#include <linux/perf_event.h>
#include <sched.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/personality.h>
#include <sys/syscall.h>
#include <unistd.h>

#if defined(__x86_64__) || defined(__i386__)
#include <cpuid.h>
#endif

#define ITERS 1000000u
#define REPS 5
#define POSITIONS 256
#define STEP 16
#define REEXEC_MARK "noaslr"

/* The three static counters of Figure 1(a). Static storage: the low 12 address bits are fixed at link time. */
struct statics {
	volatile uint32_t i, j, k;
};
static struct statics statics __attribute__((aligned(64)));

/* The two stack locals of Figure 1(a): the loop counter g and the increment inc. */
struct frame {
	volatile uint32_t g, inc;
};

enum { EV_CYCLES, EV_INSTRUCTIONS, EV_L1D_MISS, EV_ALIAS, EV_COUNT };

struct event {
	const char *name;
	uint32_t type;
	uint64_t config;
	int required;
	int fd;
};

struct sample {
	uintptr_t slot;
	long long value[EV_COUNT];
};

static struct event events[EV_COUNT];

static void die(const char *fmt, ...)
{
	va_list ap;

	fputs("kernel: ", stderr);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	exit(2);
}

__attribute__((noinline)) static void kernel(struct frame *f, uint32_t iters)
{
	for (f->g = 0; f->g < iters; f->g++) {
		statics.i += f->inc;
		statics.j += f->inc;
		statics.k += f->inc;
	}
}

/*
 * A fixed stack address would hide the effect under study, so ASLR has to be off:
 * with ADDR_NO_RANDOMIZE set, arch_align_stack() in the kernel stops subtracting a random
 * sub-page amount and the stack address becomes a pure function of the exec strings.
 * Re-executing through /proc/self/exe also makes the filename string on the stack a constant.
 */
static void ensure_no_aslr(int argc, char **argv)
{
	int persona = personality(0xffffffff);
	int reexeced = argc >= 3 && strcmp(argv[2], REEXEC_MARK) == 0;
	char *next[4];

	if (persona == -1)
		die("personality(0xffffffff) failed: %s", strerror(errno));
	if (persona & ADDR_NO_RANDOMIZE)
		return;
	if (reexeced)
		die("ADDR_NO_RANDOMIZE did not survive execve; cannot fix the stack address");
	if (personality(persona | ADDR_NO_RANDOMIZE) == -1)
		die("personality(ADDR_NO_RANDOMIZE) failed: %s (a seccomp profile, such as Docker's default, forbids it)",
		    strerror(errno));
	next[0] = argv[0];
	next[1] = argv[1];
	next[2] = (char *)REEXEC_MARK;
	next[3] = NULL;
	execv("/proc/self/exe", next);
	die("execv(/proc/self/exe) failed: %s", strerror(errno));
}

/* First CPU of a sysfs cpu list ("0-11,16") that is also in the allowed set, or -1. */
static int first_allowed_cpu(const char *path, const cpu_set_t *allowed)
{
	FILE *f = fopen(path, "r");
	int lo, hi, found = -1;

	if (f == NULL)
		return -1;
	while (found < 0 && fscanf(f, "%d", &lo) == 1) {
		int c = fgetc(f);

		hi = lo;
		if (c == '-' && fscanf(f, "%d", &hi) == 1)
			c = fgetc(f);
		for (int cpu = lo; cpu <= hi && cpu < CPU_SETSIZE; cpu++) {
			if (CPU_ISSET(cpu, allowed)) {
				found = cpu;
				break;
			}
		}
		if (c != ',')
			break;
	}
	fclose(f);
	return found;
}

/*
 * One CPU for the whole run, so a migration cannot change the core under the counters.
 * On a hybrid Intel CPU the generic and raw events below bind to the P-core PMU (cpu_core),
 * so the thread has to sit on a P-core or the counters never run.
 */
static void pin_to_one_cpu(void)
{
	cpu_set_t allowed, one;
	int cpu;

	if (sched_getaffinity(0, sizeof(allowed), &allowed) != 0)
		die("sched_getaffinity failed: %s", strerror(errno));
	cpu = first_allowed_cpu("/sys/devices/cpu_core/cpus", &allowed);
	if (cpu < 0)
		cpu = sched_getcpu();
	if (cpu < 0)
		die("sched_getcpu failed: %s", strerror(errno));
	CPU_ZERO(&one);
	CPU_SET(cpu, &one);
	if (sched_setaffinity(0, sizeof(one), &one) != 0)
		die("sched_setaffinity(cpu %d) failed: %s", cpu, strerror(errno));
}

/*
 * The load-blocked-by-4K-alias event is model specific. Encodings are the EventCode and UMask
 * fields of tools/perf/pmu-events/arch/x86/<model>/pipeline.json in Linux v6.12, keyed by the
 * CPUID models that mapfile.csv in the same tree assigns to each table.
 * Returns 0 when this CPU has no known event.
 */
static int alias_event(const char **name, uint64_t *config)
{
#if defined(__x86_64__) || defined(__i386__)
	/* LD_BLOCKS_PARTIAL.ADDRESS_ALIAS, EventCode 0x07, UMask 0x01: Sandy Bridge to Rocket Lake. */
	static const unsigned char partial[] = {
		0x2a, 0x2d, 0x3a, 0x3e, 0x3c, 0x45, 0x46, 0x3f, 0x3d, 0x47, 0x56, 0x4f, 0x4e,
		0x5e, 0x8e, 0x9e, 0xa5, 0xa6, 0x55, 0x7d, 0x7e, 0x6a, 0x6c, 0x8c, 0x8d, 0xa7,
	};
	/* LD_BLOCKS.ADDRESS_ALIAS, EventCode 0x03, UMask 0x04: Alder Lake and later, Sapphire Rapids and later. */
	static const unsigned char blocks[] = {
		0x97, 0x9a, 0xb7, 0xba, 0xbf, 0xbe, 0xcf, 0x8f, 0xad, 0xae, 0xaa, 0xac, 0xb6, 0xaf,
	};
	/* LD_BLOCKS.4K_ALIAS, EventCode 0x03, UMask 0x04: Goldmont, Goldmont Plus, Snow Ridge, Elkhart Lake. */
	static const unsigned char atom[] = { 0x5c, 0x5f, 0x7a, 0x86, 0x96, 0x9c };
	unsigned int a, b, c, d, family, model;
	char vendor[13];

	if (!__get_cpuid(0, &a, &b, &c, &d))
		return 0;
	memcpy(vendor, &b, 4);
	memcpy(vendor + 4, &d, 4);
	memcpy(vendor + 8, &c, 4);
	vendor[12] = '\0';
	if (strcmp(vendor, "GenuineIntel") != 0 || !__get_cpuid(1, &a, &b, &c, &d))
		return 0;
	family = (a >> 8) & 0xf;
	model = ((a >> 4) & 0xf) | ((a >> 12) & 0xf0);
	if (family != 6)
		return 0;
	for (size_t n = 0; n < sizeof(partial); n++) {
		if (partial[n] == model) {
			*name = "LD_BLOCKS_PARTIAL.ADDRESS_ALIAS";
			*config = 0x0107;
			return 1;
		}
	}
	for (size_t n = 0; n < sizeof(blocks); n++) {
		if (blocks[n] == model) {
			*name = "LD_BLOCKS.ADDRESS_ALIAS";
			*config = 0x0403;
			return 1;
		}
	}
	for (size_t n = 0; n < sizeof(atom); n++) {
		if (atom[n] == model) {
			*name = "LD_BLOCKS.4K_ALIAS";
			*config = 0x0403;
			return 1;
		}
	}
#endif
	(void)name;
	(void)config;
	return 0;
}

static int perf_open(const struct event *ev)
{
	struct perf_event_attr attr;

	memset(&attr, 0, sizeof(attr));
	attr.size = sizeof(attr);
	attr.type = ev->type;
	attr.config = ev->config;
	attr.disabled = 1;
	attr.exclude_kernel = 1;
	attr.exclude_hv = 1;
	attr.read_format = PERF_FORMAT_TOTAL_TIME_ENABLED | PERF_FORMAT_TOTAL_TIME_RUNNING;
	return (int)syscall(SYS_perf_event_open, &attr, 0, -1, -1, 0);
}

static void open_events(void)
{
	const char *alias_name = "none";
	uint64_t alias_config = 0;
	int have_alias = alias_event(&alias_name, &alias_config);

	events[EV_CYCLES] = (struct event){ "cycles", PERF_TYPE_HARDWARE, PERF_COUNT_HW_CPU_CYCLES, 1, -1 };
	events[EV_INSTRUCTIONS] =
		(struct event){ "instructions", PERF_TYPE_HARDWARE, PERF_COUNT_HW_INSTRUCTIONS, 1, -1 };
	events[EV_L1D_MISS] = (struct event){ "L1-dcache-load-misses", PERF_TYPE_HW_CACHE,
					      PERF_COUNT_HW_CACHE_L1D | (PERF_COUNT_HW_CACHE_OP_READ << 8) |
						      (PERF_COUNT_HW_CACHE_RESULT_MISS << 16),
					      0, -1 };
	events[EV_ALIAS] = (struct event){ alias_name, PERF_TYPE_RAW, alias_config, 0, -1 };

	for (int e = 0; e < EV_COUNT; e++) {
		if (e == EV_ALIAS && !have_alias)
			continue;
		events[e].fd = perf_open(&events[e]);
		if (events[e].fd >= 0 || !events[e].required)
			continue;
		if (errno == EACCES || errno == EPERM)
			die("perf_event_open(%s) was refused: %s; needs kernel.perf_event_paranoid <= 2 or CAP_PERFMON, "
			    "and a seccomp profile that allows the call",
			    events[e].name, strerror(errno));
		die("perf_event_open(%s) failed: %s; this host exposes no hardware PMU to the process (typical of a VM "
		    "or container)",
		    events[e].name, strerror(errno));
	}
}

static void counters_start(void)
{
	for (int e = EV_COUNT - 1; e >= 0; e--) {
		if (events[e].fd >= 0 && ioctl(events[e].fd, PERF_EVENT_IOC_RESET, 0) != 0)
			die("PERF_EVENT_IOC_RESET(%s) failed: %s", events[e].name, strerror(errno));
	}
	/* cycles is enabled last and disabled first, so its window is the innermost one. */
	for (int e = EV_COUNT - 1; e >= 0; e--) {
		if (events[e].fd >= 0 && ioctl(events[e].fd, PERF_EVENT_IOC_ENABLE, 0) != 0)
			die("PERF_EVENT_IOC_ENABLE(%s) failed: %s", events[e].name, strerror(errno));
	}
}

static void counters_stop(struct sample *s)
{
	for (int e = 0; e < EV_COUNT; e++) {
		if (events[e].fd >= 0 && ioctl(events[e].fd, PERF_EVENT_IOC_DISABLE, 0) != 0)
			die("PERF_EVENT_IOC_DISABLE(%s) failed: %s", events[e].name, strerror(errno));
	}
	for (int e = 0; e < EV_COUNT; e++) {
		struct {
			uint64_t value, enabled, running;
		} r;

		s->value[e] = -1;
		if (events[e].fd < 0)
			continue;
		if (read(events[e].fd, &r, sizeof(r)) != (ssize_t)sizeof(r))
			die("read(%s) failed: %s", events[e].name, strerror(errno));
		if (r.running == 0)
			die("counter %s never ran; on a hybrid CPU the thread must sit on a P-core", events[e].name);
		/* time_running below time_enabled means the kernel multiplexed the counter and the value is a partial count. */
		if (r.running < r.enabled - r.enabled / 1000)
			die("counter %s was multiplexed (ran %llu of %llu ns); free a PMU counter, for example stop other "
			    "perf sessions or the NMI watchdog",
			    events[e].name, (unsigned long long)r.running, (unsigned long long)r.enabled);
		s->value[e] = (long long)r.value;
	}
}

/* The frame under test lives here, so its address is this function's stack position. */
__attribute__((noinline)) static void measure(struct sample *best)
{
	_Alignas(16) struct frame f;
	struct sample s = { 0 };

	f.inc = 1;
	for (int rep = 0; rep < REPS; rep++) {
		counters_start();
		kernel(&f, ITERS);
		counters_stop(&s);
		if (rep == 0 || s.value[EV_CYCLES] < best->value[EV_CYCLES])
			*best = s;
	}
	best->slot = (uintptr_t)&f.g;
}

/* Push the frame under test down by pad bytes. The write after the call keeps the hole live, which forbids a tail call. */
__attribute__((noinline)) static void measure_at_offset(size_t pad, struct sample *best)
{
	volatile char *hole = alloca(pad + STEP);

	hole[0] = 0;
	measure(best);
	hole[0] = 1;
}

static void print_sample(const struct sample *s)
{
	printf("sample static=%03lx slot=%03lx cycles=%lld instructions=%lld l1d_miss=%lld alias=%lld\n",
	       (unsigned long)((uintptr_t)&statics.i & 0xfff), (unsigned long)(s->slot & 0xfff), s->value[EV_CYCLES],
	       s->value[EV_INSTRUCTIONS], s->value[EV_L1D_MISS], s->value[EV_ALIAS]);
}

int main(int argc, char **argv)
{
	struct sample s;
	int sweep;

	if (argc < 2 || (strcmp(argv[1], "naive") != 0 && strcmp(argv[1], "sweep") != 0)) {
		fputs("usage: kernel naive|sweep\n", stderr);
		return 2;
	}
	sweep = strcmp(argv[1], "sweep") == 0;
	ensure_no_aslr(argc, argv);
	pin_to_one_cpu();
	open_events();
	printf("meta iters=%u reps=%d alias_event=%s\n", ITERS, REPS, events[EV_ALIAS].fd >= 0 ? events[EV_ALIAS].name : "none");
	for (int p = 0; p < (sweep ? POSITIONS : 1); p++) {
		measure_at_offset((size_t)p * STEP, &s);
		print_sample(&s);
	}
	return 0;
}
