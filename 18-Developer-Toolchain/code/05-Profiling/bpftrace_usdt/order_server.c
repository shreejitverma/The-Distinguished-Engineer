/*
 * order_server.c - a toy order handler with two USDT probes.
 *
 *   orders:order_start(id, qty)              no semaphore: the arguments are already live
 *   orders:order_done(id, notional, symbol)  semaphore-gated: notional is expensive to compute
 *
 * The program counts how often the expensive argument preparation ran. With no tracer
 * attached to order_done the count stays 0; a tracer that enables the probe raises the
 * semaphore and the count follows the number of orders.
 */
#include <stdio.h>

#include "sdt_min.h"

SDT_DEFINE_SEMAPHORE(orders_order_done_semaphore);

struct order {
	int id;
	int qty;
	long price_ticks;
	const char *symbol;
};

static const struct order BOOK[] = {
	{ 101, 10, 18950, "AAPL" }, { 102, 250, 41275, "MSFT" }, { 103, 5, 13310, "NVDA" },
	{ 104, 75, 17780, "AMZN" }, { 105, 1200, 2515, "F" },
};

static int expensive_preparations;

/* Stands in for work an untraced process must never pay for: serialization, hashing, a lock. */
__attribute__((noinline)) static long notional_ticks(const struct order *o)
{
	expensive_preparations++;
	return (long)o->qty * o->price_ticks;
}

__attribute__((noinline)) static long handle_order(const struct order *o)
{
	int id = o->id;
	int qty = o->qty;
	long filled = 0;

	SDT_PROBE2(orders, order_start, 0, id, qty);

	for (int lot = 0; lot < qty; lot++)
		filled += o->price_ticks & 0xff;

	if (SDT_ENABLED(orders_order_done_semaphore)) {
		long notional = notional_ticks(o);
		unsigned long symbol = (unsigned long)o->symbol;

		SDT_PROBE3(orders, order_done, orders_order_done_semaphore, id, notional, symbol);
	}
	return filled;
}

int main(void)
{
	long total = 0;
	int handled = 0;

	for (unsigned i = 0; i < sizeof(BOOK) / sizeof(BOOK[0]); i++) {
		total += handle_order(&BOOK[i]);
		handled++;
	}
	printf("orders handled: %d, fill checksum: %ld\n", handled, total);
	printf("expensive argument preparations: %d\n", expensive_preparations);
	return 0;
}
