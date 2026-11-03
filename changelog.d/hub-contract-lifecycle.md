- The verify-brigade `hub-contract` drive now checks message and question expiry, the
  sealed-content lifecycle and the message `client_id` window on a second hub whose clock
  it steps (`--clock-hub-cmd`), sweeps 60 more refusal paths, and compares the exact text
  of every refusal and the sealed tombstones with a golden captured from the Python hub
  (`--write-golden`).
