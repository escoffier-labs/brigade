"""Local portable tool and skill catalog inspection."""

from __future__ import annotations

from . import catalog_manage
from . import catalog_health
from . import calls
from . import checkpoint_store
from . import checkpoints
from . import config
from . import constants
from . import helpers
from . import mcp
from . import issues
from . import paths
from . import packs
from . import projections
from . import runtimes
from . import runs
from . import safety
from . import status

OK = constants.OK
WARN = constants.WARN
FAIL = constants.FAIL
CONFIG_REL_PATH = constants.CONFIG_REL_PATH
CALLS_REL_PATH = constants.CALLS_REL_PATH
RUNS_REL_PATH = constants.RUNS_REL_PATH
CHECKPOINTS_REL_PATH = constants.CHECKPOINTS_REL_PATH
RUNTIMES_REL_PATH = constants.RUNTIMES_REL_PATH
RUNTIME_STATE_REL_PATH = constants.RUNTIME_STATE_REL_PATH
POLICY_REL_PATH = constants.POLICY_REL_PATH
PARITY_CLOSEOUTS_REL_PATH = constants.PARITY_CLOSEOUTS_REL_PATH
HEALTH_STALE_HOURS = constants.HEALTH_STALE_HOURS
CALL_STALE_HOURS = constants.CALL_STALE_HOURS
CALL_RUNNING_STALE_HOURS = constants.CALL_RUNNING_STALE_HOURS
PROJECTION_MARKER = constants.PROJECTION_MARKER
FAMILIES = constants.FAMILIES
KNOWN_HARNESSES = constants.KNOWN_HARNESSES
PARITY_ISSUE_TYPES = constants.PARITY_ISSUE_TYPES
APPROVAL_MODES = constants.APPROVAL_MODES
SCHEMA_TYPES = constants.SCHEMA_TYPES
UNSAFE_FIELD_PATTERN = constants.UNSAFE_FIELD_PATTERN
HIGH_RISK_COMMAND_PATTERNS = constants.HIGH_RISK_COMMAND_PATTERNS
DEFAULT_TOOLS = constants.DEFAULT_TOOLS
DEFAULT_TOOL_SOURCE_TEXTS = constants.DEFAULT_TOOL_SOURCE_TEXTS
DEFAULT_RUNTIMES = constants.DEFAULT_RUNTIMES
DEFAULT_POLICY = constants.DEFAULT_POLICY

config_path = paths.config_path
calls_path = paths.calls_path
runs_path = paths.runs_path
checkpoints_path = paths.checkpoints_path
runtimes_config_path = paths.runtimes_config_path
runtime_state_path = paths.runtime_state_path
policy_path = paths.policy_path
parity_closeouts_path = paths.parity_closeouts_path

_parse_iso_datetime = helpers._parse_iso_datetime
_file_hash = helpers._file_hash
_text_hash = helpers._text_hash
_short = helpers._short
_as_path = helpers._as_path
_path_within_target = helpers._path_within_target
_format_inline_list = helpers._format_inline_list
_format_inline_table = helpers._format_inline_table
_format_toml_key = helpers._format_toml_key
_format_toml_object = helpers._format_toml_object
_format_tool_entries = helpers._format_tool_entries
_format_tools_toml = helpers._format_tools_toml
_default_tool_source_text = helpers._default_tool_source_text
_ensure_default_tool_sources = helpers._ensure_default_tool_sources
_format_runtimes_toml = helpers._format_runtimes_toml
_format_policy_toml = helpers._format_policy_toml
_read_json = helpers._read_json
_stable_hash = helpers._stable_hash
_now = helpers._now
_write_json = helpers._write_json

_load_config = config._load_config
_load_runtime_config = config._load_runtime_config
_load_policy_config = config._load_policy_config

