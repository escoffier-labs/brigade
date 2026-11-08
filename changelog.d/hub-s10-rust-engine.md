- `brigade fleet serve --engine rust` runs the Rust hub, `brigade-hub`, in front of the Python hub on the
  same database. It migrates the file first, refuses a binary whose SHA-256 does not match its recorded
  checksum, forwards stop signals, and exits non-zero after 3 failed `/health` checks so the service
  manager restarts it. `--route-mode` sets `brigade-hub` route modes. Dropping `--engine rust` rolls back.
- `brigade setup --component brigade-hub` installs the Rust hub on a Linux hub host. Releases publish it
  for `linux-amd64` and `linux-arm64` after pre-publication acceptance on each target, a plain
  `brigade setup` never downloads it, and a host that installed it keeps it on later setups and updates.
- With a native route family, `brigade-hub` restarts a Python upstream that crashed, after 1 s and then
  doubling to 30 s, while native routes keep serving. Proxied routes answer `502 upstream-down` and
  `/ready` answers 503 until it is back. Native long polls answer at once when the hub starts its 25 s
  shutdown drain.
- The dogfood Compose recipe runs `fleet serve --engine rust`, with the watchdog also exiting when the
  hub's network namespace is left with only loopback.
