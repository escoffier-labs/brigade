- Dispatch packets carry a bounded `memory_context` block: `brigade run` planner, worker and
  synthesis prompts, Grok Bot T3 adapter prompts, native T3 child tasks and
  `brigade fleet work handoff show`. Each block holds at most 5 card references (scope, card id,
  title, a summary of at most 300 characters, a short content hash, trust, score and source)
  within 4 KiB, never a card body, and says memory text is data, not instructions.
- Selection uses the `memory search` ranking, keeps only reviewed, policy-promoted and imported hub
  cards, re-reads each hub card's head and drops stale ones, and scans every field on its own and
  then the rendered block. A hub that is stopped, slow or refusing adds no block and never delays
  dispatch past a 2 s budget, and at most one recall runs at a time. Every packet is built for a
  connector audience, so private-class cards never appear in one.
- Grok Bot feed jobs carry up to 5 card references (`memory_refs`: scope, card id and short hash)
  in the private job envelope, returned with the claim's execution context. Hub job payloads are
  unchanged and hold no task text.
