# Hub schema variants

Python owns all migrations. Rust accepts a complete recorded table variant,
including column order, normalized DDL, nullability, defaults, checks, keys,
foreign keys, and indexes. It never combines per-column alternatives.
`required.json` contains the accepted structures. `variant-inventory.json`
records every differing field with its fresh and upgraded values and sources.
Only whitespace in DDL is normalized. Unknown objects remain refused.

The fixtures cover schema versions 1 through 48 present in repository history,
all published releases containing `fleet_hub.init_db` (v0.27.0 through v0.30.0),
the storage-parity v22/v34 captures, and supported legacy regression builders.
Versions absent from history were never separate capturable revisions.
The fixture header records the exact source commit or release.

| Table | Migration and possible difference from fresh |
| --- | --- |
| events | `_EVENTS_ADDITIVE_COLUMNS`, context repair: appended order and DDL. The v4 test builder also permits nullable original event fields. |
| claims | `_migrate_claims_table`: pre-holder tables are recreated, lease columns append in migration order. DDL and order differ. |
| model_policy | v8 rebuild changes the key to seat, then roster columns append. DDL and order differ. |
| model_leases | roster linkage columns append. No residual difference in captured layouts. |
| grokbot_jobs | `fleet_hub_grokbot.ensure_schema`: additive job columns change order and DDL. |
| grokbot_operations | additive operation columns. No residual difference in captured layouts. |
| run_preference | v19 research/security/scout append, v48 chef rename. Order and quoted rename DDL differ. |
| interactive_sessions | cloud context and provider link append. DDL and order differ for independent repairs. |
| fleet_messages | legacy client_id/payload_hash/received_at additions are nullable, legacy reply_to lacks a foreign key. Metadata and sealed fields append in migration order. DDL and order differ. |
| fleet_message_principals | enrollment, roles, authorization and grant epoch additions. DDL and order differ. |
| nodes | v14/v4 test builders permit nullable original fields. DDL and nullability differ. |
| claim_retired_holders | v33 rename/rebuild recreates the current CHECK, key, retirement time and age indexes. No residual difference. |
| work_import_keys | unchanged/owner_node/refused additions. No residual difference in captured layouts. |
| work_events | seq addition. No residual difference in captured layouts. |
| work_links | adapter/owner/source_version additions change order and DDL. |
| fleet_events | producer addition retains NOT NULL DEFAULT empty string, unlike fresh with no default. Order and DDL differ. |
| fleet_event_subscriptions | generation addition retains its declared default. No residual difference in captured layouts. |
| fleet_read_snapshots | last_access repair retains NOT NULL DEFAULT 0, unlike fresh with no default. DDL differs. |
| fleet_questions | late-answer and desktop reply columns append. Order and DDL differ. Reply trigger is replaced by Python with current DDL. |
| agent_actions | destination_thread addition. No residual difference in captured layouts. |

All startup indexes and triggers are included in each real-run snapshot. Their
presence and DDL converge across the captured upgrades. This includes event,
claim expiry, lease, routing, message, question, paging, audit, policy and
Worklore indexes/triggers. CHECK constraints also converge, including the
retired-holder rebuild. No independent optional index exemption is added.

Schema v49 adds `fleet_findings` and `fleet_finding_grants` as required startup
tables, including their complete indexes and constraints. Rust proxies findings
until S12h and must accept these Python-owned objects after rollback. They have
no fresh/upgrade variants.

Lazy families remain optional only by absence: fleet_mcp_reports,
fleet_model_inventory_{observations,projections,aliases},
fleet_skill_{reports,review_drafts,review_events}, fleet_project_boards,
fleet_policy_migration and the twelve fleet_policy_guard triggers.
Their indexes and complete structures remain mandatory when present.
CI-node cache and fleet-export DDL belong to separate databases.

Regenerate through `brigade work verify run`: run `capture_upgrades.py` from
the repository root for history captures, `lockstep.py --generate` for the
contract, and `inventory.py` for exact differences. Lockstep tests consume
recorded fixtures and compare against the reviewed contract, without updating it.
