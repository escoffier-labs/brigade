- Hub memory has verifier principals. An admin sets or clears a principal's `verifier` flag
  with a new memory action, and every credential carries a copy: native enrollment binds it
  before issuing, refuses an unflagged credential for a verifier principal with
  `memory_verifier_credential_unflagged`, and a request whose copy differs from the principal
  is refused with `403 memory_verifier_flag_mismatch`. Revocation and removal keep the flag.
  Memory schema moves to 11.
- Every proposal from a verifier principal is Tier 2 at submit, preflight and every decision.
  Preflight reports a `verifier_floor` hold and `auto_apply` refuses the proposal. A fleet
  policy that lists a verifier in `memory.reflect_principals` is refused with
  `memory_policy_verifier_listed`, and so is flagging a listed principal. A flag change that
  cannot publish the verifier list for the policy writer is rolled back and refused. The list
  is written durably and carries the memory file's generation, which must equal the one the hub
  records before each flag change, so a policy write refuses a stale list or one left by
  restoring either database, and hub memory does not start until it can republish it. Memory
  `status` reports both generations, and the admin `verifier_resync` action repairs a mismatch
  after a restore. Memory `status`
  reports the caller's `verifier` and `reflect_principal` booleans and a `verifier_floor`
  capability.
