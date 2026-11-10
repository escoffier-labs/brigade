- The Fleet Hub dogfood setup (`ops/fleet-hub/bin/hub-setup.py`) now also writes `secrets/operator-admin-token.txt`,
  a mode-0600 copy of the admin token that `brigade memory rescan` and `brigade memory reindex` accept as
  `--admin-token-file`. The container's 0444 copy is unchanged. Rerunning `init` adds the copy to an existing
  install, rewrites it after the admin token changes and resets a drifted mode.
