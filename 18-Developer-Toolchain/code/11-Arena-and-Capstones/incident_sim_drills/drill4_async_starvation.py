#!/usr/bin/env python3
"""Drill 4: Asynchronous Runtime Thread Pool Starvation & Event Loop Lag.

Simulates event loop blocking caused by a synchronous call inside an async task.
Measures event loop latency degradation and validates remediation via thread pool
delegation (asyncio.to_thread / tokio::task::spawn_blocking).
"""

import asyncio
import time

async def health_check_monitor(iterations=5, max_acceptable_lag_s=0.030):
    lags = []
    for _ in range(iterations):
        t0 = time.monotonic()
        await asyncio.sleep(0.010)  # Scheduled 10ms sleep
        t1 = time.monotonic()
        lag = (t1 - t0) - 0.010
        lags.append(max(0.0, lag))
    return sum(lags) / len(lags)

def blocking_synchronous_workload():
    # Simulates synchronous blocking disk I/O, heavy crypto, or synchronous lock
    time.sleep(0.080)  # 80ms synchronous stall on the thread

async def simulate_hung_event_loop():
    print("[DRILL 4] --- SIMULATING EVENT LOOP BLOCKING (FLAWED CODE) ---")
    
    async def flawed_worker():
        # Anti-pattern: Calling blocking synchronous code directly on the event loop
        blocking_synchronous_workload()

    # Schedule flawed worker concurrently with health check
    worker_task = asyncio.create_task(flawed_worker())
    avg_lag = await health_check_monitor(iterations=3)
    await worker_task
    
    if avg_lag > 0.020:
        print("[DRILL 4] MEASURED_LOOP_LAG: EXCEEDED_THRESHOLD (mean lag > 20.00 ms)")
        print("[DRILL 4] STARVATION_CONFIRMED: Event loop thread stalled! Health checks timed out.")
    else:
        print("[DRILL 4] MEASURED_LOOP_LAG: WITHIN_SLA (mean lag <= 20.00 ms)")

async def simulate_remediated_event_loop():
    print("\n[DRILL 4] --- SIMULATING REMEDIATED RUNTIME (THREAD POOL OFFLOAD) ---")
    
    async def remediated_worker():
        # Correct pattern: Offloading blocking operations to worker thread pool
        await asyncio.to_thread(blocking_synchronous_workload)

    worker_task = asyncio.create_task(remediated_worker())
    avg_lag = await health_check_monitor(iterations=3)
    await worker_task
    
    if avg_lag < 0.010:
        print("[DRILL 4] MEASURED_LOOP_LAG: WITHIN_SLA (mean lag < 10.00 ms)")
        print("[DRILL 4] SUCCESS: Event loop remained fully responsive during background execution.")
    else:
        print("[DRILL 4] MEASURED_LOOP_LAG: EXCEEDED_THRESHOLD (mean lag >= 10.00 ms)")

def main():
    asyncio.run(simulate_hung_event_loop())
    asyncio.run(simulate_remediated_event_loop())

if __name__ == "__main__":
    main()
