---
type: problem
track: [sde, distinguished, low-latency, quant-dev]
level:
status: solid
last_reviewed:
sources:
  - "Gil Tene and HdrHistogram contributors, HdrHistogram README (Corrected vs. Raw value recording calls), HdrHistogram project on GitHub, 2024, https://github.com/HdrHistogram/HdrHistogram/blob/master/README.md"
  - "Gil Tene and HdrHistogram contributors, AbstractHistogram.java (recordSingleValueWithExpectedInterval, copyCorrectedForCoordinatedOmission, getValueAtPercentile), HdrHistogram 2.2.2 source, 2024, https://github.com/HdrHistogram/HdrHistogram/blob/HdrHistogram-2.2.2/src/main/java/org/HdrHistogram/AbstractHistogram.java"
  - "Gil Tene and HdrHistogram contributors, Histogram.java (class Javadoc: value range, precision, and quantization), HdrHistogram 2.2.2 source, 2024, https://github.com/HdrHistogram/HdrHistogram/blob/HdrHistogram-2.2.2/src/main/java/org/HdrHistogram/Histogram.java"
  - "HdrHistogram_c contributors, hdr_histogram.h (hdr_record_corrected_value), HdrHistogram_c source on the main branch, 2026, https://github.com/HdrHistogram/HdrHistogram_c/blob/main/include/hdr/hdr_histogram.h"
  - "Gil Tene, wrk2 README: a HTTP benchmarking tool based mostly on wrk (A note about wrk2's latency measurement technique), GitHub, 2019, https://github.com/giltene/wrk2/blob/master/README.md"
  - "Gil Tene, wrk2 src/wrk.c and src/wrk.h (usec_to_next_send, response_complete, CALIBRATE_DELAY_MS), GitHub, 2019, https://github.com/giltene/wrk2/blob/master/src/wrk.c"
  - "Bianca Schroeder, Adam Wierman, and Mor Harchol-Balter, Open Versus Closed: A Cautionary Tale, 3rd Symposium on Networked Systems Design and Implementation (NSDI '06), USENIX Association, pp. 239-252, 2006, https://www.usenix.org/legacy/events/nsdi06/tech/full_papers/schroeder/schroeder_html/index.html"
  - "Ivan Prisyazhynyy, On Coordinated Omission, ScyllaDB Blog, 2021, https://www.scylladb.com/2021/04/22/on-coordinated-omission/"
  - "Grafana Labs, Open and closed models, Grafana k6 documentation, 2026, https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/"
  - "Grafana Labs, Dropped iterations, Grafana k6 documentation, 2026, https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/dropped-iterations/"
  - "Gatling Corp, Gatling injection scripting reference, Gatling documentation, 2026, https://docs.gatling.io/concepts/injection/"
  - "YCSB contributors, Measurements.java (measurement.interval), YCSB source on the master branch, 2026, https://github.com/brianfrankcooper/YCSB/blob/master/core/src/main/java/site/ycsb/measurements/Measurements.java"
  - "Tomas Senart and Vegeta contributors, Vegeta README (attack command: -rate, -workers, -max-workers), GitHub, 2026, https://github.com/tsenart/vegeta/blob/master/README.md"
  - "Gil Tene, How NOT to Measure Latency, InfoQ presentation, 2016, https://www.infoq.com/presentations/latency-response-time/"
  - "Gil Tene and HdrHistogram contributors, HdrHistogram README at tag HdrHistogram-2.2.2 (identical to master as of 2026-11), GitHub, 2024, https://github.com/HdrHistogram/HdrHistogram/blob/HdrHistogram-2.2.2/README.md"
  - "Gil Tene, wrk2 src/wrk.c parse_args() (rate required) and src/wrk.h (CALIBRATE_DELAY_MS), GitHub, 2019, https://github.com/giltene/wrk2/blob/master/src/wrk.h"
  - "YCSB contributors, DBWrapper.java (measure() records end minus intended start through measureIntended), YCSB source on the master branch, 2026, https://github.com/brianfrankcooper/YCSB/blob/master/core/src/main/java/site/ycsb/DBWrapper.java"
---

# Drill: Coordinated Omission in Load Generators

> [!summary] Scenario:
> A load test drives a service at 200 requests per second for 60 seconds, reports p99.9 = 1.200 ms against a 10 ms SLO, and the release ships.
> Callers arriving at the same 200 requests per second see p99.9 = 118.335 ms, 99 times worse, because the service freezes for 125 ms every 10 seconds.
> The generator sends a request only after the previous response returns, so each freeze costs it one slow sample and 24 requests that it never sends, and the unsent requests are exactly the ones that would have been slow.
> Your job is to prove the omission from the report's own numbers, repair the recorded data with HdrHistogram's expected-interval correction, repair the generator with a static schedule, and explain why the two repairs disagree at p99.