_policy_decision = safety._policy_decision
_policy_health = safety._policy_health
_unsafe_fields = safety._unsafe_fields
_command_parts = safety._command_parts
_command_resolves = safety._command_resolves
_high_risk_command = safety._high_risk_command
_redact_value = safety._redact_value
_redact_payload = safety._redact_payload
_redact_text = safety._redact_text
_redact_known_values = safety._redact_known_values
_schema_path = safety._schema_path
_load_schema = safety._load_schema
_schema_shape_errors = safety._schema_shape_errors
_json_type_matches = safety._json_type_matches
_validate_json_value = safety._validate_json_value
_render_argument_template = safety._render_argument_template
_contract_defined = safety._contract_defined
_contract_summary = safety._contract_summary
_source_fingerprint = safety._source_fingerprint
_contract_fingerprint = safety._contract_fingerprint
_contract_issues = safety._contract_issues
policy_init = safety.policy_init
policy_show = safety.policy_show
policy_doctor = safety.policy_doctor

_projection_issue = issues._projection_issue
_tool_issue = issues._tool_issue
_is_parity_issue = issues._is_parity_issue
_parity_issue_fingerprint = issues._parity_issue_fingerprint
_latest_parity_closeout = issues._latest_parity_closeout
_apply_parity_closeout = issues._apply_parity_closeout

_find_runtime = runtimes._find_runtime
_runtime_file = runtimes._runtime_file
_runtime_pid_path = runtimes._runtime_pid_path
_runtime_metadata_path = runtimes._runtime_metadata_path
_runtime_health_path = runtimes._runtime_health_path
_runtime_log_paths = runtimes._runtime_log_paths
_read_pid = runtimes._read_pid
_process_alive = runtimes._process_alive
_read_runtime_metadata = runtimes._read_runtime_metadata
_write_runtime_metadata = runtimes._write_runtime_metadata
_port_in_use = runtimes._port_in_use
_runtime_cwd = runtimes._runtime_cwd
_runtime_status_item = runtimes._runtime_status_item
_runtime_payload = runtimes._runtime_payload
_tool_runtime_issues = runtimes._tool_runtime_issues
_start_runtime_payload = runtimes._start_runtime_payload
_stop_runtime_payload = runtimes._stop_runtime_payload
_restart_runtime_payload = runtimes._restart_runtime_payload
runtime_init = runtimes.runtime_init
runtime_list = runtimes.runtime_list
runtime_show = runtimes.runtime_show
runtime_status = runtimes.runtime_status
runtime_start = runtimes.runtime_start
runtime_stop = runtimes.runtime_stop
runtime_restart = runtimes.runtime_restart
runtime_doctor = runtimes.runtime_doctor

_managed_header = projections._managed_header
_managed_yaml_comment = projections._managed_yaml_comment
_parse_projection_metadata = projections._parse_projection_metadata
_read_projection = projections._read_projection
_relative_path = projections._relative_path
_render_projection_body = projections._render_projection_body
_yaml_string = projections._yaml_string
_codex_skill_frontmatter = projections._codex_skill_frontmatter
_is_codex_skill_projection = projections._is_codex_skill_projection
_projection_managed_body = projections._projection_managed_body
_render_managed_projection = projections._render_managed_projection
_projection_item = projections._projection_item
_projection_plan_payload = projections._projection_plan_payload
_inspect_mcp_config = projections._inspect_mcp_config
_inspect_tool = projections._inspect_tool
_find_tool = projections._find_tool
_contracts_payload = projections._contracts_payload

init = catalog_manage.init
_read_tool_entries = catalog_manage._read_tool_entries
_projection_scope = catalog_manage._projection_scope
_gitignore_selection = catalog_manage._gitignore_selection
_scoped_default_tool = catalog_manage._scoped_default_tool
defaults = catalog_manage.defaults
list_tools = catalog_manage.list_tools
show = catalog_manage.show
search = catalog_manage.search
describe = catalog_manage.describe
contracts = catalog_manage.contracts
plan = catalog_manage.plan
apply = catalog_manage.apply

