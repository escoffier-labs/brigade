# Runbooks

Operator procedures that are safe to follow as written:

- [Fleet hub on a Proxmox LXC](fleet-hub-proxmox.md), provisioning and operating the Tailscale-bound Phase 2 hub.
- [Fleet Hub container preview](fleet-hub-containers.md), non-root OCI, Compose, and Linux Podman Quadlet recipes.
- [Dogfood hub from the Compose recipe](fleet-hub-dogfood.md), the holder namespace layout, retrieval mode setup, backups and the restore tool, with its [host actions](../../ops/fleet-hub/HOST-ACTIONS.md).
- [Hyper-V native acceptance](hyper-v-native-acceptance.md), the maintainer-operated Windows acceptance gate.
- [Research live acceptance](research-live-acceptance.md), live checks for first-class multi-lane research.
- [Cold-start release gate](cold-start-gate.json), the receipt-backed release smoke test and its [shell wrapper](cold-start-gate.sh).