The definition of coordinated omission, the queueing theory of open and closed workloads, and the HdrHistogram bucket layout are owned by [[Statistics-for-Benchmarks-Coordinated-Omission-and-HDR-Histograms]].
This drill owns the hands-on diagnosis: one stalling service, one plausible generator, and the evidence trail from a passing report to a failing one.
Every number in this note that is described as measured comes from the lab's recorded output on darwin-arm64.

## Reproduction

The lab is `code/04-Measurement-Methodology/drill_coord_omission/`.
It needs `bash` and `python3` 3.10 or later with the standard library only, and it has no `# requires:` line because it uses only the standard library and no OS-specific calls; only `expected/darwin-arm64.txt` is recorded, so a Linux host has no golden file to compare against.

```bash
cd code/04-Measurement-Methodology/drill_coord_omission
python3 drill.py symptom   # the scenario and the symptom only, for a blind attempt
bash run.sh                # the full report, which is what the harness records
```

| File | Role |
| :--- | :--- |
| `loadgen_naive.py` | The generator under investigation; read this one first |
| `sim.py` | `VirtualClock` and `StallingService`: one FIFO worker that freezes during each stall window |
| `hdr.py` | A port of the HdrHistogram 2.2.2 recording, correction, and percentile paths |
| `loadgen_resetting.py` | A common half-fix, used in the solution |
| `loadgen_fixed.py` | The model solution generator |
| `drill.py` | Runs every generator against a fresh copy of the same service and prints the report |

The lab runs in virtual time, in integer microseconds, so its output is byte-identical on every run.
The generators call `clock.now()`, `clock.sleep_until()`, and `service.call()` exactly as they would call a monotonic clock, a sleep, and a blocking request.
Only the meaning of time is swapped, which removes scheduler noise from the result and leaves the generator logic as the single variable.
Request number k costs the same amount of work, between 0.800 and 1.200 ms, in every run, so differences between reports come from the generators and never from the service.

The service has one worker and a FIFO queue.
It freezes for 125 ms six times, once every 10 seconds, with each freeze starting 400 microseconds after a planned send.
A freeze is total: a request already in service stops making progress, and requests that arrive during the freeze wait for it to end.
That is the shape of a stop-the-world garbage collection pause, a blocking `fsync` on the request thread, or a `SIGSTOP`.

The generator under investigation is one short function, the whole of `loadgen_naive.py` apart from its module docstring:

```python
def run(clock, service, histogram, rate_per_s: int, duration_us: int) -> int:
    """Drive `service` at `rate_per_s` for `duration_us`; return requests sent."""
    interval = 1_000_000 // rate_per_s
    end = clock.now() + duration_us
    sent = 0
    while clock.now() < end:
        start = clock.now()
        service.call(clock)                      # blocks until the response arrives
        histogram.record_value(clock.now() - start)
        sent += 1
        clock.sleep_until(start + interval)      # pace to one request per interval
    return sent
```

## Observed Symptom

Recorded on darwin-arm64 with Python 3.15.2 on 2026-11-10; the file is `code/04-Measurement-Methodology/drill_coord_omission/expected/darwin-arm64.txt`.

```text
== Scenario ==
target rate: 200 req/s, one request every 5.000 ms, for 60 s
planned requests: 12000
service: one FIFO worker, 0.800 to 1.200 ms of work per request
stalls: 6 x 125.000 ms, 1.25% of the run
planned sends that fall inside a stall: 150 (1.25% of the plan)
SLO: p99.9 under 10.000 ms

== 1. Symptom: the load test and the callers disagree ==
  latency in ms                            p50       p90       p99     p99.9    p99.99       max
  load test report (loadgen_naive)       1.000     1.160     1.196     1.200   125.183   126.207
  independent callers, same rate         1.003     1.166    46.527   118.335   126.207   126.207
load test p99.9 1.200 ms: SLO PASS
callers   p99.9 118.335 ms: SLO FAIL
p99.9 understated by a factor of 99
```

The first row is the load test report.
The second row is the ground truth: 12,000 independent callers, one arriving every 5 ms, each timed from its own arrival to its own response.
The two rows agree to within 0.006 ms through p90, then diverge by a factor of 39 at p99 and 99 at p99.9.
The load test passes the SLO with a margin of 8x, and the callers miss it by 12x.

Answer these before opening the hints:

1. Which row is right, and for which question?
2. What evidence of the problem is inside the load test's own numbers, without the callers' row?
3. Can the recorded data be repaired after the run, and how close does the repair get?
4. What must change in `loadgen_naive.py` so that it reproduces the callers' row exactly?
5. Is one change enough, or are there two independent defects?

## Progressive Hint Ladder

> [!tip]- Hint 1: Count the samples
> A generator that targets 200 requests per second for 60 seconds plans 12,000 requests.
> Add a line that prints how many samples the histogram holds.
> If the count is short, work out when the missing requests were supposed to be sent.

> [!tip]- Hint 2: Do the time budget
> The service is frozen for 6 x 125 ms = 750 ms, which is 1.25% of a 60 second run.
> If callers arrive evenly, 1.25% of them arrive while the service is frozen.
> A report in which only 0.05% of samples are slow cannot describe those callers, whatever its percentiles say.