_describe_payload = calls._describe_payload
_load_args = calls._load_args
_call_plan_payload = calls._call_plan_payload
_read_calls = calls._read_calls
_write_calls = calls._write_calls
_call_fingerprint = calls._call_fingerprint
_call_plan_from_record = calls._call_plan_from_record
_stored_call_fingerprint = calls._stored_call_fingerprint
_approval_fingerprint = calls._approval_fingerprint
_make_call_record = calls._make_call_record
_queue_call_payload = calls._queue_call_payload
_resolve_call = calls._resolve_call
_call_current_fingerprints = calls._call_current_fingerprints
_call_projection_summary = calls._call_projection_summary
_runtime_snapshot_for_call = calls._runtime_snapshot_for_call
_run_id_for_call = calls._run_id_for_call
_call_run_blockers = calls._call_run_blockers
_call_health = calls._call_health
call_plan = calls.call_plan
call_queue = calls.call_queue
call_list = calls.call_list
call_show = calls.call_show
_call_review = calls._call_review
call_approve = calls.call_approve
call_reject = calls.call_reject
call_hold = calls.call_hold

_checkpoint_paths = checkpoint_store._checkpoint_paths
_read_checkpoint = checkpoint_store._read_checkpoint
_write_checkpoint = checkpoint_store._write_checkpoint
_checkpoint_public_summary = checkpoint_store._checkpoint_public_summary
_resolve_checkpoint = checkpoint_store._resolve_checkpoint
_normalize_checkpoint = checkpoint_store._normalize_checkpoint
_collect_run_checkpoints = checkpoint_store._collect_run_checkpoints
_checkpoint_expired = checkpoint_store._checkpoint_expired

_mcp_jsonrpc_requests = mcp._mcp_jsonrpc_requests
_parse_mcp_responses = mcp._parse_mcp_responses
_mcp_response_by_id = mcp._mcp_response_by_id
_mcp_tool_list_contains = mcp._mcp_tool_list_contains
_run_mcp_call = mcp._run_mcp_call

_write_run_receipt = runs._write_run_receipt
_run_receipt_paths = runs._run_receipt_paths
_read_run_receipt = runs._read_run_receipt
_run_sort_key = runs._run_sort_key
_run_public_summary = runs._run_public_summary
_run_history_payload = runs._run_history_payload
_resolve_run_receipt = runs._resolve_run_receipt
_replay_plan_payload = runs._replay_plan_payload
_replay_call_payload = runs._replay_call_payload
_log_path_exists = runs._log_path_exists
_run_history_health = runs._run_history_health
_next_approved_call = runs._next_approved_call
_run_call_payload = runs._run_call_payload
call_run = runs.call_run
run_list = runs.run_list
run_show = runs.run_show
run_latest = runs.run_latest
run_replay = runs.run_replay

_checkpoint_resume_blockers = checkpoints._checkpoint_resume_blockers
_checkpoint_payload = checkpoints._checkpoint_payload
_checkpoint_health = checkpoints._checkpoint_health
_resume_checkpoint_payload = checkpoints._resume_checkpoint_payload
checkpoint_list = checkpoints.checkpoint_list
checkpoint_show = checkpoints.checkpoint_show
_checkpoint_review = checkpoints._checkpoint_review
checkpoint_approve = checkpoints.checkpoint_approve
checkpoint_reject = checkpoints.checkpoint_reject
checkpoint_resume = checkpoints.checkpoint_resume

_catalog_payload = catalog_health._catalog_payload

_packs_root = packs._packs_root
_packs_archive_root = packs._packs_archive_root
_tool_pack_payload = packs._tool_pack_payload
_tool_pack_evidence_fingerprint = packs._tool_pack_evidence_fingerprint
pack_build = packs.pack_build
_tool_packs = packs._tool_packs
_sync_plan_summary = packs._sync_plan_summary
_tool_pack_health = packs._tool_pack_health
pack_list = packs.pack_list
_find_tool_pack = packs._find_tool_pack
pack_show = packs.pack_show
pack_archive = packs.pack_archive
pack_import = packs.pack_import
sync_plan = packs.sync_plan
sync_apply = packs.sync_apply

health = status.health
_issue_records = status._issue_records
doctor = status.doctor
import_issues = status.import_issues
parity_status = status.parity_status
parity_closeout = status.parity_closeout
