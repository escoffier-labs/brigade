# Runbooks

Operator procedures and their validation status:

- **Proposed per-host; acceptance record required:** [Fleet Hub on a generic Linux host](fleet-hub-linux.md), with pinned foreground installs, least-privilege systemd operation, recovery, and removal while preserving data.
- **Proposed deployment; application behavior tested:** [Fleet hub on a Proxmox LXC](fleet-hub-proxmox.md), provisioning and operating the Tailscale-bound Phase 2 hub.
- [Hyper-V native acceptance](hyper-v-native-acceptance.md), the maintainer-operated Windows acceptance gate.
- [Research live acceptance](research-live-acceptance.md), live checks for first-class multi-lane research.
- [Cold-start release gate](cold-start-gate.json), the receipt-backed release smoke test and its [shell wrapper](cold-start-gate.sh).

“Tested” describes evidence from the named acceptance procedure. “Proposed”
means the recipe is documented but must not be represented as a completed live
deployment.