> [!tip]- Hint 3: Find the two events that bound the latency clock
> Read `loadgen_naive.py` and trace one iteration in which `service.call()` takes 126 ms.
> `clock.sleep_until(start + interval)` returns at once, because the deadline passed 121 ms ago.
> Write down the send times the loop produces around that iteration, next to the send times a 5 ms plan asks for.

> [!tip]- Hint 4: Name the two quantities
> Service time runs from the moment a request is actually sent to the moment its response arrives.
> Response time runs from the moment the request was supposed to be sent to the moment its response arrives, so it includes every microsecond spent waiting to be sent.
> Decide which of the two a caller experiences and which of the two the SLO is written about.

> [!tip]- Hint 5: Repair the data first
> HdrHistogram has `recordValueWithExpectedInterval(value, expectedIntervalBetweenValueSamples)`.
> Work out by hand what it records for one 125 ms sample when the expected interval is 5 ms, then apply it to every recorded sample.
> Compare the result with the callers' row at p99 and at p99.9, and explain the percentile where it is furthest off.

> [!tip]- Hint 6: Repair the generator, in two parts
> Fix the plan before the run starts: request k is intended for `t0 + k * interval`, and nothing the service does may move or remove it.
> When the generator is behind, it sends at once and keeps sending until it has caught up.
> Record `completion - intended` for every request.
> Then try each change alone, the schedule without the clock and the clock without the schedule, and look at p99.9 both times.

## Model Solution

