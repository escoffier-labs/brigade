# Live roster model inventory

Issue #299 needs an advisory check at the point where operators already inspect a roster. Schema validation proves that a CLI accepts a model flag, but it cannot prove that the named model still exists. `brigade roster doctor` should query only harnesses with cheap read-only inventory commands and report one of four states: `exact`, `fuzzy-resolved`, `missing`, or `unavailable`.

The first supported inventories are direct Cursor, direct Grok, and Ollama. Cursor uses `cursor-agent models`; Grok uses `grok models`. Exact IDs pass. A non-exact ID is `fuzzy-resolved` only when it shares the same normalized model family and version with a live ID after removing Cursor's `cursor-` namespace and recognized effort or `-fast` suffixes. This narrow rule catches aliases such as `grok-4.5-xhigh` without treating a different model version as compatible. Cursor ACP seats are excluded because the ACP server advertises different model IDs than the direct CLI.

Ollama continues to use its existing local list guard. A listed local model is exact. A listed `:cloud` model also gets `ollama show <model>`, which is read-only and exposes retired remote models even when the local list still contains their manifest. A known retired or missing response is `missing`; network, authentication, timeout, command, and output-shape failures are `unavailable`.

All non-exact states are warnings. Inventory failures never make roster loading fail and never turn transient provider state into a blocking doctor result. Brigade does not invoke a model, guess across versions, pull an Ollama model, or add a general provider router. Repeated seats share inventory results within one doctor run.
