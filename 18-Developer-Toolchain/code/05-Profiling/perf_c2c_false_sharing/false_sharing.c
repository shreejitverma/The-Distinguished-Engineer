/*
 * false_sharing.c - one writer thread and one reader thread that never touch
 * the same variable.
 *
 * Built twice by run.sh, with -DLINE=<cache line size in bytes from sysfs>:
 *   -DPADDED=0  both fields sit in one cache line (false sharing)
 *   -DPADDED=1  each field owns a cache line (the fix)
 *
 * The writer increments writer_count; the reader only loads reader_value.
 * In the packed build every store by the writer takes the line away from the
 * reader's core, so the reader's next load has to fetch it back from a cache
 * that holds it Modified. That load is what perf c2c reports as HITM.
 *
 * usage: false_sharing <info-file> <writer-cpu> <reader-cpu> <milliseconds>
 * The info file receives the address and layout facts the checker needs; the
 * program prints nothing on stdout.
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdalign.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#ifndef PADDED
#error "build with -DPADDED=0 or -DPADDED=1"
#endif

#ifndef LINE
#error "build with -DLINE=<cache line size in bytes>"
#endif

struct pair {
#if PADDED
	alignas(LINE) volatile uint64_t writer_count;
	alignas(LINE) volatile uint64_t reader_value;
#else
	volatile uint64_t writer_count;
	volatile uint64_t reader_value;
#endif
};

/* The struct starts on a line boundary in both builds, so the offsets below
 * decide alone whether the two fields share a line. */
static alignas(LINE) struct pair shared;

/* The stop flag is read by both threads and written once, so it gets a line
 * of its own and cannot be the contended line. */
static struct {
	alignas(LINE) volatile int flag;
	char pad[LINE - sizeof(int)];
} stop;

struct job {
	int cpu;
	uint64_t iterations;
	uint64_t sink;
};

static int pin_to(int cpu)
{
	cpu_set_t set;

	CPU_ZERO(&set);
	CPU_SET(cpu, &set);
	return pthread_setaffinity_np(pthread_self(), sizeof(set), &set);
}

static void *writer(void *arg)
{
	struct job *job = arg;
	uint64_t n = 0;

	if (pin_to(job->cpu) != 0)
		return (void *)1;
	while (!stop.flag) {
		shared.writer_count = shared.writer_count + 1;
		n++;
	}
	job->iterations = n;
	return NULL;
}

static void *reader(void *arg)
{
	struct job *job = arg;
	uint64_t n = 0, sum = 0;

	if (pin_to(job->cpu) != 0)
		return (void *)1;
	while (!stop.flag) {
		sum += shared.reader_value;
		n++;
	}
	job->iterations = n;
	job->sink = sum;
	return NULL;
}

int main(int argc, char **argv)
{
	struct job wjob = { 0 }, rjob = { 0 };
	struct timespec ts;
	pthread_t wt, rt;
	void *wret, *rret;
	long ms;
	FILE *info;

	if (argc != 5) {
		fprintf(stderr, "usage: %s <info-file> <writer-cpu> <reader-cpu> <ms>\n", argv[0]);
		return 2;
	}
	wjob.cpu = atoi(argv[2]);
	rjob.cpu = atoi(argv[3]);
	ms = atol(argv[4]);
	if (ms <= 0) {
		fprintf(stderr, "milliseconds must be positive\n");
		return 2;
	}

	if (pthread_create(&wt, NULL, writer, &wjob) != 0 ||
	    pthread_create(&rt, NULL, reader, &rjob) != 0) {
		fprintf(stderr, "pthread_create failed\n");
		return 1;
	}

	ts.tv_sec = ms / 1000;
	ts.tv_nsec = (ms % 1000) * 1000000L;
	nanosleep(&ts, NULL);
	stop.flag = 1;

	pthread_join(wt, &wret);
	pthread_join(rt, &rret);
	if (wret != NULL || rret != NULL) {
		fprintf(stderr, "could not pin a thread to its cpu\n");
		return 1;
	}

	info = fopen(argv[1], "w");
	if (info == NULL) {
		perror(argv[1]);
		return 1;
	}
	fprintf(info, "base=%#lx\n", (unsigned long)(uintptr_t)&shared);
	fprintf(info, "off_writer=%zu\n", offsetof(struct pair, writer_count));
	fprintf(info, "off_reader=%zu\n", offsetof(struct pair, reader_value));
	fprintf(info, "sizeof=%zu\n", sizeof(struct pair));
	fprintf(info, "writer_iterations=%llu\n", (unsigned long long)wjob.iterations);
	fprintf(info, "reader_iterations=%llu\n", (unsigned long long)rjob.iterations);
	if (fclose(info) != 0) {
		perror(argv[1]);
		return 1;
	}
	return 0;
}