> [!success]- Solution
> **Step 1: Prove the omission from the report's own numbers.**
>
> Recorded on darwin-arm64 (same file, section 2):
>
> ```text
> == 2. Evidence inside the load test's own numbers ==
> requests sent: 11856 of 12000 planned (144 never sent)
> achieved rate: 197.60 req/s against a target of 200
> samples slower than one send interval: 6 (0.051% of samples)
> callers slower than one send interval: 186 (1.550% of callers)
> p99.9 to max: 1.200 ms to 126.207 ms, a jump of 105x with nothing in between
> ```
>
> The count is the first tell: 11,856 samples against a plan of 12,000.
> The 144 missing requests are 24 per stall, and they are not spread over the run.
> At the first stall the request sent at 5.000 s is frozen 0.4 ms into its work and completes about 126 ms later.
> The 25 sends planned from 5.005 s to 5.125 s never happen, because the loop is blocked inside `service.call()`.
> The loop then sends one request as soon as the response returns, so the net loss is 24 sends per stall.
> Those are precisely the sends whose callers would have waited between about 122 ms and 26 ms.
>
> The time budget is the second tell: the service was frozen for 1.25% of the run, yet 0.051% of the samples are slow.
> Six slow samples in 11,856 sit above the 99.949th percentile, so p99.9 never sees them and p99.99 does.
> The cliff is the third tell: p99.9 is 1.200 ms, the maximum is 126.207 ms, and there is no sample between them.
> A real queue drains gradually, so a tail with one step and nothing on the step's slope means that the samples on the slope were not taken.
>
> **Step 2: Name the mechanism.**
>
> The generator is a closed loop: a new request starts only after the previous one completes.
> The system under test therefore controls when it is sampled, and it is sampled least when it is slowest.
> Gil Tene named this coordinated omission, and the wrk2 README states the mechanism in one sentence: slow responses cause "the load generator coordinating with the server to avoid measurement during high latency periods".
> Two separate lines in `loadgen_naive.py` carry it.
> `start = clock.now()` is taken after the previous response, so the latency clock starts at the actual send and measures service time.
> `clock.sleep_until(start + interval)` derives each deadline from the previous actual send, so a late response moves the whole schedule and the skipped sends are never made up.
>
> Both rows of the symptom are correct measurements of different things.
> The load test row is the service time of the requests the generator chose to send.
> The callers' row is the response time of a population that arrives whether or not the service is ready, which is what an SLO on user-visible latency is about.
>
> **Step 3: Repair the data with the expected-interval correction.**
>
> `recordSingleValueWithExpectedInterval()` in HdrHistogram 2.2.2 records the value, then loops `missingValue` from `value - expectedInterval` down in steps of `expectedInterval` while `missingValue >= expectedInterval`, recording each one.
> The lab's `hdr.py` ports that loop, and `drill.py` replays every naive sample through it with an expected interval of 5,000 microseconds.
>
> Recorded on darwin-arm64 (same file, section 3):
>
> ```text
> == 3. Repair the data: HdrHistogram expected-interval correction ==
> one 125.000 ms sample at a 5.000 ms expected interval backfills 24 samples, 120.000 ms down to 5.000 ms
> samples: 11856 recorded + 143 backfilled = 11999
>   latency in ms                            p50       p90       p99     p99.9    p99.99       max
>   naive, corrected after the fact        1.003     1.165    29.711   119.743   125.183   126.207
>   independent callers, same rate         1.003     1.166    46.527   118.335   126.207   126.207
> corrected p99 is 64% of the callers' p99: backfilled samples never queue behind each other
> ```
>
> The correction restores the population: 11,999 samples against a plan of 12,000.
> Five of the six slow samples backfill 24 values and one, at 124.710 ms, backfills 23.
> At p99.9 it lands at 119.743 ms against a true 118.335 ms, which is 1.2% high.
> At p99 it lands at 29.711 ms against a true 46.527 ms, which is 36% low.
>
> The gap at p99 is a modeling error, not a rounding error.
> The correction assumes that each missing request would have waited only for the stall to end, so its backfilled values fall by one full interval per step: about 126, 121, 116 ms, and so on.
> Real callers queue behind each other.
> The caller who arrives j intervals into the stall waits for the stall to end and then for the j callers ahead of it, at about 1 ms of service each, so the true waits fall by only 4 ms per step: 126.163, 121.990, 117.926, 113.926 ms at the first stall.
> The backlog also outlives the stall: 5 more callers per stall arrive while it drains and wait longer than one interval, and the correction knows nothing about them.
> The slope error is the ratio of service time to send interval, which is the utilization, here about 20%.
> At low utilization the correction is close, and near saturation it understates the tail badly.
>
> Use the correction when the run cannot be repeated, label the result as corrected, and state the expected interval.
> The HdrHistogram Javadoc calls the at-recording method and `copyCorrectedForCoordinatedOmission()` mutually exclusive, so apply exactly one of them to a data set.
>
> **Step 4: Rule out the half-fix.**
>
> A frequent first attempt keeps a `next_send` variable, measures from it, and advances it with `next_send = max(next_send + interval, clock.now())`.
> That is `loadgen_resetting.py`.
>
> Recorded on darwin-arm64 (same file, section 4):
>
> ```text
> == 4. A half-fix: intended-time clock, schedule reset when behind ==
> requests sent: 11856 of 12000 planned (144 never sent)
>   latency in ms                            p50       p90       p99     p99.9    p99.99       max
>   loadgen_resetting                      1.000     1.160     1.196     1.200   125.183   126.207
> p99.9 1.200 ms: SLO PASS
> identical to the naive report, sample for sample: yes
> ```
>
> The report is identical to the naive one, sample for sample.
> The `max()` rebuilds the schedule from the current time whenever the generator is behind, so the intended send time and the actual send time are always equal and the backlog is dropped.
> The ScyllaDB article calls this a dynamic schedule in a queueless generator.
>
> **Step 5: Repair the generator.**
>
> The fix is a static schedule plus a clock that starts at the plan:
>
> ```python
> interval = 1_000_000 // rate_per_s
> planned = duration_us // interval
> t0 = clock.now()
> for k in range(planned):
>     intended = t0 + k * interval
>     clock.sleep_until(intended)              # returns at once when behind schedule
>     actual = clock.now()
>     service.call(clock)
>     done = clock.now()
>     response_hist.record_value(done - intended)
>     service_hist.record_value(done - actual)
> ```
>
> Recorded on darwin-arm64 (same file, sections 5 and 6):
>
> ```text
> == 5. Repair the generator: static schedule, latency from intended send time ==
> requests sent: 12000 of 12000 planned (0 never sent)
>   latency in ms                            p50       p90       p99     p99.9    p99.99       max
>   response time (from intended send)     1.003     1.166    46.527   118.335   126.207   126.207
>   service time (from actual send)        1.000     1.160     1.196     1.200   126.207   126.207
>   independent callers, same rate         1.003     1.166    46.527   118.335   126.207   126.207
> response time p99.9 118.335 ms: SLO FAIL
> requests sent late and timed from the plan: 186
>
> == 6. Schedule x clock: both must change ==
>   schedule   clock starts at    sent  p99.9 ms  SLO   source
>   resetting  actual send       11856     1.200  PASS  loadgen_naive
>   resetting  intended send     11856     1.200  PASS  loadgen_resetting
>   static     actual send       12000     1.200  PASS  loadgen_fixed, service time
>   static     intended send     12000   118.335  FAIL  loadgen_fixed, response time
> ```
>
> All 12,000 planned requests are sent, the response-time row matches the callers' row in every column, and the SLO verdict flips to FAIL.
> 186 requests left later than planned, 31 per stall: the 25 planned inside the stall and the 6 planned while the backlog drained.
>
> The service-time row is the important control.
> It comes from the same run, with the same static schedule and the same 12,000 requests, and its p99.9 is still 1.200 ms.
> The backlog is sent in a burst after each stall, each of those requests completes about 1 ms after its actual send, and a clock that starts at the actual send records 1 ms for a caller who waited 100.
> The matrix in section 6 is the answer to question 5: there are two independent defects, and fixing either one alone leaves p99.9 at 1.200 ms.
> A sample count that matches the plan is necessary, and it is not sufficient.
>
> This is the model wrk2 implements.
> Its `response_complete()` computes `expected_latency_start = c->thread_start + (c->complete_at_last_batch_start / c->throughput)` and records `now - expected_latency_start` into the reported histogram.
> It also records `now - c->actual_latency_start` into a second histogram that `--u_latency` prints, which is the service-time row of this lab.
>
> **Step 6: Check the repair.**
>
> Recorded on darwin-arm64 (same file, section 7):
>
> ```text
> == 7. Checks ==
> fixed generator reproduces the callers' samples exactly: yes
> histogram percentiles land in the same bucket as an exact sort: yes
> largest quantization error against an exact sort: 0.045% (3 significant digits allow 0.1%)
> histogram layout for 1 us to 1 hour at 3 digits: 22 buckets x 2048 sub-buckets, 23552 counters
> ```
>
> The fixed generator reproduces the callers' 12,000 samples exactly, value for value.
> That equality holds because the service has one FIFO worker: a request completes at the same instant whether it waits in the service's queue or in the generator's schedule.
> It does not hold in general.
> With parallel workers, real callers would be served side by side after a stall while a generator with one connection would feed them in one at a time, so the generator would report its own queue as the service's latency.
> That is an argument from the queueing structure, not a measurement, because the lab simulates only one worker; the rule it implies is to give the generator enough connections that it is never the bottleneck, and to check its own send lateness before trusting its tail.
>
> The remaining lines audit the histogram port: every reported percentile falls in the same bucket as an exact sort of the raw samples, the worst quantization error is 0.045% against the 0.1% that 3 significant digits allow, and the layout for 1 microsecond to 1 hour is 23,552 counters.
> At 8 bytes per counter that is 188,416 bytes, which matches the "around 185KB" the HdrHistogram README gives for the same configuration.

