# S6a development load harness

Stdlib Python only. These files are development tools and are outside the Python
package and hub release artifacts. They change no hub routes or writer code.
The matrix uses disposable databases and a disposable admin token. Never point
it at a live database. Measurements from a shared host are provisional.

## Short smoke matrix

Build the hub separately through Brigade if the binary is missing. Run the
following command inside the executable verification script described by the
repository workflow:

```sh
python engines/hub/bench/matrix.py --output /tmp/hub-smoke \
  --seconds 1 --warmup 0.2 --rate 2 --clients 1
```

This makes three independent runs for each combination of Python and Rust
managed proxy, closed and open loop, reference and 1 vCPU / 512 MiB allotments.
Each run starts with an empty database. Initial and final database sizes and effective server limits are recorded. Rust launches its keyed Python upstream
in upstream mode. Both processes inherit the server's cgroup. The client runs
outside it. All HTTP requests bind the same numeric loopback source.

The constrained server runs through `systemd-run --user --scope -p CPUQuota=100%
-p MemoryMax=512M`. A guard reads its effective cgroup v2 limits before executing
the hub. Missing delegation, unapplied limits and startup failure stop the
matrix with a nonzero exit. `--allotment reference` permits an explicit reference
smoke when delegation is unavailable. it does not satisfy the constrained gate.

Default payloads are one unique event per attempt with fixed field sizes apart
from the sequence and run identifier. The admin-write flag permits that fake
node. There are no client retries. `--connection close` is the pinned default.
`--connection keep-alive` reuses each worker's connection if the server permits.
Open loop schedules arrivals at `--rate` requests/s independently of responses.
A bounded client queue records every rejected arrival as `client-backlog-full`.
Closed loop sends the next request only after the prior response and an explicit
think time of `1 / rate` seconds per worker. It has no fixed offered rate.
Warmup attempts are retained and excluded from the measured summary. Requests
admitted before the deadline drain afterward, bounded by socket idle timeout.
Latencies include scheduling delay. service time and dispatch lag are separate.
Percentiles use nearest rank and include HTTP failures. Unsent arrivals have no
latency. Throughput includes completed errors. inspect the errors count too.

The provisional local execution results are in [SMOKE.md](SMOKE.md).

## Quiet-hardware measurement

Use the same binary, Python revision, OS, CPU affinity, connection mode, payload,
database starting size, flags, warmup, duration, worker count and offered rate for
every comparison. Record the invocation alongside the reports. Pin client cores
with `taskset -c <cores>` and keep those cores out of the server's CPU allocation
when using dedicated reference hardware. CPUQuota limits CPU time, not affinity.
Choose load levels and duration with the operator on quiet hardware. Do not run
heavy loads on the shared workstation. The three-run matrix smoke checks the
tool, and supplies no throughput acceptance claim.

For a preseeded database or another workload, launch an isolated target yourself
and use `load.py` with `--database`, `--flags`, `--payload`, `--method` and `--path`.
`--payload` sends exact bytes, so repeated event ids may measure deduplication.
Use a fresh seeded database copy per run and record its initial size separately.
The matrix intentionally uses the empty-database unique-event workload only.

One authorized end-to-end run against the operator's TLS front uses the same
`load.py` options with an `https://` URL. Supply its credential through the
`BRIGADE_BENCH_TOKEN` environment, never an argument. TLS certificates are
verified with the system trust store. That report is observational and never
gated. The endpoint and credentials are omitted from reports. No TLS run is
claimed by the local matrix. Keep TLS reports private until error bodies have
been checked for private details.

## Report schema, version 1

Each run writes a UTF-8 JSON object. The following fields remain stable as
writer and metrics slices land:

| Field | Meaning |
|---|---|
| `schema_version`, `provisional`, `started_unix_s` | Format and measurement qualification |
| `machine` | Machine role, OS, architecture, CPU count, memory, actual client affinity |
| `host_load_average.start`, `.end` | Host 1, 5 and 15 minute load averages |
| `conditions` | Target, allotment, transport, TLS gate status, mode, connection, duration, warmup, workers, backlog, rate, think time, timeout, sample interval, method, path, payload fingerprint, database sizes and flags |
| `summary` | Measured attempt and error counts, completed/s, mean service time and p50/p90/p95/p99/p100 latency in ms |
| `telemetry.queue_depth` | Per-metric summaries of `brigade_hub_writer_queue_depth`, `reader_queue_depth` and `cpu_queue_depth`, or the marker string `not available until S4a/S9` when no sample held a finite value |
| `telemetry.batch_sizes` | The same summary for `brigade_hub_writer_batch_size`, or the marker |
| `telemetry.checkpoint_stalls` | The same summary for `brigade_hub_checkpoint_busy_total` and `brigade_hub_checkpoint_stall_frames`, or the marker |
| `telemetry.samples` | Timestamped `/metrics` status and decoded value: a JSON body as is, metrics text as a map from each sample line (labels included) to its number, with `NaN` stored as `null`. Python may return 404 |
| `attempts` | Every warmup and measured arrival, including unsent ones |

A telemetry summary is keyed by the metric name without `brigade_hub_`. Each holds
`metric`, `samples`, `min`, `max`, `first`, `last` and `values`, the finite readings in
sample order. Writer gauges report the last batch finished, so `batch_sizes` is only
as fine as `--sample-interval`, and a batch that starts and ends between two samples
is not seen. A counter such as `checkpoint_busy_total` is read as `last - first`. The
marker keeps its original text so reports stay comparable. It now means "no finite
value in any sample": a Python target, or a Rust front with no writer, which exports
`NaN`. The columns went from marker-only to marker or summary without a change to
their names or to the other fields, so `schema_version` stays 1.

Each attempt records its id, phase, scheduled and started offsets, HTTP status,
reason, complete error body, Retry-After, payload bytes, service time, dispatch
lag and latency. Transport errors record the exception class and errno without
endpoint names. A truncated HTTP 200 body is a transport error. HTTP 502, 503
and 504 reasons pass through unchanged, including `proxy-inflight-full`. HTTP
failures lacking a JSON reason retain their error bodies. Missing writer
telemetry is never estimated from client timing or WAL file size. Raw metrics
remain available for interpreting future counters without changing columns.

Run tests through Brigade with `python -m unittest discover -s engines/hub/bench
-p 'test_*.py'`, and run `tests/test_v2_plan_consistency.py`. No dependencies are
installed by this harness.
