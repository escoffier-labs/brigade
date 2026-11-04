# Provisional S6a smoke record

Shared reference workstation: Linux x86_64, 24 logical CPUs, 66,843,295,744 bytes RAM.
Client affinity: CPUs 0 through 23. Debug Rust build at base `dbd6999f8`.
One client, close connections, 1 s measured, 0.2 s warmup, 2 arrivals/s in open loop.
Closed loop uses 0.5 s think time after each response. Every run starts with an empty database.
Each run measured 2 attempts and no errors. These samples prove execution only.
Quiet-hardware measurement and the observational operator TLS run remain pending.

| Allotment | Target | Mode | Run | p50 ms | p99 ms | Start host load (1/5/15 min) |
|---|---|---|---|---|---|---|
| 1cpu-512m | python | closed | 1 | 3.582 | 5.503 | 15.119 / 11.827 / 8.315 |
| 1cpu-512m | python | closed | 2 | 4.605 | 6.358 | 15.119 / 11.827 / 8.315 |
| 1cpu-512m | python | closed | 3 | 4.822 | 7.223 | 15.119 / 11.827 / 8.315 |
| 1cpu-512m | python | open | 1 | 3.825 | 7.063 | 15.430 / 11.946 / 8.373 |
| 1cpu-512m | python | open | 2 | 4.832 | 5.279 | 15.430 / 11.946 / 8.373 |
| 1cpu-512m | python | open | 3 | 4.543 | 49.704 | 15.430 / 11.946 / 8.373 |
| 1cpu-512m | rust-proxy | closed | 1 | 60.899 | 89.277 | 14.915 / 11.897 / 8.376 |
| 1cpu-512m | rust-proxy | closed | 2 | 7.731 | 25.981 | 14.915 / 11.897 / 8.376 |
| 1cpu-512m | rust-proxy | closed | 3 | 5.196 | 7.833 | 14.915 / 11.897 / 8.376 |
| 1cpu-512m | rust-proxy | open | 1 | 4.612 | 7.855 | 14.201 / 11.799 / 8.363 |
| 1cpu-512m | rust-proxy | open | 2 | 7.117 | 7.741 | 14.201 / 11.799 / 8.363 |
| 1cpu-512m | rust-proxy | open | 3 | 6.402 | 6.933 | 14.201 / 11.799 / 8.363 |
| reference | python | closed | 1 | 35.784 | 44.423 | 15.552 / 11.604 / 8.148 |
| reference | python | closed | 2 | 5.970 | 43.202 | 15.347 / 11.627 / 8.175 |
| reference | python | closed | 3 | 5.169 | 5.423 | 15.347 / 11.627 / 8.175 |
| reference | python | open | 1 | 4.882 | 5.449 | 15.347 / 11.627 / 8.175 |
| reference | python | open | 2 | 5.111 | 6.927 | 15.239 / 11.667 / 8.207 |
| reference | python | open | 3 | 10.404 | 12.471 | 15.239 / 11.667 / 8.207 |
| reference | rust-proxy | closed | 1 | 6.227 | 15.855 | 15.239 / 11.667 / 8.207 |
| reference | rust-proxy | closed | 2 | 15.579 | 30.957 | 15.781 / 11.838 / 8.281 |
| reference | rust-proxy | closed | 3 | 48.009 | 58.359 | 15.781 / 11.838 / 8.281 |
| reference | rust-proxy | open | 1 | 5.859 | 8.509 | 15.478 / 11.841 / 8.301 |
| reference | rust-proxy | open | 2 | 5.607 | 7.058 | 15.478 / 11.841 / 8.301 |
| reference | rust-proxy | open | 3 | 8.141 | 9.990 | 15.478 / 11.841 / 8.301 |

All constrained runs observed `cpu.max=100000 100000` and `memory.max=536870912`.
The Rust front exposed `proxy_buffer_fallback_total=0`. Python metrics were unavailable.
Queue depth, batch sizes and checkpoint stalls: `not available until S4a/S9`.

Full per-attempt JSON reports remain local. This committed table contains no endpoint,
credential, host name or personal database data. No throughput or latency gate was applied.