## Core Takeaway

A latency percentile describes a population of requests, and the load generator decides which requests are in the population.
A generator that waits for each response before it schedules the next send lets the system under test pick its own sample times, and a system picks them when it is healthy.
The result is a report that is accurate for every request that was sent and silent about every request that should have been.

Three invariants make a latency report trustworthy, and each one is checkable from the report alone:

- The schedule is a function of the start time and the request number only, so no response can move or remove a planned send.
- The latency clock starts at the intended send time, so waiting to be sent counts as waiting.
- The number of requests sent equals the number planned, or the report states the difference and why.

The expected-interval correction repairs a histogram after the fact, and it is an estimate built on a no-queueing assumption, so it belongs on data that cannot be re-collected and never in place of a correct generator.
The mechanism, its queueing theory, and the histogram that makes high percentiles cheap to record are owned by [[Statistics-for-Benchmarks-Coordinated-Omission-and-HDR-Histograms]].
A latency gate built on a closed-loop generator inherits the defect, which is why [[Performance-Regression-Detection-in-CI]] depends on this drill's invariants.
The stall itself is an off-CPU event, and [[Off-CPU-Analysis-Thread-States-and-Causal-Profiling]] covers how to find where a frozen thread was waiting.
The RED method's duration signal has the same blind spot when it is timed inside the handler, which [[USE-RED-and-Systems-Performance-Methodologies]] covers.

## Commands and Numbers to Know

