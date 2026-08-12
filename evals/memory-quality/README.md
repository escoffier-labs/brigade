# Memory quality eval (#845)

This deterministic fixture drives a read-only eval over canonical Markdown cards
and captured evidence/retrieval contracts. Run
`python -m brigade.memory_retrieval_eval.quality_cli --root evals/memory-quality`.

The JSON report measures expired `fresh_until` values surfaced by retrieval,
canonical-versus-derived content hash drift, and results returned outside their
declared scope. Missing fields are recorded as contract wishes; this harness does
not extend the memory, retrieval, or evidence subsystems and never rewrites cards.