| Item | Value | Platform and date | Why it matters |
| :--- | :--- | :--- | :--- |
| `recordValueWithExpectedInterval(value, expectedInterval)` | Records `value`, then `value - k * interval` while the result is at least one interval | Both (as of 2026-11, HdrHistogram 2.2.2, Java) | At-recording correction for a constant intended rate |
| `copyCorrectedForCoordinatedOmission(expectedInterval)` | Same backfill applied to a finished histogram | Both (as of 2026-11, HdrHistogram 2.2.2, Java) | Post-recording correction; mutually exclusive with the at-recording form |
| `hdr_record_corrected_value(h, value, expected_interval)` | C equivalent of the at-recording form | Both (as of 2026-11, HdrHistogram_c main) | Same correction for C and C++ generators |
| Backfill for one 125 ms sample at a 5 ms interval | 24 samples, 120 ms down to 5 ms | Both (as of 2026-11, this lab) | Size of the hole one stall leaves in a closed-loop run |
| wrk2 `-R`, `--rate` | Total requests per second across all connections; required, because `parse_args()` exits when it is 0, although the README says the default is 1000 | Both (as of 2026-11, wrk2 master, last commit 2019-09) | Sets the plan that latency is measured against |
| wrk2 `--latency` and `--u_latency` | Corrected distribution, and the uncorrected one next to it | Both (as of 2026-11, wrk2 master) | The gap between the two is the coordinated omission in the run |
| wrk2 calibration period | 10 seconds (`CALIBRATE_DELAY_MS 10000`) | Both (as of 2026-11, wrk2 master) | The README warns that runs shorter than 10 to 20 seconds may not be useful |
| wrk2 catch-up rate | Twice the per-connection rate (`catch_up_throughput = throughput * 2`) | Both (as of 2026-11, wrk2 master) | A connection that is behind sends faster than planned, and is still timed from the plan |
| wrk2 histogram | `hdr_init(1, MAX_LATENCY, 3, ...)` with `MAX_LATENCY` of 24 hours in microseconds | Both (as of 2026-11, wrk2 master) | 3 significant digits from 1 microsecond up |
| k6 open-model executors | `constant-arrival-rate`, `ramping-arrival-rate` | Both (as of 2026-11, k6 latest docs) | Iterations start independently of iteration completion |
| k6 `dropped_iterations` | Counter of scheduled iterations that never ran | Both (as of 2026-11, k6 latest docs) | Arrival-rate executors drop iterations when no VU is free, which is omission made visible |
| Gatling open injection | `injectOpen` with `constantUsersPerSec`, `rampUsersPerSec` | Both (as of 2026-11, Gatling docs) | Arrival rate is the control variable |
| Gatling closed injection | `injectClosed` with `constantConcurrentUsers`, `rampConcurrentUsers` | Both (as of 2026-11, Gatling docs) | Concurrency is the control variable |
| YCSB `measurement.interval` | `op` (default), `intended`, or `both` | Both (as of 2026-11, YCSB master) | `intended` measures from the scheduled start; the default does not |
| Vegeta `-rate` | Default `50/1s`; `0` means as fast as possible | Both (as of 2026-11, Vegeta master) | A fixed rate with growing workers is an open model |
| Vegeta `-max-workers` | Default 18446744073709551615 | Both (as of 2026-11, Vegeta master) | With `-rate=0` it models a fixed set of serial users, which is a closed loop |
| HdrHistogram layout, 1 to 3,600,000,000 at 3 digits | 22 buckets x 2048 sub-buckets, 23,552 counters | Both (as of 2026-11, this lab's port of 2.2.2) | Fixed footprint regardless of sample count |

## Pitfalls

> [!warning] Pitfall: correcting twice
> A histogram recorded with `recordValueWithExpectedInterval()` and then passed through `copyCorrectedForCoordinatedOmission()` is backfilled twice.
> Correcting the output of a generator that already measures from intended send times does the same thing by another route.
> Either mistake inflates the tail, and the inflated tail looks like a careful, pessimistic result.

> [!warning] Pitfall: an expected interval that the generator never kept
> The correction needs one known, constant interval between intended sends.
> A generator with randomized arrivals, think time, or a ramp has no such interval, and plugging in the average produces a tail with no defined meaning.
> The wrk2 README makes the same point about its own technique: a varying or stochastic plan needs a model that can give the intended start time of each request.

> [!warning] Pitfall: reading a matching sample count as proof
> The service-time row of the fixed generator holds all 12,000 samples and still reports p99.9 = 1.200 ms.
> Sample count catches a dropped schedule and says nothing about where the latency clock starts.

> [!warning] Pitfall: an open loop that quietly closes
> An arrival-rate generator needs somewhere to run each new request.
> When its pool of virtual users, connections, or workers is exhausted, it either drops the scheduled request or waits for a free slot, and waiting for a slot is a closed loop again.
> k6 counts the unsent iterations in `dropped_iterations`, so a nonzero value in an arrival-rate scenario means the latency numbers describe fewer requests than the plan.

> [!warning] Pitfall: charging the generator's pauses to the service
> Timing from the intended send assigns every delay to the system under test, including a garbage collection pause, a descheduled thread, or a saturated CPU on the generator host.
> The wrk2 README bounds its own accuracy at about 1 ms because of operating system sleep behavior.
> Record the generator's send lateness as its own histogram and control the host as [[Benchmark-Methodology-Noise-and-Environment-Control]] describes.

> [!warning] Pitfall: running an open loop past saturation
> If the planned rate exceeds what the service can sustain, an open-loop backlog grows for as long as the test runs, and every percentile becomes a function of the test duration.
> That result is correct and it answers a different question, so report the sustainable rate first and latency only at rates under it.

## Practice Drill

> [!tip] Practice Drill: Change one variable and predict the report
> Each exercise changes one constant at the top of `drill.py`; write the prediction down before running.
> 1. Raise the service time to 4.000 ms flat, which is 80% utilization, and predict whether the corrected p99 moves closer to the callers' p99 or further from it.
> 2. Keep the stalls and double the rate to 400 requests per second, then predict the number of requests the naive loop never sends and the percentile at which its six slow samples become visible.
> 3. Replace the six 125 ms stalls with sixty 12.5 ms stalls, the same 1.25% of the run, and predict which rows of the schedule and clock matrix still pass the SLO.
> 4. Give `StallingService` a second worker and compare the fixed generator with the independent callers; the "reproduces the callers' samples exactly" check is expected to fail, and the task is to explain the direction of the difference.
> 5. Port `loadgen_fixed.py` to a real clock with `time.monotonic_ns()` and a real socket, add a histogram of `actual - intended`, and find the rate at which your own host's send lateness reaches 1 ms.

## Interview Questions

> [!question] L5: What is coordinated omission, and which line of a typical load generator causes it?
>
> > [!success]- Answer
> > It is the loss of latency samples that happens when the measuring system waits for the measured system, so that slow periods are sampled less often than fast ones.
> > The cause is a send that is gated on the previous response: a loop of send, wait, record, sleep, with the next send time derived from the previous actual send.
> > During a stall the loop takes one slow sample and skips every send that was due in the meantime, and those skipped sends are the ones that would have been slow.
> > In the lab a 125 ms stall at a 5 ms interval produces 1 slow sample and 24 unsent requests.

> [!question] L5: Predict the output: an HdrHistogram receives the single call `recordValueWithExpectedInterval(125000, 5000)`, so what is its total count, and what are the smallest and largest recorded values?
>
> > [!success]- Answer
> > The total count is 25.
> > The method records 125,000, then loops from 120,000 down in steps of 5,000 while the value is at least 5,000, which adds 24 values.
> > The largest is 125,000 and the smallest is 5,000; nothing is recorded under one interval, because a request that waits less than an interval delays no other planned send.
> > The lab prints the same arithmetic: one 125.000 ms sample at a 5.000 ms interval backfills 24 samples, 120.000 ms down to 5.000 ms.

> [!question] L5: What is the difference between service time and response time, and which one belongs in an SLO?
>
> > [!success]- Answer
> > Service time runs from the actual send to the response, and response time runs from the intended send, or the caller's arrival, to the response.
> > Response time is service time plus all waiting, in the caller's queue, in a connection pool, or in the server's accept backlog.
> > A user-facing SLO is about response time, because a caller experiences the wait.
> > In the lab's fixed generator the same 12,000 requests give a service-time p99.9 of 1.200 ms and a response-time p99.9 of 118.335 ms.

> [!question] L6: Debug this: a team says it fixed coordinated omission by timing from the intended send, yet the report did not change, so where is the defect?
>
> ```python
> next_send = clock.now()
> while next_send < end:
>     clock.sleep_until(next_send)
>     service.call(clock)
>     histogram.record_value(clock.now() - next_send)
>     next_send = max(next_send + interval, clock.now())
> ```
>
> > [!success]- Answer
> > The last line rebuilds the schedule from the current time whenever the loop is behind.
> > After a slow call, `clock.now()` is larger than `next_send + interval`, so the next intended send becomes "now" and every send that was due during the slow call is dropped.
> > Because the intended time is reset to the actual time, `clock.now() - next_send` is service time under a different name.
> > In the lab this generator sends 11,856 of 12,000 requests and its samples are identical to the naive generator's, one for one.
> > The fix is `next_send = t0 + k * interval` with k counting every planned request, so the schedule depends only on the start time.

> [!question] L6: With no access to the generator source, what do you check in a report that shows 11,856 samples, p99.9 = 1.200 ms, and max = 126.207 ms for a 60 second run at a target of 200 requests per second?
>
> > [!success]- Answer
> > Compare the sample count with rate times duration: 12,000 planned against 11,856 recorded means 144 requests were never sent.
> > Compare achieved throughput with the target: 197.60 against 200 is the same fact in another unit.
> > Look for a cliff: a 105x jump from p99.9 to the maximum with no samples between them is the signature of a stall that was sampled once.
> > Estimate the stall budget: if the maximum is a stall, 126 ms at a 5 ms interval should have produced about 24 slow samples per stall, and the report has 6 in total.
> > Then ask for the uncorrected and corrected distributions side by side, or rerun with a tool that measures from the intended send.

> [!question] L6: Why does the expected-interval correction land within 1.2% of the truth at p99.9 and 36% low at p99 in the lab?
>
> > [!success]- Answer
> > The top of the tail is set by the stall length, which the single slow sample captures exactly, so the first backfilled values are nearly right.
> > Further down, the correction assumes each missing request waits only for the stall to end, so its values drop by one interval, 5 ms, per step.
> > Real callers also wait for the callers queued ahead of them, about 1 ms each, so the true waits drop by 4 ms per step, and the two lines diverge with depth.
> > The correction also has no samples for callers who arrive while the backlog drains.
> > The slope error equals the utilization, service time over interval, so the correction degrades as the service approaches saturation.

> [!question] L6: How does wrk2 avoid coordinated omission without sending requests asynchronously?
>
> > [!success]- Answer
> > It keeps a constant-throughput plan per connection and measures each response from the time the request should have been sent under that plan.
> > In `response_complete()` the expected start is `thread_start + complete_at_last_batch_start / throughput`, and the recorded latency is the current time minus that value.
> > A connection that falls behind sends at up to twice its planned rate until it catches up, and those late requests are still timed from the plan.
> > The uncorrected timing is kept in a second histogram for `--u_latency`.
> > The cost is that the connection is a serial pipe, so too few connections make the generator a bottleneck and its own queue shows up as latency.

> [!question] L7: When is a closed-loop generator the correct model, and what do you report from it?
>
> > [!success]- Answer
> > It is correct when the real callers are a fixed population that waits for each response: a batch pipeline with a fixed worker pool, a connection-pool-limited internal client, or a user who cannot click again until the page loads.
> > Schroeder, Wierman, and Harchol-Balter define the closed model as one in which new arrivals are triggered only by completions followed by think time, and their Principle (i) is that, for a given load, mean response times are significantly lower in closed systems than in open systems.
> > In a closed system a stall costs throughput and not queueing delay, so the honest report is throughput over time, the stall durations, and per-request service time.
> > What is never correct is to take latency percentiles from a closed loop and present them as the experience of an open population, such as internet traffic that arrives regardless of server state.
> > The same paper defines a partly open model, in which users arrive by an outside process and each completed request is followed by another with probability p, and a generator for a session-based workload has to match both parts.

> [!question] L7: Does coordinated omission affect production monitoring, where there is no load generator?
>
> > [!success]- Answer
> > Yes, whenever the timer starts after the request has already waited; this follows from the definition of the effect and is engineering reasoning here, not a measured result.
> > A handler-level timer starts when the handler runs, so a frozen process records nothing for requests that are sitting in the accept backlog, a proxy queue, or a client's connection pool.
> > Those requests are eventually served and timed quickly, which is the service-time row again.
> > The remedies are to time at the caller or at the nearest layer that does not share the stall, such as the load balancer or the client library, and to run a fixed-rate probe whose schedule does not depend on responses.
> > A second check is the same count invariant: requests received per second should match requests offered per second in every interval, and a dip in the first with no dip in the second is a stall.

> [!question] L7: Your open-loop generator reports a p99 latency that keeps growing the longer the test runs; is the generator broken?
>
> > [!success]- Answer
> > Usually not; the planned rate is above the service's sustainable throughput.
> > Arrivals exceed completions, the backlog grows without bound, and each new request waits longer than the previous one, so every percentile depends on test duration.
> > Confirm it by comparing completed requests per second with planned requests per second, and by checking the generator's send lateness to rule out a generator bottleneck.
> > Then find the highest rate at which the backlog stays bounded, and report latency only at rates under it.
> > A closed-loop generator would have hidden this by slowing down, which is the reason it reports a comfortable latency for an overloaded system.

> [!question] L8+: You own performance standards for 200 teams whose load tests feed release gates; what do you standardize so that a coordinated-omission report cannot pass a gate?
>
> > [!success]- Answer
> > Standardize the report contract before the tool: planned request count, sent count, dropped count, target and achieved rate, the response-time distribution from intended send, the service-time distribution, and the generator's send-lateness distribution.
> > Make the gate reject a run whose sent count is below plan beyond a stated tolerance, whose dropped count is nonzero, or whose send lateness exceeds a bound, because each of those means the latency numbers describe the wrong population.
> > Provide one paved-road configuration per tool that uses an arrival-rate model, and treat closed-loop latency gates as a documented exception with an owner.
> > Require histograms, not precomputed percentiles, as the stored artifact, so that runs can be merged and corrected values can be recomputed and labeled.
> > Migrate by running both models side by side for a release cycle and publishing the difference per service, since that difference is the size of the risk each team has been carrying.
> > Record the decision and its exceptions in the form [[Measurement-Methodology-Decision-Records]] describes, and wire the checks into the gate that [[Performance-Regression-Detection-in-CI]] defines.

## Further Reading

- HdrHistogram README, "Corrected vs. Raw value recording calls": the library's own statement of what the expected-interval correction adds and when corrected and raw data sets differ.
- `AbstractHistogram.java` at HdrHistogram 2.2.2: `recordSingleValueWithExpectedInterval()` is the short loop the lab ports, and the Javadoc on `copyCorrectedForCoordinatedOmission()` states the mutual-exclusion rule.
- wrk2 README, "A note about wrk2's latency measurement technique": the clearest short description of the mechanism, with a 1.4 second pause example in which the corrected p99 is 1.27 s and the uncorrected p99 is 6.04 ms.
- wrk2 `src/wrk.c`: `usec_to_next_send()` and `response_complete()` show the plan, the catch-up rule, and both histograms in about 100 lines.
- Schroeder, Wierman, and Harchol-Balter, "Open Versus Closed: A Cautionary Tale" (NSDI '06): the queueing-theory basis for choosing a generator model, including partly open systems.
- Ivan Prisyazhynyy, "On Coordinated Omission" (ScyllaDB, 2021): a taxonomy of static and dynamic schedules, queueing and queueless generators, and latency correction, with YCSB as the worked tool.
- Grafana k6 documentation, "Open and closed models" and "Dropped iterations": how one mainstream tool exposes the model choice and reports the requests it could not send.
- Gil Tene, "How NOT to Measure Latency" (InfoQ, 2016): the long-form talk on latency measurement by the author of HdrHistogram and wrk2; this note cites it for background only and takes no numbers from it.

## Related

- Prerequisites: [[Statistics-for-Benchmarks-Coordinated-Omission-and-HDR-Histograms]]
- See also: [[14-Low-Latency-Systems/07 - Time & Measurement/Coordinated Omission in Low Latency Systems|Coordinated Omission in Low Latency Systems]]
- Lab: `code/04-Measurement-Methodology/drill_coord_omission/`
