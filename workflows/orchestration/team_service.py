from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from teams_runtime.workflows.roles import (
    EXECUTION_AGENT_ROLES,
    get_agent_capability,
    intent_to_role_map,
    load_agent_utilization_policy,
)
from teams_runtime.workflows.state.backlog_store import (
    backlog_status_counts,
    backlog_status_report_context,
    apply_backlog_state_from_todo,
    build_backlog_fingerprint,
    build_blocked_backlog_review_fingerprint,
    build_sourcer_candidate_trace_fingerprint,
    build_sourcer_review_fingerprint,
    classify_backlog_kind,
    clear_backlog_blockers,
    desired_backlog_status_for_todo,
    drop_non_actionable_backlog_items,
    fallback_backlog_candidates_from_findings,
    iter_backlog_items,
    is_actionable_backlog_status,
    is_active_backlog_status,
    is_non_actionable_backlog_item,
    is_reusable_backlog_status,
    load_backlog_item,
    normalize_blocked_backlog_review_candidates,
    normalize_backlog_acceptance_criteria,
    normalize_sourcer_review_candidates,
    refresh_backlog_markdown,
    render_blocked_backlog_review_markdown,
    repair_non_actionable_carry_over_backlog_items,
    render_sourcer_review_markdown,
    save_backlog_item,
)
from teams_runtime.shared.config import load_discord_agents_config, load_team_runtime_config
from teams_runtime.workflows.orchestration.relay import (
    archive_internal_relay_file,
    build_internal_relay_message_stub as render_internal_relay_message_stub,
    build_internal_relay_record_id,
    deserialize_internal_relay_envelope,
    enqueue_internal_relay,
    internal_relay_archive_dir,
    internal_relay_inbox_dir,
    internal_relay_root,
    is_internal_relay_summary_content,
)
from teams_runtime.workflows.orchestration.delegation import (
    apply_role_result as apply_role_result_helper,
    build_delegate_body as build_delegate_body_helper,
    build_delegate_envelope as build_delegate_envelope_helper,
    build_delegation_context as build_delegation_context_helper,
    build_handoff_routing_path as build_handoff_routing_path_helper,
    build_internal_sprint_delegation_payload as build_internal_sprint_delegation_payload_helper,
    build_role_result_semantic_context as build_role_result_semantic_context_helper,
    delegate_request as delegate_request_helper,
    delegate_task_text as delegate_task_text_helper,
    derive_routing_decision_after_report as derive_routing_decision_after_report_helper,
    extract_semantic_leaf_lines as extract_semantic_leaf_lines_helper,
    format_role_request_snapshot_markdown as format_role_request_snapshot_markdown_helper,
    handle_delegated_request as handle_delegated_request_helper,
    handle_role_report as handle_role_report_helper,
    planner_backlog_titles as planner_backlog_titles_helper,
    planner_doc_targets as planner_doc_targets_helper,
    process_delegated_request as process_delegated_request_helper,
    proposal_semantic_details as proposal_semantic_details_helper,
    proposal_nested_string_list as proposal_nested_string_list_helper,
    run_local_orchestrator_request as run_local_orchestrator_request_helper,
    summarize_relay_body as summarize_relay_body_helper,
    synthesize_latest_role_context as synthesize_latest_role_context_helper,
    write_role_request_snapshot as write_role_request_snapshot_helper,
)
from teams_runtime.workflows.orchestration.artifacts import (
    backlog_artifact_candidate_paths as backlog_artifact_candidate_paths_helper,
    collect_artifact_candidates as collect_artifact_candidates_helper,
    collect_backlog_candidates_from_payload as collect_backlog_candidates_from_payload_helper,
    load_backlog_candidates_from_artifact as load_backlog_candidates_from_artifact_helper,
    message_attachment_artifacts as message_attachment_artifacts_helper,
    normalize_backlog_file_candidates as normalize_backlog_file_candidates_helper,
    planner_backlog_write_receipts as planner_backlog_write_receipts_helper,
    resolve_artifact_path as resolve_artifact_path_helper,
    workspace_artifact_hint as workspace_artifact_hint_helper,
)
from teams_runtime.workflows.orchestration.scheduler import (
    backlog_sourcing_interval_seconds as backlog_sourcing_interval_seconds_helper,
    backlog_sourcing_loop as backlog_sourcing_loop_helper,
    build_backlog_sourcing_findings as build_backlog_sourcing_findings_helper,
    build_sourcer_existing_backlog_context as build_sourcer_existing_backlog_context_helper,
    collect_backlog_linked_request_ids as collect_backlog_linked_request_ids_helper,
    discover_backlog_candidates as discover_backlog_candidates_helper,
    maybe_queue_blocked_backlog_review_for_autonomous_start as maybe_queue_blocked_backlog_review_for_autonomous_start_helper,
    perform_backlog_sourcing as perform_backlog_sourcing_helper,
    poll_backlog_sourcing_once as poll_backlog_sourcing_once_helper,
    poll_scheduler_once as poll_scheduler_once_helper,
    scheduler_loop as scheduler_loop_helper,
    select_backlog_items_for_sprint as select_backlog_items_for_sprint_helper,
)
from teams_runtime.workflows.orchestration.engine import (
    DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT,
    WORKFLOW_CONTRACT_VERSION,
    WORKFLOW_PHASE_CLOSEOUT,
    WORKFLOW_PHASE_IMPLEMENTATION,
    WORKFLOW_PHASE_PLANNING,
    WORKFLOW_PHASE_VALIDATION,
    WORKFLOW_PHASES,
    WORKFLOW_POLICY_SOURCE,
    WORKFLOW_REOPEN_CATEGORIES,
    WORKFLOW_SELECTION_SOURCE,
    WORKFLOW_STEP_ARCHITECT_GUIDANCE,
    WORKFLOW_STEP_CLOSEOUT,
    WORKFLOW_STEP_DEVELOPER_BUILD,
    WORKFLOW_STEP_PLANNER_ADVISORY,
    WORKFLOW_STEP_RESEARCH_INITIAL,
    WORKFLOW_STEPS,
    default_workflow_state,
    behavior_trait_matches as behavior_trait_matches_helper,
    build_governed_routing_selection as build_governed_routing_selection_helper,
    classify_request_state as classify_request_state_helper,
    coerce_nonterminal_workflow_role_result as coerce_nonterminal_workflow_role_result_helper,
    derive_workflow_routing_decision as derive_pure_workflow_routing_decision,
    derive_routing_phase as derive_routing_phase_helper,
    execution_evidence_score as execution_evidence_score_helper,
    enforce_workflow_role_report_contract as apply_workflow_role_report_contract,
    infer_legacy_internal_workflow_state,
    initial_workflow_state,
    is_planner_owned_surface_artifact_hint,
    is_planning_surface_artifact_hint,
    match_reference_terms as match_reference_terms_helper,
    normalize_artifact_hint,
    normalize_workflow_state,
    preferred_skill_matches as preferred_skill_matches_helper,
    qa_result_is_runtime_sync_anomaly,
    qa_result_requires_planner_reopen,
    request_indicates_execution as request_indicates_execution_helper,
    required_workflow_planner_doc_hints,
    research_first_workflow_state,
    role_hint_score as role_hint_score_helper,
    routing_phase_for_role,
    routing_signal_matches as routing_signal_matches_helper,
    score_candidate_role as score_candidate_role_helper,
    set_request_workflow_state,
    should_not_handle_matches as should_not_handle_matches_helper,
    strongest_domain_matches as strongest_domain_matches_helper,
    workflow_planner_doc_contract_violation,
    workflow_complete_decision as build_workflow_complete_decision,
    workflow_reason,
    workflow_reopen_decision as build_workflow_reopen_decision,
    workflow_review_cycle_limit_block_decision as build_workflow_review_cycle_limit_block_decision,
    workflow_review_cycle_limit_reached,
    workflow_route_to_architect_guidance_decision,
    workflow_route_to_architect_review_decision,
    workflow_route_to_developer_build_decision,
    workflow_route_to_planner_draft_decision,
    workflow_route_to_planner_finalize_decision,
    workflow_route_to_planning_advisory_decision,
    workflow_route_to_qa_decision,
    workflow_route_to_research_initial_decision,
    workflow_terminal_block_decision as build_workflow_terminal_block_decision,
    workflow_should_close_in_planning as workflow_should_close_in_planning_helper,
    workflow_transition,
    workflow_transition_requests_explicit_continuation,
    workflow_transition_requests_validation_handoff,
)
from teams_runtime.workflows.repository_ops import (
    ActionExecutor,
    build_sprint_commit_message,
    build_version_control_helper_command,
    capture_git_baseline,
    collect_sprint_owned_paths,
    inspect_sprint_closeout,
)
from teams_runtime.workflows.orchestration.ingress import is_manual_sprint_start_text
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import (
    build_request_fingerprint,
    new_request_id,
    normalize_runtime_datetime,
    read_json,
    utc_now_iso,
    write_json,
)
from teams_runtime.workflows.orchestration.ingress import (
    build_planning_envelope_with_inferred_verification,
    build_duplicate_request_fingerprint,
    build_requester_route,
    extract_original_requester,
    handle_message as handle_message_helper,
    handle_non_orchestrator_message as handle_non_orchestrator_message_helper,
    handle_orchestrator_message as handle_orchestrator_message_helper,
    handle_user_request as handle_user_request_helper,
    is_attachment_only_save_failure as is_attachment_only_save_failure_helper,
    is_message_allowed as is_message_allowed_helper,
    is_trusted_relay_message as is_trusted_relay_message_helper,
    listen_forever as listen_forever_helper,
    on_ready as on_ready_helper,
    planning_envelope_has_explicit_source_context,
    request_identity_from_envelope,
    request_identity_matches,
    should_request_sprint_milestone_for_relay_intake,
)
from teams_runtime.workflows.orchestration.relay import (
    build_internal_relay_summary_message as render_internal_relay_summary_message,
)
from teams_runtime.workflows.state.request_store import (
    append_request_event,
    build_blocked_backlog_review_request_record as build_blocked_backlog_review_request_record_helper,
    build_sourcer_review_request_record as build_sourcer_review_request_record_helper,
    find_open_blocked_backlog_review_request as find_open_blocked_backlog_review_request_helper,
    find_open_sourcer_review_request as find_open_sourcer_review_request_helper,
    is_blocked_backlog_review_request as is_blocked_backlog_review_request_helper,
    is_internal_sprint_request as is_internal_sprint_request_helper,
    is_planner_backlog_review_request as is_planner_backlog_review_request_helper,
    is_sourcer_review_request as is_sourcer_review_request_helper,
    is_terminal_internal_request_status as is_terminal_internal_request_status_helper,
    is_terminal_request,
    iter_request_records,
    iter_sprint_task_request_records as iter_sprint_task_request_records_helper,
    load_request,
    save_request,
)
from teams_runtime.shared.formatting import (
    ReportSection,
    build_progress_report,
    read_process_summary,
)
from teams_runtime.workflows.sprints.reporting import (
    archive_sprint_history as archive_sprint_history_helper,
    build_sprint_closeout_result as build_sprint_closeout_result_helper,
    build_sprint_closeout_state_update as build_sprint_closeout_state_update_helper,
    build_sprint_report_archive_state as build_sprint_report_archive_state_helper,
    build_sprint_report_path_text as build_sprint_report_path_text_helper,
    build_planner_closeout_artifacts as build_planner_closeout_artifacts_helper,
    build_planner_closeout_context_payload as build_planner_closeout_context_payload_helper,
    build_planner_closeout_envelope_payload as build_planner_closeout_envelope_payload_helper,
    build_planner_closeout_request_context as build_planner_closeout_request_context_helper,
    build_planner_initial_phase_activity_report as build_planner_initial_phase_activity_report_helper,
    build_planner_initial_phase_activity_sections as build_planner_initial_phase_activity_sections_helper,
    build_derived_closeout_result_from_sprint_state as build_derived_closeout_result_from_sprint_state_helper,
    build_sprint_delivered_change as build_sprint_delivered_change_helper,
    build_machine_sprint_report_lines as render_machine_sprint_report_lines_helper,
    build_sprint_report_snapshot as build_sprint_report_snapshot_helper,
    build_sprint_change_summary_lines as render_sprint_change_summary_lines_helper,
    build_sprint_progress_log_summary as render_sprint_progress_log_summary_helper,
    build_sprint_achievement_lines as render_sprint_achievement_lines,
    build_sprint_agent_contribution_lines as render_sprint_agent_contribution_lines,
    build_sprint_artifact_lines as render_sprint_artifact_lines,
    build_sprint_commit_lines as build_sprint_commit_lines_helper,
    build_sprint_followup_lines as build_sprint_followup_lines_helper,
    build_generic_sprint_report_sections as build_generic_sprint_report_sections_helper,
    build_sprint_kickoff_preview_lines as build_sprint_kickoff_preview_lines_helper,
    build_sprint_kickoff_report_sections as build_sprint_kickoff_report_sections_helper,
    build_sprint_spec_todo_report_body as build_sprint_spec_todo_report_body_helper,
    build_sprint_spec_todo_report_sections as build_sprint_spec_todo_report_sections_helper,
    build_sprint_todo_list_report_sections as build_sprint_todo_list_report_sections_helper,
    build_terminal_sprint_report_sections as build_terminal_sprint_report_sections_helper,
    collect_sprint_role_report_events as collect_sprint_role_report_events_helper,
    decorate_sprint_report_title as decorate_sprint_report_title_helper,
    format_backlog_report_line as format_backlog_report_line_helper,
    format_priority_value as format_priority_value_helper,
    format_sprint_duration as format_sprint_duration_helper,
    format_sprint_report_text as format_sprint_report_text_helper,
    format_todo_report_line as format_todo_report_line_helper,
    limit_sprint_report_lines as limit_sprint_report_lines_helper,
    planner_closeout_request_id as planner_closeout_request_id_helper,
    planner_initial_phase_next_action as planner_initial_phase_next_action_helper,
    planner_initial_phase_priority_lines as planner_initial_phase_priority_lines_helper,
    planner_initial_phase_report_key as planner_initial_phase_report_key_helper,
    planner_initial_phase_report_keys as planner_initial_phase_report_keys_helper,
    planner_initial_phase_work_lines as planner_initial_phase_work_lines_helper,
    preview_sprint_artifact_path as preview_sprint_artifact_path_helper,
    report_section as report_section_helper,
    relative_workspace_path as relative_workspace_path_helper,
    build_sprint_headline as render_sprint_headline,
    build_sprint_issue_lines as render_sprint_issue_lines,
    build_sprint_overview_lines as render_sprint_overview_lines,
    build_sprint_planned_todo_lines as build_sprint_planned_todo_lines_helper,
    build_sprint_terminal_state_update as build_sprint_terminal_state_update_helper,
    should_refresh_sprint_history_archive as should_refresh_sprint_history_archive_helper,
    build_sprint_timeline_lines as render_sprint_timeline_lines,
    render_backlog_status_report as render_backlog_status_report_helper,
    render_live_sprint_report_markdown as render_live_sprint_report_markdown_helper,
    render_sprint_iteration_log_markdown as render_sprint_iteration_log_markdown_helper,
    render_sprint_kickoff_markdown as render_sprint_kickoff_markdown_helper,
    render_sprint_milestone_markdown as render_sprint_milestone_markdown_helper,
    render_sprint_plan_markdown as render_sprint_plan_markdown_helper,
    render_sprint_report_body as render_sprint_report_body_helper,
    render_sprint_spec_markdown as render_sprint_spec_markdown_helper,
    refresh_sprint_history_archive as refresh_sprint_history_archive_helper,
    render_sprint_status_report as render_sprint_status_report_helper,
    render_sprint_completion_user_report as render_sprint_completion_user_report_helper,
    render_sprint_todo_backlog_markdown as render_sprint_todo_backlog_markdown_helper,
    sprint_role_display_name as sprint_role_display_name_helper,
    sprint_status_label as sprint_status_label_helper,
    parse_sprint_report_fields as parse_sprint_report_fields_helper,
    parse_sprint_report_int_field as parse_sprint_report_int_field_helper,
    parse_sprint_report_list_field as parse_sprint_report_list_field_helper,
    refresh_sprint_report_body as refresh_sprint_report_body_helper,
    split_report_body_lines as split_report_body_lines_helper,
    sprint_artifact_paths as sprint_artifact_paths_helper,
    send_sprint_completion_user_report_for_service as send_sprint_completion_user_report_for_service_helper,
    send_sprint_kickoff_for_service as send_sprint_kickoff_for_service_helper,
    send_sprint_report_for_service as send_sprint_report_for_service_helper,
    send_sprint_todo_list_for_service as send_sprint_todo_list_for_service_helper,
    send_terminal_sprint_reports_for_service as send_terminal_sprint_reports_for_service_helper,
)
from teams_runtime.workflows.orchestration.notifications import (
    DiscordNotificationService,
    _render_discord_message_chunks as _notifications_render_discord_message_chunks,
    _split_discord_chunks as _notifications_split_discord_chunks,
)
from teams_runtime.workflows.sprints.lifecycle import (
    attachment_storage_relative_path,
    build_active_sprint_id,
    build_sprint_artifact_folder_name,
    build_sprint_planning_request_record as build_sprint_planning_request_record_helper,
    extract_sprint_folder_name,
    slugify_sprint_value,
    sprint_attachment_filename,
    utc_now,
)
from teams_runtime.workflows.sprints.reporting import (
    collect_sprint_todo_artifact_entries,
    render_sprint_artifact_index_markdown,
)
from teams_runtime.workflows.state.sprint_store import (
    append_sprint_event,
    iter_sprint_event_entries,
)
from teams_runtime.workflows.orchestration.relay import (
    consume_internal_relay_loop as consume_internal_relay_loop_helper,
    consume_internal_relay_once as consume_internal_relay_once_helper,
    process_internal_relay_envelope as process_internal_relay_envelope_helper,
    record_relay_delivery as record_relay_delivery_helper,
    send_relay_transport as send_relay_transport_helper,
)
from teams_runtime.workflows.orchestration.notifications import (
    announce_startup_notification as announce_startup_notification_helper,
    append_markdown_entry as append_markdown_entry_helper,
    append_role_history as append_role_history_helper,
    append_role_journal as append_role_journal_helper,
    append_shared_workspace_entry as append_shared_workspace_entry_helper,
    build_requester_status_message as build_requester_status_message_helper,
    build_sourcer_report_state_update as build_sourcer_report_state_update_helper,
    ensure_markdown_file as ensure_markdown_file_helper,
    get_sourcer_report_client_for_service as get_sourcer_report_client_for_service_helper,
    normalize_insights as normalize_insights_helper,
    normalize_markdown_body as normalize_markdown_body_helper,
    record_shared_role_result as record_shared_role_result_helper,
    refresh_role_todos as refresh_role_todos_helper,
    report_sourcer_activity_sync as report_sourcer_activity_sync_helper,
    reply_to_requester as reply_to_requester_helper,
    send_channel_reply as send_channel_reply_helper,
    send_discord_content as send_discord_content_helper,
    send_immediate_receipt as send_immediate_receipt_helper,
)
from teams_runtime.workflows.sprints.lifecycle import (
    INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
    INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
    INITIAL_PHASE_STEP_ARTIFACT_SYNC,
    INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
    INITIAL_PHASE_STEP_TODO_FINALIZATION,
    INITIAL_PHASE_STEPS,
    SPRINT_ACTIVE_BACKLOG_STATUSES,
    apply_sprint_planning_result as apply_sprint_planning_result_helper,
    build_idle_current_sprint_markdown as build_idle_current_sprint_markdown_helper,
    build_manual_sprint_names as build_manual_sprint_names_helper,
    build_manual_sprint_state as build_manual_sprint_state_helper,
    build_recovered_sprint_todo_from_request_for_service as build_recovered_sprint_todo_from_request_for_service_helper,
    collect_sprint_relevant_backlog_items as collect_sprint_relevant_backlog_items_helper,
    continue_manual_daily_sprint as continue_manual_daily_sprint_helper,
    continue_sprint as continue_sprint_helper,
    create_internal_request_record as create_internal_request_record_helper,
    enforce_task_commit_for_completed_todo as enforce_task_commit_for_completed_todo_helper,
    execute_sprint_todo as execute_sprint_todo_helper,
    fail_sprint_due_to_exception as fail_sprint_due_to_exception_helper,
    finalize_sprint as finalize_sprint_helper,
    finish_scheduler_after_sprint as finish_scheduler_after_sprint_helper,
    initial_phase_step as initial_phase_step_helper,
    initial_phase_step_instruction as initial_phase_step_instruction_helper,
    initial_phase_step_position as initial_phase_step_position_helper,
    initial_phase_step_title as initial_phase_step_title_helper,
    is_executable_todo_status as is_executable_todo_status_helper,
    is_initial_phase_planner_request as is_initial_phase_planner_request_helper,
    is_manual_sprint_cutoff_reached as is_manual_sprint_cutoff_reached_helper,
    is_resumable_blocked_sprint as is_resumable_blocked_sprint_helper,
    is_sprint_planning_request as is_sprint_planning_request_helper,
    is_wrap_up_requested as is_wrap_up_requested_helper,
    load_sprint_state_with_sync as load_sprint_state_with_sync_helper,
    mark_restart_checkpoint_backlog_selected as mark_restart_checkpoint_backlog_selected_helper,
    maybe_update_sprint_name_from_result as maybe_update_sprint_name_from_result_helper,
    merge_recovered_sprint_todo as merge_recovered_sprint_todo_helper,
    next_initial_phase_step as next_initial_phase_step_helper,
    normalize_trace_list as normalize_trace_list_helper,
    prepare_requested_restart_checkpoint as prepare_requested_restart_checkpoint_helper,
    record_sprint_planning_iteration as record_sprint_planning_iteration_helper,
    recover_sprint_todos_from_requests as recover_sprint_todos_from_requests_helper,
    resume_active_sprint as resume_active_sprint_helper,
    resume_uncommitted_sprint_todo as resume_uncommitted_sprint_todo_helper,
    run_internal_request_chain as run_internal_request_chain_helper,
    run_autonomous_sprint as run_autonomous_sprint_helper,
    save_sprint_state_with_sync as save_sprint_state_with_sync_helper,
    select_restart_checkpoint_todo as select_restart_checkpoint_todo_helper,
    should_start_sprint_research_prepass as should_start_sprint_research_prepass_helper,
    sort_sprint_todos as sort_sprint_todos_helper,
    sprint_research_prepass_artifacts as sprint_research_prepass_artifacts_helper,
    sprint_uses_manual_flow as sprint_uses_manual_flow_helper,
    sync_internal_sprint_artifacts_from_role_report as sync_internal_sprint_artifacts_from_role_report_helper,
    sync_manual_sprint_queue as sync_manual_sprint_queue_helper,
    sync_planner_backlog_review_from_role_report as sync_planner_backlog_review_from_role_report_helper,
    sync_sprint_planning_state as sync_sprint_planning_state_helper,
    synchronize_sprint_todo_backlog_state as synchronize_sprint_todo_backlog_state_helper,
    todo_status_from_request_record as todo_status_from_request_record_helper,
    todo_status_rank as todo_status_rank_helper,
    uses_manual_daily_sprint as uses_manual_daily_sprint_helper,
    validate_initial_phase_step_result as validate_initial_phase_step_result_helper,
)
from teams_runtime.workflows.orchestration.ingress import (
    extract_ready_planning_artifact as extract_ready_planning_artifact_helper,
    extract_verification_related_request_ids as extract_verification_related_request_ids_helper,
    find_blocked_requests_for_verified_artifact as find_blocked_requests_for_verified_artifact_helper,
    find_recent_ready_planning_verification as find_recent_ready_planning_verification_helper,
    is_blocked_planning_request_waiting_for_document as is_blocked_planning_request_waiting_for_document_helper,
    normalize_reference_text as normalize_reference_text_helper,
    request_mentions_artifact as request_mentions_artifact_helper,
    verification_result_payload,
    apply_resume_request_update as apply_resume_request_update_helper,
    analyze_blocked_duplicate_followup as analyze_blocked_duplicate_followup_helper,
    augment_blocked_duplicate_request as augment_blocked_duplicate_request_helper,
    build_created_request_record as build_created_request_record_helper,
    build_resume_routing_context_kwargs as build_resume_routing_context_kwargs_helper,
    cancel_request as cancel_request_helper,
    clean_kickoff_text as clean_kickoff_text_helper,
    combine_envelope_scope_and_body as combine_envelope_scope_and_body_helper,
    execute_registered_action as execute_registered_action_helper,
    extract_manual_sprint_kickoff_payload as extract_manual_sprint_kickoff_payload_helper,
    extract_manual_sprint_milestone_title as extract_manual_sprint_milestone_title_helper,
    forward_user_request as forward_user_request_helper,
    is_manual_sprint_finalize_request as is_manual_sprint_finalize_request_helper,
    is_manual_sprint_start_request as is_manual_sprint_start_request_helper,
    normalize_kickoff_requirements as normalize_kickoff_requirements_helper,
    parse_kickoff_text_sections as parse_kickoff_text_sections_helper,
    reply_status_request as reply_status_request_helper,
    retry_blocked_duplicate_request as retry_blocked_duplicate_request_helper,
)
from teams_runtime.discord.client import (
    DiscordClient,
    DiscordMessage,
    MESSAGE_END_MARKER,
    MESSAGE_START_MARKER,
)
from teams_runtime.shared.models import MessageEnvelope, RequestRecord, RoleResult, TEAM_ROLES, WorkflowState
from teams_runtime.runtime.base_runtime import RoleAgentRuntime, normalize_role_payload
from teams_runtime.runtime.internal.backlog_sourcing import BacklogSourcingRuntime
from teams_runtime.runtime.internal.intent_parser import IntentParserRuntime, normalize_intent_payload
from teams_runtime.runtime.research_runtime import ResearchAgentRuntime
from teams_runtime.runtime.identities import local_runtime_identity, service_runtime_identity


LOGGER = logging.getLogger("teams_runtime.core.orchestration")
SCHEDULER_POLL_SECONDS = 15.0
LISTENER_RETRY_SECONDS = 5.0
INTERNAL_REQUEST_POLL_SECONDS = 0.2
BACKLOG_SOURCING_POLL_SECONDS = 15.0
ROLE_REQUEST_RESUME_POLL_SECONDS = 5.0
MALFORMED_RELAY_LOG_WINDOW_SECONDS = 60.0
RELAY_TRANSPORT_INTERNAL = "internal"
RELAY_TRANSPORT_DISCORD = "discord"
VALID_RELAY_TRANSPORTS = {
    RELAY_TRANSPORT_INTERNAL,
    RELAY_TRANSPORT_DISCORD,
}
INTERNAL_RELAY_SUMMARY_MARKER = "내부 relay 요약:"

SPRINT_DISCORD_SUMMARY_FLOW_LIMIT = 4
SPRINT_DISCORD_SUMMARY_ROLE_LIMIT = 4
SPRINT_DISCORD_SUMMARY_ISSUE_LIMIT = 3
SPRINT_DISCORD_SUMMARY_ACHIEVEMENT_LIMIT = 3
SPRINT_DISCORD_SUMMARY_ARTIFACT_LIMIT = 3

PLANNING_CONTEXT_RECENCY_SECONDS = 3600.0
SPRINT_INITIAL_PHASE_MAX_ITERATIONS = 3
RECENT_SPRINT_ACTIVITY_LIMIT = 25
SPRINT_SPEC_TODO_REPORT_DOC_KEYS = ("milestone", "plan", "spec", "todo_backlog", "iteration_log")


def _split_discord_chunks(content: str, limit: int = 2000) -> list[str]:
    return _notifications_split_discord_chunks(content, limit=limit)


def _render_discord_message_chunks(
    content: str,
    *,
    limit: int = 2000,
    prefix: str = "",
    include_sequence_markers: bool = True,
) -> list[str]:
    return _notifications_render_discord_message_chunks(
        content,
        limit=limit,
        prefix=prefix,
        include_sequence_markers=include_sequence_markers,
    )


def _parse_report_body_json(body: str) -> dict[str, Any]:
    raw = str(body or "").strip()
    if not raw:
        return {}
    candidates: list[str] = [raw]
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            candidates.append("\n".join(lines[1:-1]).strip())
        fenced_segments: list[str] = []
        segment_lines: list[str] = []
        in_fenced_json = False
        for line in lines:
            stripped = line.strip()
            if not in_fenced_json:
                if stripped.startswith("```"):
                    in_fenced_json = True
                    segment_lines = []
                continue
            if stripped == "```":
                fenced_segments.append("\n".join(segment_lines).strip())
                segment_lines = []
                in_fenced_json = False
                continue
            segment_lines.append(line)
        merged_fenced = "\n".join(segment for segment in fenced_segments if segment).strip()
        if merged_fenced:
            candidates.append(merged_fenced)
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _normalize_markdown_body(lines: list[str]) -> str:
    return normalize_markdown_body_helper(lines)


def _normalize_insights(result: dict[str, Any]) -> list[str]:
    return normalize_insights_helper(result)


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _truncate_text(value: Any, *, limit: int = 240) -> str:
    normalized = _collapse_whitespace(value)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _join_semantic_fragments(fragments: Iterable[str], *, separator: str = " | ") -> str:
    normalized = [_collapse_whitespace(item) for item in fragments if _collapse_whitespace(item)]
    return separator.join(normalized)


def _looks_meta_change_text(text: str) -> bool:
    normalized = _collapse_whitespace(text)
    if not normalized:
        return False
    meta_markers = (
        "정리했습니다",
        "정리합니다",
        "구체화했습니다",
        "반영했습니다",
        "반영된 것을 확인했습니다",
        "일관되게 반영",
        "동기화했습니다",
        "재구성했습니다",
        "업데이트했습니다",
        "개선했습니다",
        "개선 방향",
        "prompt",
        "프롬프트",
        "문서",
        "라우팅",
        "회귀 테스트",
        "regression test",
    )
    return any(marker in normalized.lower() for marker in meta_markers)


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        normalized = str(value).strip()
        return [normalized] if normalized else []
    return []


def _normalize_sprint_report_changes(value: Any) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return changes
    for raw_item in value:
        if not isinstance(raw_item, dict):
            continue
        title = _collapse_whitespace(raw_item.get("title") or "")
        why = _collapse_whitespace(raw_item.get("why") or "")
        what_changed = _collapse_whitespace(
            raw_item.get("what_changed")
            or raw_item.get("what")
            or raw_item.get("behavior")
            or raw_item.get("summary")
            or ""
        )
        meaning = _collapse_whitespace(raw_item.get("meaning") or "")
        how = _collapse_whitespace(raw_item.get("how") or "")
        artifacts = _normalize_string_list(raw_item.get("artifacts"))
        request_ids = _normalize_string_list(raw_item.get("request_ids"))
        if not any((title, why, what_changed, meaning, how, artifacts, request_ids)):
            continue
        changes.append(
            {
                "title": title,
                "why": why,
                "what_changed": what_changed,
                "meaning": meaning,
                "how": how,
                "artifacts": artifacts,
                "request_ids": request_ids,
            }
        )
    return changes


def _normalize_sprint_report_contributions(value: Any) -> list[dict[str, Any]]:
    contributions: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return contributions
    for raw_item in value:
        if isinstance(raw_item, str):
            summary = _collapse_whitespace(raw_item)
            if summary:
                contributions.append({"role": "", "summary": summary, "artifacts": []})
            continue
        if not isinstance(raw_item, dict):
            continue
        role = _collapse_whitespace(raw_item.get("role") or "")
        summary = _collapse_whitespace(
            raw_item.get("summary")
            or raw_item.get("highlight")
            or raw_item.get("what")
            or ""
        )
        artifacts = _normalize_string_list(raw_item.get("artifacts"))
        if not any((role, summary, artifacts)):
            continue
        contributions.append({"role": role, "summary": summary, "artifacts": artifacts})
    return contributions


def _normalize_sprint_report_draft(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized = {
        "headline": _collapse_whitespace(value.get("headline") or value.get("tl_dr") or ""),
        "changes": _normalize_sprint_report_changes(value.get("changes")),
        "timeline": _normalize_string_list(value.get("timeline")),
        "agent_contributions": _normalize_sprint_report_contributions(value.get("agent_contributions")),
        "issues": _normalize_string_list(value.get("issues")),
        "achievements": _normalize_string_list(value.get("achievements")),
        "highlight_artifacts": _normalize_string_list(value.get("highlight_artifacts")),
    }
    if not any(
        (
            normalized["headline"],
            normalized["changes"],
            normalized["timeline"],
            normalized["agent_contributions"],
            normalized["issues"],
            normalized["achievements"],
            normalized["highlight_artifacts"],
        )
    ):
        return {}
    return normalized


def _extract_proposal_acceptance_criteria(proposals: dict[str, Any]) -> list[str]:
    criteria = _normalize_string_list(proposals.get("acceptance_criteria"))
    if criteria:
        return criteria
    design_feedback = proposals.get("design_feedback")
    if isinstance(design_feedback, dict):
        criteria = _normalize_string_list(design_feedback.get("acceptance_criteria"))
        if criteria:
            return criteria
    backlog_item = proposals.get("backlog_item")
    if isinstance(backlog_item, dict):
        criteria = _normalize_string_list(backlog_item.get("acceptance_criteria"))
        if criteria:
            return criteria
    backlog_items = proposals.get("backlog_items")
    if isinstance(backlog_items, list):
        for item in backlog_items:
            if not isinstance(item, dict):
                continue
            criteria = _normalize_string_list(item.get("acceptance_criteria"))
            if criteria:
                return criteria
    return []


def _extract_proposal_required_inputs(proposals: dict[str, Any]) -> list[str]:
    required_inputs = _normalize_string_list(proposals.get("required_inputs"))
    if required_inputs:
        return required_inputs
    design_feedback = proposals.get("design_feedback")
    if isinstance(design_feedback, dict):
        required_inputs = _normalize_string_list(design_feedback.get("required_inputs"))
        if required_inputs:
            return required_inputs
    return []


def _normalize_constraint_point(
    value: str,
    *,
    canonical_prefix: str,
    source_prefixes: tuple[str, ...],
) -> str:
    normalized = _collapse_whitespace(value)
    if not normalized:
        return ""
    lowered = normalized.lower()
    for source_prefix in source_prefixes:
        for normalized_separator in (":", " ", " -"):
            source_with_separator = f"{source_prefix}{normalized_separator}"
            if lowered.startswith(source_with_separator):
                remainder = _collapse_whitespace(normalized[len(source_with_separator) :])
                return f"{canonical_prefix}: {remainder}" if remainder else canonical_prefix
        if lowered == source_prefix:
            return canonical_prefix
    return f"{canonical_prefix}: {normalized}"


def _constraint_point_body(value: str) -> str:
    normalized = _collapse_whitespace(value).lower()
    for prefix in (
        "완료 기준:",
        "완료기준:",
        "추가 입력:",
        "필수 입력:",
        "필수입력:",
        "필요 입력:",
        "필요입력:",
        "required input:",
        "required_inputs:",
        "acceptance criteria:",
        "acceptance_criteria:",
        "acceptancecriteria:",
    ):
        if normalized.startswith(prefix):
            remainder = _collapse_whitespace(normalized[len(prefix) :])
            return remainder if remainder else ""
        prefix_with_space = f"{prefix[:-1]} "
        if normalized.startswith(prefix_with_space):
            remainder = _collapse_whitespace(normalized[len(prefix_with_space) :])
            return remainder if remainder else ""
    return normalized


def _constraint_point_signature(value: str) -> str:
    normalized = _collapse_whitespace(value)
    if not normalized:
        return ""
    signature = _constraint_point_body(normalized)
    return signature if signature else normalized.lower()


def _summarize_proposals(proposals: dict[str, Any]) -> str:
    if not isinstance(proposals, dict) or not proposals:
        return ""
    parts: list[str] = []
    research_report = proposals.get("research_report")
    if isinstance(research_report, dict):
        backing_sources = research_report.get("backing_sources")
        if isinstance(backing_sources, list) and backing_sources:
            parts.append(f"research source {len(backing_sources)}건")
        else:
            parts.append("research 판단 1건")
    backlog_items = proposals.get("backlog_items")
    if isinstance(backlog_items, list) and backlog_items:
        parts.append(f"backlog 후보 {len(backlog_items)}건")
    elif isinstance(proposals.get("backlog_item"), (dict, str)):
        parts.append("backlog 후보 1건")
    acceptance_criteria = _extract_proposal_acceptance_criteria(proposals)
    if acceptance_criteria:
        parts.append(f"완료 기준 {len(acceptance_criteria)}개")
    required_inputs = _extract_proposal_required_inputs(proposals)
    if required_inputs:
        parts.append(f"추가 입력 {len(required_inputs)}개 필요")
    if not parts:
        candidate_keys = [
            str(key).strip()
            for key in proposals.keys()
            if str(key).strip() not in {"routing", "acceptance_criteria", "required_inputs"}
        ]
        if candidate_keys:
            parts.append("제안 항목: " + ", ".join(candidate_keys[:3]))
    return " / ".join(parts)


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for item in values:
        candidate = str(item).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _compact_reference_items(values: list[str], *, limit: int = 3) -> list[str]:
    normalized = _dedupe_preserving_order(values)
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + [f"외 {len(normalized) - limit}건"]


def _first_meaningful_text(*values: Any) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _format_count_summary(counts: dict[str, int], ordered_keys: list[str] | tuple[str, ...]) -> str:
    parts: list[str] = []
    for key in ordered_keys:
        value = int(counts.get(key) or 0)
        if value > 0:
            parts.append(f"{key}:{value}")
    return ", ".join(parts) if parts else "N/A"


def _decorate_sprint_report_title(title: str) -> str:
    return decorate_sprint_report_title_helper(title)


def _is_terminal_todo_status(status: str) -> bool:
    return str(status or "").strip().lower() in {"completed", "committed", "failed", "blocked"}


class _NullDiscordClient:
    def __init__(self, *, client_name: str = ""):
        self.client_name = str(client_name or "").strip()
        self.sent_channels: list[tuple[str, str]] = []
        self.sent_dms: list[tuple[str, str]] = []

    def current_identity(self) -> dict[str, str]:
        return {}

    async def listen(self, _on_message, on_ready=None):
        if on_ready is not None:
            result = on_ready()
            if asyncio.iscoroutine(result):
                await result
        return None

    async def send_channel_message(self, channel_id, content):
        self.sent_channels.append((str(channel_id), str(content)))
        return DiscordMessage(
            message_id="null-channel",
            channel_id=str(channel_id),
            guild_id="",
            author_id="",
            author_name=self.client_name or "null",
            content=str(content),
            is_dm=False,
            mentions_bot=False,
            created_at=datetime.now(UTC),
        )

    async def send_dm(self, user_id, content):
        self.sent_dms.append((str(user_id), str(content)))
        return DiscordMessage(
            message_id="null-dm",
            channel_id="dm",
            guild_id=None,
            author_id="",
            author_name=self.client_name or "null",
            content=str(content),
            is_dm=True,
            mentions_bot=False,
            created_at=datetime.now(UTC),
        )

    async def close(self):
        return None


class TeamService:
    def __init__(
        self,
        workspace_root: str | Path,
        role: str,
        *,
        enable_discord_client: bool = True,
        relay_transport: str = RELAY_TRANSPORT_DISCORD,
    ):
        if role not in TEAM_ROLES:
            raise ValueError(f"Unsupported role: {role}")
        self.paths = RuntimePaths.from_root(workspace_root)
        self.paths.ensure_runtime_dirs()
        self.role = role
        normalized_relay_transport = str(relay_transport or "").strip().lower() or RELAY_TRANSPORT_DISCORD
        if normalized_relay_transport not in VALID_RELAY_TRANSPORTS:
            raise ValueError(
                "relay_transport must be one of: "
                + ", ".join(sorted(VALID_RELAY_TRANSPORTS))
            )
        self.relay_transport = normalized_relay_transport
        self.discord_config = load_discord_agents_config(self.paths.workspace_root)
        self.runtime_config = load_team_runtime_config(self.paths.workspace_root)
        self.agent_utilization_policy = load_agent_utilization_policy(self.paths.workspace_root)
        self.role_config = self.discord_config.get_role(role)
        self.action_executor = ActionExecutor(self.paths, self.runtime_config)
        self.role_runtime = self._build_role_runtime(
            role,
            self.runtime_config.sprint_id,
            session_identity=service_runtime_identity(role),
        )
        self.intent_parser = IntentParserRuntime(
            paths=self.paths,
            sprint_id=self.runtime_config.sprint_id,
            runtime_config=self.runtime_config.role_defaults["orchestrator"],
            session_identity=self._local_runtime_session_identity("parser"),
        )
        self.backlog_sourcer = BacklogSourcingRuntime(
            paths=self.paths,
            sprint_id=self.runtime_config.sprint_id,
            runtime_config=self.runtime_config.role_defaults["orchestrator"],
            session_identity=self._local_runtime_session_identity("sourcer"),
        )
        self.version_controller_runtime = RoleAgentRuntime(
            paths=self.paths,
            role="version_controller",
            sprint_id=self.runtime_config.sprint_id,
            runtime_config=self.runtime_config.role_defaults["orchestrator"],
            agent_root=self.paths.internal_agent_root("version_controller"),
            session_identity=self._local_runtime_session_identity("version_controller"),
        )
        self._sourcer_report_config = self.discord_config.internal_agents.get("sourcer")
        self._sourcer_report_client: DiscordClient | None = None
        self._purge_request_scoped_role_output_files()
        self._role_runtime_cache: dict[tuple[str, str, str], RoleAgentRuntime] = {
            (role, self.runtime_config.sprint_id, service_runtime_identity(role)): self.role_runtime
        }
        self._active_request_ids: set[str] = set()
        self._active_request_ids_lock = asyncio.Lock()
        self._sprint_resume_lock = asyncio.Lock()
        self._role_resume_lock = asyncio.Lock()
        self._pending_role_request_resume_task: asyncio.Task[None] | None = None
        self._internal_relay_consumer_task: asyncio.Task[None] | None = None
        self._backlog_sourcing_lock = threading.Lock()
        self._last_backlog_sourcing_activity: dict[str, Any] = {}
        self._malformed_relay_log_times: dict[str, float] = {}
        self._last_sourcer_report_client_label = ""
        self._last_sourcer_report_reason = ""
        self._last_sourcer_report_category = ""
        self._last_sourcer_report_recovery_action = ""
        self._last_sourcer_report_failure_signature = ""
        self._last_sourcer_report_failure_logged_at = 0.0
        if enable_discord_client:
            self.discord_client = DiscordClient(
                token_env=self.role_config.token_env,
                expected_bot_id=self.role_config.bot_id,
                allowed_bot_author_ids=self.discord_config.trusted_bot_ids - {self.role_config.bot_id},
                always_listen_channel_ids={self.discord_config.relay_channel_id},
                transcript_log_file=self.paths.agent_discord_log(role),
                attachment_dir_resolver=self._resolve_message_attachment_root,
                client_name=role,
            )
        else:
            self.discord_client = _NullDiscordClient(client_name=role)
        self.notification_service = DiscordNotificationService(
            paths=self.paths,
            role=self.role,
            discord_config=self.discord_config,
            runtime_config=self.runtime_config,
            discord_client=self.discord_client,
        )
        if self.agent_utilization_policy.load_error:
            LOGGER.warning(self.agent_utilization_policy.load_error)

    def _agent_capability(self, role: str):
        return get_agent_capability(role, self.agent_utilization_policy)

    def _uses_manual_daily_sprint(self) -> bool:
        return uses_manual_daily_sprint_helper(self.runtime_config.sprint_start_mode)

    def _sprint_uses_manual_flow(self, sprint_state: dict[str, Any] | None = None) -> bool:
        return sprint_uses_manual_flow_helper(
            sprint_start_mode=self.runtime_config.sprint_start_mode,
            sprint_state=sprint_state,
        )

    def _load_active_sprint_state(self) -> dict[str, Any]:
        scheduler_state = self._load_scheduler_state()
        return self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))

    @staticmethod
    def _message_received_at(message: DiscordMessage | None) -> datetime | None:
        created_at = getattr(message, "created_at", None)
        if not isinstance(created_at, datetime):
            return None
        return normalize_runtime_datetime(created_at)

    def _request_started_at_hint(self, request_record: RequestRecord) -> datetime | None:
        for field_name in ("source_message_created_at", "created_at"):
            parsed = self._parse_datetime(str(request_record.get(field_name) or ""))
            if parsed is not None:
                return normalize_runtime_datetime(parsed)
        return None

    @staticmethod
    def _combine_envelope_scope_and_body(envelope: MessageEnvelope) -> str:
        return combine_envelope_scope_and_body_helper(envelope)

    @staticmethod
    def _normalize_kickoff_requirements(value: Any) -> list[str]:
        return normalize_kickoff_requirements_helper(value)

    @staticmethod
    def _clean_kickoff_text(value: Any) -> str:
        return clean_kickoff_text_helper(value)

    @staticmethod
    def _parse_kickoff_text_sections(
        text: str,
    ) -> tuple[str, list[str]]:
        return parse_kickoff_text_sections_helper(text)

    def _extract_manual_sprint_kickoff_payload(self, envelope: MessageEnvelope) -> dict[str, Any]:
        return extract_manual_sprint_kickoff_payload_helper(envelope)

    def _extract_manual_sprint_milestone_title(self, envelope: MessageEnvelope) -> str:
        return extract_manual_sprint_milestone_title_helper(envelope)

    def _is_manual_sprint_start_request(self, envelope: MessageEnvelope) -> bool:
        return is_manual_sprint_start_request_helper(envelope)

    def _is_manual_sprint_finalize_request(self, envelope: MessageEnvelope) -> bool:
        return is_manual_sprint_finalize_request_helper(envelope)

    def _ensure_orchestrator_session_ready_for_sprint_start(self, envelope: MessageEnvelope) -> None:
        if self.role != "orchestrator":
            return
        if not self._is_manual_sprint_start_request(envelope):
            return
        try:
            self.role_runtime.session_manager.ensure_session()
        except Exception:
            LOGGER.exception("Failed to prepare orchestrator session workspace for sprint start request")

    def _build_manual_sprint_names(self, *, sprint_id: str, milestone_title: str) -> tuple[str, str]:
        return build_manual_sprint_names_helper(
            sprint_id=sprint_id,
            milestone_title=milestone_title,
        )

    def _build_idle_current_sprint_markdown(self) -> str:
        return build_idle_current_sprint_markdown_helper()

    def _is_manual_sprint_cutoff_reached(self, sprint_state: dict[str, Any]) -> bool:
        return is_manual_sprint_cutoff_reached_helper(
            sprint_start_mode=self.runtime_config.sprint_start_mode,
            sprint_state=sprint_state,
        )

    def _build_manual_sprint_state(
        self,
        *,
        milestone_title: str,
        trigger: str,
        started_at: datetime | None = None,
        kickoff_brief: str = "",
        kickoff_requirements: list[str] | None = None,
        kickoff_request_text: str = "",
        kickoff_source_request_id: str = "",
        kickoff_reference_artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        return build_manual_sprint_state_helper(
            milestone_title=milestone_title,
            trigger=trigger,
            sprint_cutoff_time=self.runtime_config.sprint_cutoff_time,
            sprint_artifacts_root=self.paths.sprint_artifacts_root,
            git_baseline=capture_git_baseline(self.paths.project_workspace_root),
            build_sprint_id=build_active_sprint_id,
            started_at=started_at,
            kickoff_brief=kickoff_brief,
            kickoff_requirements=kickoff_requirements,
            kickoff_request_text=kickoff_request_text,
            kickoff_source_request_id=kickoff_source_request_id,
            kickoff_reference_artifacts=kickoff_reference_artifacts,
        )

    @staticmethod
    def _is_internal_sprint_request(request_record: dict[str, Any]) -> bool:
        return is_internal_sprint_request_helper(request_record)

    @staticmethod
    def _is_sprint_planning_request(request_record: dict[str, Any]) -> bool:
        return is_sprint_planning_request_helper(request_record)

    @staticmethod
    def _initial_phase_step(request_record: dict[str, Any]) -> str:
        return initial_phase_step_helper(request_record)

    def _is_initial_phase_planner_request(self, request_record: dict[str, Any]) -> bool:
        return is_initial_phase_planner_request_helper(request_record)

    @staticmethod
    def _initial_phase_step_title(step: str) -> str:
        return initial_phase_step_title_helper(step)

    @staticmethod
    def _initial_phase_step_position(step: str) -> int:
        return initial_phase_step_position_helper(step)

    def _next_initial_phase_step(self, step: str) -> str:
        return next_initial_phase_step_helper(step)

    def _initial_phase_step_instruction(self, step: str) -> str:
        return initial_phase_step_instruction_helper(step)

    @staticmethod
    def _is_sourcer_review_request(request_record: dict[str, Any]) -> bool:
        return is_sourcer_review_request_helper(request_record)

    @staticmethod
    def _is_blocked_backlog_review_request(request_record: dict[str, Any]) -> bool:
        return is_blocked_backlog_review_request_helper(request_record)

    @classmethod
    def _is_planner_backlog_review_request(cls, request_record: dict[str, Any]) -> bool:
        return is_planner_backlog_review_request_helper(request_record)

    @staticmethod
    def _is_terminal_internal_request_status(status: str) -> bool:
        return is_terminal_internal_request_status_helper(status)

    def _inspect_task_version_control_state(self, request_record: dict[str, Any]) -> dict[str, Any]:
        baseline = dict(request_record.get("git_baseline") or {})
        repo_root, changed_paths = collect_sprint_owned_paths(self.paths.project_workspace_root, baseline)
        if repo_root is None:
            return {
                "status": "no_repo",
                "repo_root": "",
                "changed_paths": [],
                "message": "git repository를 찾을 수 없습니다.",
            }
        if changed_paths:
            return {
                "status": "pending_changes",
                "repo_root": str(repo_root),
                "changed_paths": changed_paths,
                "message": "현재 task 소유 변경이 아직 commit되지 않았습니다.",
            }
        return {
            "status": "no_changes",
            "repo_root": str(repo_root),
            "changed_paths": [],
            "message": "현재 task 소유 변경 파일이 없습니다.",
        }

    def _record_internal_visited_role(self, request_record: dict[str, Any], role: str) -> None:
        if not self._is_internal_sprint_request(request_record):
            return
        normalized = str(role or "").strip()
        if not normalized:
            return
        visited_roles = [
            str(item).strip()
            for item in (request_record.get("visited_roles") or [])
            if str(item).strip()
        ]
        if normalized not in visited_roles:
            visited_roles.append(normalized)
        request_record["visited_roles"] = visited_roles

    @staticmethod
    def _default_workflow_state() -> dict[str, Any]:
        return default_workflow_state()

    def _research_first_workflow_state(self) -> dict[str, Any]:
        return research_first_workflow_state()

    def _initial_workflow_state_for_internal_request(self) -> dict[str, Any]:
        return initial_workflow_state(
            max(
                1,
                int(
                    self.agent_utilization_policy.implementation_review_cycle_limit
                    or DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT
                ),
            )
        )

    def _request_workflow_state(self, request_record: RequestRecord) -> WorkflowState:
        params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
        raw = params.get("workflow")
        if isinstance(raw, dict):
            return normalize_workflow_state(raw)
        if self._is_sprint_planning_request(request_record):
            return {}
        if not isinstance(raw, dict):
            if self._is_internal_sprint_request(request_record):
                inferred = self._infer_legacy_internal_workflow_state(request_record)
                if inferred:
                    return inferred
            return {}

    def _infer_legacy_internal_workflow_state(self, request_record: dict[str, Any]) -> dict[str, Any]:
        return infer_legacy_internal_workflow_state(request_record)

    def _set_request_workflow_state(self, request_record: RequestRecord, workflow_state: WorkflowState) -> None:
        set_request_workflow_state(request_record, workflow_state)

    @staticmethod
    def _workflow_transition(result: dict[str, Any]) -> dict[str, Any]:
        return workflow_transition(result)

    @staticmethod
    def _workflow_transition_requests_explicit_continuation(transition: dict[str, Any]) -> bool:
        return workflow_transition_requests_explicit_continuation(transition)

    @staticmethod
    def _workflow_transition_requests_validation_handoff(transition: dict[str, Any]) -> bool:
        return workflow_transition_requests_validation_handoff(transition)

    @staticmethod
    def _workflow_review_cycle_limit_reached(workflow_state: dict[str, Any]) -> bool:
        return workflow_review_cycle_limit_reached(workflow_state)

    @staticmethod
    def _workflow_reason(result: dict[str, Any], transition: dict[str, Any], default: str) -> str:
        return workflow_reason(result, transition, default)

    def _workflow_request_context_text(self, request_record: dict[str, Any]) -> str:
        params = dict(request_record.get("params") or {})
        backlog_id = str(params.get("backlog_id") or request_record.get("backlog_id") or "").strip()
        backlog_item = self._load_backlog_item(backlog_id) if backlog_id else {}
        parts = [
            str(request_record.get("scope") or "").strip(),
            str(request_record.get("body") or "").strip(),
            str(backlog_item.get("title") or "").strip(),
            str(backlog_item.get("summary") or "").strip(),
            *[
                str(item).strip()
                for item in (backlog_item.get("acceptance_criteria") or [])
                if str(item).strip()
            ],
        ]
        return " ".join(part for part in parts if part)

    def _workflow_should_close_in_planning(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        current_role: str,
        transition: dict[str, Any],
    ) -> bool:
        workflow_state = self._request_workflow_state(request_record)
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        artifacts = [
            str(item).strip()
            for item in (result.get("artifacts") or [])
            if str(item).strip()
        ]
        request_text = self._workflow_request_context_text(request_record)
        return workflow_should_close_in_planning_helper(
            workflow_state=workflow_state,
            current_role=current_role,
            transition=transition,
            proposals=proposals,
            artifacts=artifacts,
            request_indicates_execution_flag=self._request_indicates_execution(
                intent=str(request_record.get("intent") or "").strip().lower(),
                text=request_text,
            ),
        )

    def _enforce_workflow_role_report_contract(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return result
        role = str(result.get("role") or request_record.get("current_role") or "").strip().lower()
        transition = self._workflow_transition(result)
        updated_result = apply_workflow_role_report_contract(
            workflow_state=workflow_state,
            role=role,
            result=result,
            planner_doc_contract=self._workflow_planner_doc_contract_violation(request_record, result)
            if role == "planner"
            else None,
            qa_requires_planner_reopen_flag=self._qa_result_requires_planner_reopen(request_record, result)
            if role == "qa"
            else False,
            qa_runtime_sync_anomaly_flag=self._qa_result_is_runtime_sync_anomaly(request_record, result)
            if role == "qa"
            else False,
            transition=transition,
        )
        if updated_result is result:
            return result
        return normalize_role_payload(updated_result)

    def _workflow_review_cycle_limit_block_decision(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            build_workflow_review_cycle_limit_block_decision(
                workflow_state,
                reason=reason,
                category=category,
            )
        )

    def _workflow_routing_context(
        self,
        next_role: str,
        *,
        workflow_state: dict[str, Any],
        reason: str,
        preferred_role: str = "",
        matched_signals: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._build_routing_context(
            next_role,
            reason=reason,
            preferred_role=preferred_role,
            selection_source=WORKFLOW_SELECTION_SOURCE,
            matched_signals=matched_signals or [],
            policy_source=WORKFLOW_POLICY_SOURCE,
            routing_phase=str(workflow_state.get("phase") or ""),
            request_state_class=str(workflow_state.get("step") or ""),
        )

    def _workflow_route_decision(
        self,
        next_role: str,
        *,
        workflow_state: dict[str, Any],
        reason: str,
        preferred_role: str = "",
        matched_signals: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "next_role": next_role,
            "routing_context": self._workflow_routing_context(
                next_role,
                workflow_state=workflow_state,
                reason=reason,
                preferred_role=preferred_role,
                matched_signals=matched_signals,
            ),
            "workflow_state": workflow_state,
        }

    def _materialize_workflow_decision(self, decision: dict[str, Any] | None) -> dict[str, Any] | None:
        if decision is None:
            return None
        materialized = dict(decision)
        next_role = str(materialized.get("next_role") or "").strip()
        if next_role:
            materialized["routing_context"] = self._workflow_routing_context(
                next_role,
                workflow_state=dict(materialized.get("workflow_state") or {}),
                reason=str(materialized.pop("route_reason", "")).strip(),
                preferred_role=str(materialized.pop("preferred_role", "")).strip(),
                matched_signals=[
                    str(item).strip()
                    for item in (materialized.pop("matched_signals", []) or [])
                    if str(item).strip()
                ],
            )
        else:
            materialized["routing_context"] = {}
            materialized.pop("route_reason", None)
            materialized.pop("preferred_role", None)
            materialized.pop("matched_signals", None)
        return materialized

    def _workflow_terminal_block_decision(
        self,
        workflow_state: dict[str, Any],
        *,
        summary: str,
        category: str = "",
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            build_workflow_terminal_block_decision(
                workflow_state,
                summary=summary,
                category=category,
            )
        )

    def _workflow_route_to_research_initial(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            workflow_route_to_research_initial_decision(
                workflow_state,
                reason=reason,
            )
        )

    def _workflow_route_to_planner_draft(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            workflow_route_to_planner_draft_decision(
                workflow_state,
                reason=reason,
                category=category,
            )
        )

    def _workflow_route_to_planner_finalize(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            workflow_route_to_planner_finalize_decision(
                workflow_state,
                reason=reason,
                category=category,
            )
        )

    def _workflow_route_to_planning_advisory(
        self,
        workflow_state: dict[str, Any],
        *,
        role: str,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            workflow_route_to_planning_advisory_decision(
                workflow_state,
                role=role,
                reason=reason,
                category=category,
            )
        )

    def _workflow_route_to_architect_guidance(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            workflow_route_to_architect_guidance_decision(
                workflow_state,
                reason=reason,
                category=category,
            )
        )

    def _workflow_route_to_developer_build(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        step: str = WORKFLOW_STEP_DEVELOPER_BUILD,
        category: str = "",
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            workflow_route_to_developer_build_decision(
                workflow_state,
                reason=reason,
                step=step,
                category=category,
            )
        )

    def _workflow_route_to_architect_review(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            workflow_route_to_architect_review_decision(
                workflow_state,
                reason=reason,
                category=category,
            )
        )

    def _workflow_route_to_qa(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            workflow_route_to_qa_decision(
                workflow_state,
                reason=reason,
            )
        )

    def _workflow_complete_decision(
        self,
        workflow_state: dict[str, Any],
        *,
        summary: str,
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            build_workflow_complete_decision(
                workflow_state,
                summary=summary,
            )
        )

    def _workflow_reopen_decision(
        self,
        workflow_state: dict[str, Any],
        *,
        current_role: str,
        category: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._materialize_workflow_decision(
            build_workflow_reopen_decision(
                workflow_state,
                current_role=current_role,
                category=category,
                reason=reason,
            )
        )

    def _derive_workflow_routing_decision(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> dict[str, Any] | None:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return None
        current_role = str(result.get("role") or sender_role or request_record.get("current_role") or "").strip().lower()
        if self._is_sprint_planning_request(request_record) and current_role == "planner":
            return None
        transition = self._workflow_transition(result)
        reason = self._workflow_reason(result, transition, "workflow step을 계속 진행합니다.")
        should_close_in_planning = self._workflow_should_close_in_planning(
            request_record,
            result,
            current_role=current_role,
            transition=transition,
        )
        return self._materialize_workflow_decision(
            derive_pure_workflow_routing_decision(
                workflow_state,
                transition,
                current_role=current_role,
                reason=reason,
                should_close_in_planning=should_close_in_planning,
            )
        )

    def _coerce_nonterminal_workflow_role_result(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> dict[str, Any]:
        if not self._request_workflow_state(request_record):
            return result
        result_status = str(result.get("status") or "").strip().lower()
        error_text = str(result.get("error") or "").strip()
        if result_status not in {"failed", "blocked"} and not error_text:
            return result
        transition = self._workflow_transition(result)
        if not self._workflow_transition_requests_explicit_continuation(transition):
            return result
        workflow_decision = self._derive_workflow_routing_decision(
            request_record,
            result,
            sender_role=sender_role,
        )
        return coerce_nonterminal_workflow_role_result_helper(
            result,
            transition=transition,
            workflow_decision=workflow_decision,
        )

    @staticmethod
    def _normalize_routing_path_nodes(raw_nodes: Any) -> list[str]:
        if not isinstance(raw_nodes, list):
            return []
        return [str(item).strip() for item in raw_nodes if str(item).strip()]

    @staticmethod
    def _format_routing_path_node(*, phase: str = "", step: str = "", role: str = "") -> str:
        normalized_role = str(role or "").strip().lower() or "unknown"
        normalized_phase = str(phase or "").strip().lower()
        normalized_step = str(step or "").strip().lower()
        if normalized_phase and normalized_step:
            return f"{normalized_phase}/{normalized_step}@{normalized_role}"
        if normalized_phase:
            return f"{normalized_phase}@{normalized_role}"
        if normalized_step:
            return f"{normalized_step}@{normalized_role}"
        return normalized_role

    def _request_routing_stage_parts(self, request_record: dict[str, Any]) -> tuple[str, str]:
        workflow_state = self._request_workflow_state(request_record)
        workflow_phase = str(workflow_state.get("phase") or "").strip().lower()
        workflow_step = str(workflow_state.get("step") or "").strip().lower()
        if workflow_phase or workflow_step:
            return workflow_phase, workflow_step
        params = dict(request_record.get("params") or {})
        sprint_phase = str(params.get("sprint_phase") or "").strip().lower()
        sprint_step = str(params.get("initial_phase_step") or "").strip().lower()
        return sprint_phase, sprint_step

    def _current_request_routing_node(self, request_record: dict[str, Any], role: str) -> str:
        phase, step = self._request_routing_stage_parts(request_record)
        if phase or step:
            return self._format_routing_path_node(phase=phase, step=step, role=role)
        return str(role or "").strip().lower()

    def _seed_sprint_routing_path_nodes(self, sprint_state: dict[str, Any] | None = None) -> list[str]:
        nodes = ["start"]
        iterations = list((sprint_state or {}).get("planning_iterations") or [])
        for entry in iterations:
            if not isinstance(entry, dict):
                continue
            node = self._format_routing_path_node(
                phase=str(entry.get("phase") or "").strip().lower(),
                step=str(entry.get("step") or "").strip().lower(),
                role="planner",
            )
            if node and node != nodes[-1]:
                nodes.append(node)
        return nodes

    def _latest_sprint_routing_path_nodes(self, sprint_state: dict[str, Any]) -> list[str]:
        for activity in reversed(list(sprint_state.get("recent_activity") or [])):
            if not isinstance(activity, dict):
                continue
            nodes = self._normalize_routing_path_nodes(activity.get("routing_path_nodes"))
            if nodes:
                return nodes
        for event in reversed(self._load_sprint_event_entries(sprint_state)):
            payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
            nodes = self._normalize_routing_path_nodes(payload.get("routing_path_nodes"))
            if nodes:
                return nodes
        return []

    def _build_sprint_routing_path_nodes(self, request_record: dict[str, Any], next_role: str) -> list[str]:
        if not self._is_internal_sprint_request(request_record):
            return []
        params = dict(request_record.get("params") or {})
        sprint_id = str(request_record.get("sprint_id") or params.get("sprint_id") or "").strip()
        sprint_state = self._load_sprint_state(sprint_id) if sprint_id else {}
        nodes = self._latest_sprint_routing_path_nodes(sprint_state) if sprint_state else []
        if not nodes:
            nodes = self._seed_sprint_routing_path_nodes(sprint_state)
        current_node = self._current_request_routing_node(request_record, next_role)
        if current_node and current_node != nodes[-1]:
            nodes.append(current_node)
        return nodes

    def _build_handoff_routing_path(
        self,
        request_record: dict[str, Any],
        *,
        source_role: str,
        target_role: str,
    ) -> str:
        return build_handoff_routing_path_helper(
            self,
            request_record,
            source_role=source_role,
            target_role=target_role,
        )

    def _build_internal_sprint_delegation_payload(
        self,
        request_record: dict[str, Any],
        next_role: str,
    ) -> dict[str, Any]:
        return build_internal_sprint_delegation_payload_helper(self, request_record, next_role)

    @staticmethod
    def _summarize_internal_sprint_activity_details(payload: dict[str, Any] | None = None) -> str:
        if not isinstance(payload, dict):
            return ""
        details: list[str] = []
        next_role = str(payload.get("next_role") or "").strip()
        if next_role:
            details.append(f"next={next_role}")
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            details.append(f"session={session_id}")
        session_workspace = str(payload.get("session_workspace") or "").strip()
        if session_workspace:
            details.append(f"workspace={_truncate_text(session_workspace, limit=48)}")
        artifacts = [str(item).strip() for item in (payload.get("artifacts") or []) if str(item).strip()]
        if artifacts:
            details.append(f"artifacts={len(artifacts)}")
        error = str(payload.get("error") or "").strip()
        if error:
            details.append(f"error={_truncate_text(error, limit=80)}")
        return " | ".join(details)

    def _record_internal_sprint_activity(
        self,
        request_record: dict[str, Any],
        *,
        event_type: str,
        role: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if not self._is_internal_sprint_request(request_record):
            return
        params = dict(request_record.get("params") or {})
        sprint_id = str(request_record.get("sprint_id") or params.get("sprint_id") or "").strip()
        if not sprint_id:
            return
        sprint_state = self._load_sprint_state(sprint_id)
        if not sprint_state:
            return
        timestamp = utc_now_iso()
        details = self._summarize_internal_sprint_activity_details(payload)
        activity = {
            "timestamp": timestamp,
            "event_type": str(event_type or "").strip() or "activity",
            "role": str(role or "").strip() or "unknown",
            "status": str(status or "").strip() or "N/A",
            "request_id": str(request_record.get("request_id") or "").strip(),
            "todo_id": str(request_record.get("todo_id") or params.get("todo_id") or "").strip(),
            "backlog_id": str(request_record.get("backlog_id") or params.get("backlog_id") or "").strip(),
            "summary": _truncate_text(summary, limit=220) or "없음",
            "details": details,
        }
        routing_path = str((payload or {}).get("routing_path") or "").strip()
        routing_path_nodes = self._normalize_routing_path_nodes((payload or {}).get("routing_path_nodes"))
        if routing_path:
            activity["routing_path"] = routing_path
        if routing_path_nodes:
            activity["routing_path_nodes"] = routing_path_nodes
        recent_activity = [dict(item) for item in (sprint_state.get("recent_activity") or []) if isinstance(item, dict)]
        recent_activity.append(activity)
        sprint_state["recent_activity"] = recent_activity[-RECENT_SPRINT_ACTIVITY_LIMIT:]
        sprint_state["last_activity_at"] = timestamp
        self._save_sprint_state(sprint_state)
        event_payload = {
            "role": activity["role"],
            "status": activity["status"],
            "request_id": activity["request_id"],
            "todo_id": activity["todo_id"],
            "backlog_id": activity["backlog_id"],
        }
        if details:
            event_payload["details"] = details
        if routing_path:
            event_payload["routing_path"] = routing_path
        if routing_path_nodes:
            event_payload["routing_path_nodes"] = routing_path_nodes
        self._append_sprint_event(
            sprint_id,
            event_type=str(event_type or "").strip() or "activity",
            summary=activity["summary"],
            payload=event_payload,
        )
        LOGGER.info(
            "sprint_activity role=%s event=%s status=%s sprint_id=%s request_id=%s todo_id=%s backlog_id=%s summary=%s details=%s",
            activity["role"],
            activity["event_type"],
            activity["status"],
            sprint_id,
            activity["request_id"] or "N/A",
            activity["todo_id"] or "N/A",
            activity["backlog_id"] or "N/A",
            activity["summary"] or "없음",
            details or "없음",
        )

    def _request_routing_text(self, request_record: dict[str, Any], result: dict[str, Any]) -> str:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        routing = dict(proposals.get("routing") or {}) if isinstance(proposals.get("routing"), dict) else {}
        parts = [
            str(request_record.get("intent") or ""),
            str(request_record.get("scope") or ""),
            str(request_record.get("body") or ""),
            str(result.get("summary") or ""),
            str(routing.get("reason") or ""),
            _summarize_proposals(proposals),
        ]
        return self._normalize_reference_text(" ".join(part for part in parts if str(part).strip()))

    def _has_explicit_planner_reentry_signal(self, result: dict[str, Any]) -> bool:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        suggested_next_step = (
            dict(proposals.get("suggested_next_step") or {})
            if isinstance(proposals.get("suggested_next_step"), dict)
            else {}
        )
        if str(suggested_next_step.get("owner") or "").strip() == "planner":
            return True
        routing = dict(proposals.get("routing") or {}) if isinstance(proposals.get("routing"), dict) else {}
        reference_text = self._normalize_reference_text(
            " ".join(
                part
                for part in (
                    str(result.get("summary") or ""),
                    str(routing.get("reason") or ""),
                    str(suggested_next_step.get("reason") or ""),
                )
                if str(part).strip()
            )
        )
        return any(
            marker in reference_text
            for marker in (
                "planner",
                "planning",
                "backlog",
                "기획",
                "계획",
                "구조화",
                "재정리",
            )
        )

    def _match_reference_terms(
        self,
        terms: tuple[str, ...],
        *,
        text: str,
        prefix: str,
        limit: int,
    ) -> list[str]:
        return match_reference_terms_helper(terms, text=text, prefix=prefix, limit=limit)

    def _routing_signal_matches(
        self,
        role: str,
        *,
        intent: str,
        text: str,
    ) -> list[str]:
        return routing_signal_matches_helper(role, policy=self.agent_utilization_policy, intent=intent, text=text)

    def _strongest_domain_matches(self, role: str, *, text: str) -> list[str]:
        return strongest_domain_matches_helper(role, policy=self.agent_utilization_policy, text=text)

    def _preferred_skill_matches(self, role: str, *, text: str) -> list[str]:
        return preferred_skill_matches_helper(role, policy=self.agent_utilization_policy, text=text)

    def _behavior_trait_matches(self, role: str, *, text: str) -> list[str]:
        return behavior_trait_matches_helper(role, policy=self.agent_utilization_policy, text=text)

    def _should_not_handle_matches(self, role: str, *, text: str) -> list[str]:
        return should_not_handle_matches_helper(role, policy=self.agent_utilization_policy, text=text)

    @staticmethod
    def _phase_for_role(role: str) -> str:
        return routing_phase_for_role(role)

    def _role_hint_score(self, role: str, *, intent: str, text: str) -> int:
        return role_hint_score_helper(role, policy=self.agent_utilization_policy, intent=intent, text=text)

    def _execution_evidence_score(self, role: str, *, intent: str, text: str) -> int:
        return execution_evidence_score_helper(role, policy=self.agent_utilization_policy, intent=intent, text=text)

    def _request_indicates_execution(self, *, intent: str, text: str) -> bool:
        return request_indicates_execution_helper(policy=self.agent_utilization_policy, intent=intent, text=text)

    def _classify_request_state(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        current_role: str,
        preferred_role: str,
        selection_source: str,
        text: str,
    ) -> str:
        del result
        return classify_request_state_helper(
            request_record,
            policy=self.agent_utilization_policy,
            current_role=current_role,
            preferred_role=preferred_role,
            selection_source=selection_source,
            text=text,
            is_internal_sprint_request=self._is_internal_sprint_request(request_record),
        )

    def _derive_routing_phase(
        self,
        *,
        current_role: str,
        preferred_role: str,
        selection_source: str,
        request_state_class: str,
        intent: str,
        text: str,
    ) -> str:
        return derive_routing_phase_helper(
            policy=self.agent_utilization_policy,
            current_role=current_role,
            preferred_role=preferred_role,
            selection_source=selection_source,
            request_state_class=request_state_class,
            intent=intent,
            text=text,
        )

    def _score_candidate_role(
        self,
        role: str,
        *,
        intent: str,
        text: str,
        routing_phase: str,
        request_state_class: str,
    ) -> dict[str, Any]:
        return score_candidate_role_helper(
            role,
            policy=self.agent_utilization_policy,
            intent=intent,
            text=text,
            routing_phase=routing_phase,
            request_state_class=request_state_class,
        )

    def _build_governed_routing_selection(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        current_role: str,
        preferred_role: str,
        selection_source: str,
    ) -> dict[str, Any]:
        return build_governed_routing_selection_helper(
            request_record,
            policy=self.agent_utilization_policy,
            current_role=current_role,
            preferred_role=preferred_role,
            selection_source=selection_source,
            routing_text=self._request_routing_text(request_record, result),
            is_internal_sprint_request=self._is_internal_sprint_request(request_record),
            planner_reentry_has_explicit_signal=self._has_explicit_planner_reentry_signal(result),
        )

    def _build_routing_context(
        self,
        role: str,
        *,
        reason: str,
        preferred_role: str = "",
        selection_source: str = "",
        matched_signals: list[str] | None = None,
        override_reason: str = "",
        matched_strongest_domains: list[str] | None = None,
        matched_preferred_skills: list[str] | None = None,
        matched_behavior_traits: list[str] | None = None,
        policy_source: str = "",
        routing_phase: str = "",
        request_state_class: str = "",
        score_total: int = 0,
        score_breakdown: dict[str, Any] | None = None,
        candidate_summary: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        capability = self._agent_capability(role)
        return {
            "selected_role": role,
            "preferred_role": str(preferred_role or "").strip(),
            "selection_source": str(selection_source or "").strip(),
            "policy_source": str(policy_source or self.agent_utilization_policy.policy_source).strip(),
            "routing_phase": str(routing_phase or "").strip(),
            "request_state_class": str(request_state_class or "").strip(),
            "reason": str(reason or "").strip(),
            "override_reason": str(override_reason or "").strip(),
            "matched_signals": [
                str(item).strip()
                for item in (matched_signals or [])
                if str(item).strip()
            ],
            "matched_strongest_domains": [
                str(item).strip()
                for item in (matched_strongest_domains or [])
                if str(item).strip()
            ],
            "matched_preferred_skills": [
                str(item).strip()
                for item in (matched_preferred_skills or [])
                if str(item).strip()
            ],
            "matched_behavior_traits": [
                str(item).strip()
                for item in (matched_behavior_traits or [])
                if str(item).strip()
            ],
            "score_total": int(score_total or 0),
            "score_breakdown": dict(score_breakdown or {}),
            "candidate_summary": list(candidate_summary or []),
            "selected_for_strength": ", ".join(capability.strongest_for[:2]),
            "suggested_skills": list(capability.preferred_skills),
            "expected_behavior": capability.expected_behavior,
            "behavior_traits": list(capability.behavior_traits),
            "role_summary": capability.summary,
        }

    def _derive_routing_decision_after_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> dict[str, Any]:
        return derive_routing_decision_after_report_helper(
            self,
            request_record,
            result,
            sender_role=sender_role,
        )

    def _derive_next_role_after_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> str:
        decision = self._derive_routing_decision_after_report(
            request_record,
            result,
            sender_role=sender_role,
        )
        return str(decision.get("next_role") or "").strip()

    @staticmethod
    def _normalize_reference_text(value: str) -> str:
        return normalize_reference_text_helper(value)

    @staticmethod
    def _verification_result_payload(result: dict[str, Any]) -> dict[str, Any]:
        return verification_result_payload(result)

    def _extract_ready_planning_artifact(self, result: dict[str, Any]) -> str:
        return extract_ready_planning_artifact_helper(result)

    def _extract_verification_related_request_ids(self, result: dict[str, Any]) -> list[str]:
        return extract_verification_related_request_ids_helper(result)

    def _is_blocked_planning_request_waiting_for_document(self, request_record: dict[str, Any]) -> bool:
        return is_blocked_planning_request_waiting_for_document_helper(request_record)

    def _request_mentions_artifact(self, request_record: dict[str, Any], artifact_path: str) -> bool:
        return request_mentions_artifact_helper(request_record, artifact_path)

    def _iter_requests_newest_first(self) -> list[dict[str, Any]]:
        return sorted(
            iter_request_records(self.paths),
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )

    def _find_recent_ready_planning_verification(
        self,
        *,
        author_id: str,
        channel_id: str,
    ) -> tuple[dict[str, Any], str]:
        return find_recent_ready_planning_verification_helper(
            self._iter_requests_newest_first(),
            author_id=author_id,
            channel_id=channel_id,
            now=utc_now(),
            recency_seconds=PLANNING_CONTEXT_RECENCY_SECONDS,
            parse_datetime=self._parse_datetime,
        )

    def _find_blocked_requests_for_verified_artifact(
        self,
        verification_request: dict[str, Any],
        result: dict[str, Any],
        *,
        author_id: str,
        channel_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        return find_blocked_requests_for_verified_artifact_helper(
            verification_request,
            result,
            author_id=author_id,
            channel_id=channel_id,
            load_request=self._load_request,
            candidate_requests=self._iter_requests_newest_first(),
        )

    async def _resume_request_with_context(
        self,
        request_record: dict[str, Any],
        *,
        next_role: str,
        summary: str,
        artifact_path: str = "",
        verified_by_request_id: str = "",
        followup_message_id: str = "",
        followup_body: str = "",
    ) -> bool:
        selection = self._build_governed_routing_selection(
            request_record,
            {},
            current_role=str(request_record.get("current_role") or "planner"),
            preferred_role=str(next_role or "").strip(),
            selection_source="planning_resume",
        )
        normalized_next_role = str(selection.get("selected_role") or "").strip() or "planner"
        routing_context = self._build_routing_context(
            normalized_next_role,
            **build_resume_routing_context_kwargs_helper(
                selection,
                selected_role=normalized_next_role,
                summary=summary,
            ),
        )
        request_record = apply_resume_request_update_helper(
            request_record,
            next_role=normalized_next_role,
            summary=summary,
            routing_context=routing_context,
            artifact_path=artifact_path,
            verified_by_request_id=verified_by_request_id,
            followup_message_id=followup_message_id,
            followup_body=followup_body,
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="resumed",
            summary=summary,
            result=dict(request_record.get("result") or {}),
        )
        return await self._delegate_request(request_record, normalized_next_role)

    def _enrich_planning_envelope_with_recent_verification(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> MessageEnvelope:
        if planning_envelope_has_explicit_source_context(envelope):
            return envelope
        author_id, channel_id = request_identity_from_envelope(message, envelope, forwarded=forwarded)
        verification_request, artifact_path = self._find_recent_ready_planning_verification(
            author_id=author_id,
            channel_id=channel_id,
        )
        return build_planning_envelope_with_inferred_verification(
            envelope,
            verification_request_id=str(verification_request.get("request_id") or ""),
            artifact_path=artifact_path,
        )

    async def _resume_blocked_planning_request_from_recent_context(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> tuple[dict[str, Any], bool] | None:
        author_id, channel_id = request_identity_from_envelope(message, envelope, forwarded=forwarded)
        verification_request, _artifact_path = self._find_recent_ready_planning_verification(
            author_id=author_id,
            channel_id=channel_id,
        )
        if not verification_request:
            return None
        candidates, artifact_path = self._find_blocked_requests_for_verified_artifact(
            verification_request,
            dict(verification_request.get("result") or {}) if isinstance(verification_request.get("result"), dict) else {},
            author_id=author_id,
            channel_id=channel_id,
        )
        if len(candidates) != 1:
            return None
        resumed_request = candidates[0]
        relay_sent = await self._resume_request_with_context(
            resumed_request,
            next_role=str(resumed_request.get("current_role") or resumed_request.get("next_role") or "planner"),
            summary="검증 완료된 기획 문서를 연결해 기존 blocked 요청을 재개했습니다.",
            artifact_path=artifact_path,
            verified_by_request_id=str(verification_request.get("request_id") or ""),
            followup_message_id=message.message_id,
            followup_body=str(envelope.body or "").strip(),
        )
        return resumed_request, relay_sent

    async def _maybe_reopen_blocked_duplicate_request(
        self,
        duplicate_request: dict[str, Any],
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> tuple[dict[str, Any], bool, str] | None:
        if str(duplicate_request.get("status") or "").strip().lower() != "blocked":
            return None
        followup = analyze_blocked_duplicate_followup_helper(duplicate_request, envelope)
        if not followup.new_artifacts and not followup.has_new_body:
            if not self._should_retry_same_input_blocked_request(duplicate_request):
                return None
            duplicate_request = retry_blocked_duplicate_request_helper(
                duplicate_request,
                message=message,
                envelope=envelope,
                forwarded=forwarded,
                routing_context=self._build_routing_context(
                    "orchestrator",
                    reason="Retrying the existing blocked orchestrator-owned request from a repeated user request.",
                    preferred_role="orchestrator",
                    selection_source="blocked_retry",
                ),
            )
            self._save_request(duplicate_request)
            await self._run_local_orchestrator_request(duplicate_request)
            return duplicate_request, True, "retried"
        duplicate_request, followup = augment_blocked_duplicate_request_helper(
            duplicate_request,
            envelope=envelope,
        )
        relay_sent = await self._resume_request_with_context(
            duplicate_request,
            next_role=str(duplicate_request.get("current_role") or duplicate_request.get("next_role") or "planner"),
            summary="후속 요청에서 보강된 입력을 반영해 기존 blocked 요청을 재개했습니다.",
            artifact_path=followup.new_artifacts[0] if followup.new_artifacts else "",
            verified_by_request_id=str(dict(envelope.params).get("inferred_source_request_id") or ""),
            followup_message_id=message.message_id,
            followup_body=followup.followup_body,
        )
        return duplicate_request, relay_sent, "augmented"

    def _should_retry_same_input_blocked_request(self, request_record: dict[str, Any]) -> bool:
        if str(request_record.get("status") or "").strip().lower() != "blocked":
            return False
        if self._is_blocked_planning_request_waiting_for_document(request_record):
            return False
        current_role = str(request_record.get("current_role") or "").strip().lower()
        next_role = str(request_record.get("next_role") or "").strip().lower()
        owner_role = str(request_record.get("owner_role") or "").strip().lower()
        result_role = str(dict(request_record.get("result") or {}).get("role") or "").strip().lower()
        orchestrator_owned = "orchestrator" in {current_role, next_role, owner_role, result_role}
        return orchestrator_owned

    async def _resume_requests_from_verification_result(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> list[str]:
        author_id = str(dict(request_record.get("reply_route") or {}).get("author_id") or "").strip()
        channel_id = str(dict(request_record.get("reply_route") or {}).get("channel_id") or "").strip()
        if not author_id or not channel_id:
            return []
        candidates, artifact_path = self._find_blocked_requests_for_verified_artifact(
            request_record,
            result,
            author_id=author_id,
            channel_id=channel_id,
        )
        if not candidates:
            return []
        resumed_request_ids: list[str] = []
        for blocked_request in candidates:
            await self._resume_request_with_context(
                blocked_request,
                next_role=str(blocked_request.get("current_role") or blocked_request.get("next_role") or "planner"),
                summary="검증 완료된 기획 문서를 연결해 기존 blocked 요청을 재개했습니다.",
                artifact_path=artifact_path,
                verified_by_request_id=str(request_record.get("request_id") or ""),
            )
            resumed_request_ids.append(str(blocked_request.get("request_id") or ""))
        return resumed_request_ids

    async def _wait_for_internal_request_result(self, request_id: str) -> dict[str, Any]:
        normalized = str(request_id or "").strip()
        if not normalized:
            return {}
        while True:
            request_record = self._load_request(normalized)
            status = str(request_record.get("status") or "").strip().lower()
            if self._is_terminal_internal_request_status(status):
                result = dict(request_record.get("result") or {})
                if result:
                    return result
                return {
                    "request_id": normalized,
                    "role": str(request_record.get("current_role") or "orchestrator"),
                    "status": status or "failed",
                    "summary": str(request_record.get("body") or request_record.get("scope") or "").strip(),
                    "insights": [],
                    "proposals": {},
                    "artifacts": [str(item) for item in request_record.get("artifacts") or []],
                    "next_role": "",
                    "error": "",
                }
            await asyncio.sleep(INTERNAL_REQUEST_POLL_SECONDS)

    async def run(self) -> None:
        if self._is_internal_relay_enabled():
            self._internal_relay_consumer_task = asyncio.create_task(self._consume_internal_relay_loop())
        if self.role != "orchestrator":
            try:
                await self._listen_forever()
            finally:
                if self._internal_relay_consumer_task is not None:
                    self._internal_relay_consumer_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._internal_relay_consumer_task
            return
        scheduler_task = asyncio.create_task(self._scheduler_loop())
        sourcing_task = asyncio.create_task(self._backlog_sourcing_loop())
        try:
            await self._listen_forever()
        finally:
            scheduler_task.cancel()
            sourcing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
            with contextlib.suppress(asyncio.CancelledError):
                await sourcing_task
            if self._internal_relay_consumer_task is not None:
                self._internal_relay_consumer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._internal_relay_consumer_task

    async def _listen_forever(self) -> None:
        await listen_forever_helper(
            self,
            retry_seconds=LISTENER_RETRY_SECONDS,
            logger=LOGGER,
        )

    async def _on_ready(self) -> None:
        await on_ready_helper(self)

    async def handle_message(self, message: DiscordMessage) -> None:
        await handle_message_helper(self, message)

    def _is_message_allowed(self, message: DiscordMessage) -> bool:
        return is_message_allowed_helper(self, message)

    def _is_trusted_relay_message(self, message: DiscordMessage) -> bool:
        return is_trusted_relay_message_helper(self, message)

    def _is_internal_relay_enabled(self) -> bool:
        return self.relay_transport == RELAY_TRANSPORT_INTERNAL

    def _internal_relay_root(self) -> Path:
        return internal_relay_root(self.paths)

    def _internal_relay_inbox_dir(self, role: str) -> Path:
        return internal_relay_inbox_dir(self.paths, role)

    def _internal_relay_archive_dir(self, role: str) -> Path:
        return internal_relay_archive_dir(self.paths, role)

    @staticmethod
    def _is_internal_relay_summary_content(content: str) -> bool:
        return is_internal_relay_summary_content(content, marker=INTERNAL_RELAY_SUMMARY_MARKER)

    def _is_internal_relay_summary_message(self, message: DiscordMessage) -> bool:
        if not self._is_trusted_relay_message(message):
            return False
        return self._is_internal_relay_summary_content(message.content)

    def _log_malformed_trusted_relay(self, *, reason: str, kind: str) -> None:
        normalized_kind = str(kind or "").strip() or "none"
        cache_key = f"{self.role}:{reason}:{normalized_kind}"
        now = time.monotonic()
        last_logged_at = self._malformed_relay_log_times.get(cache_key)
        if last_logged_at is not None and now - last_logged_at < MALFORMED_RELAY_LOG_WINDOW_SECONDS:
            return
        self._malformed_relay_log_times[cache_key] = now
        LOGGER.debug(
            "Ignoring malformed trusted relay for role %s: %s (%s)",
            self.role,
            reason,
            normalized_kind,
        )

    async def _announce_startup(self) -> None:
        current_identity = getattr(self.discord_client, "current_identity", None)
        identity = current_identity() if callable(current_identity) else {}
        scheduler_state = self._load_scheduler_state()
        await announce_startup_notification_helper(
            self.notification_service,
            role=self.role,
            identity=identity,
            active_sprint_id=str(scheduler_state.get("active_sprint_id") or "").strip(),
            startup_channel_id=self.discord_config.startup_channel_id,
            send_channel_message=lambda chunk: self.discord_client.send_channel_message(
                self.discord_config.startup_channel_id,
                chunk,
            ),
            record_startup_notification_state=self._record_startup_notification_state,
            log_warning=LOGGER.warning,
        )

    def _format_sprint_scope(self, *, sprint_id: str = "") -> str:
        scheduler_state = self._load_scheduler_state()
        active_sprint_id = str(sprint_id or scheduler_state.get("active_sprint_id") or "").strip()
        return f"현재 스프린트: {active_sprint_id or '없음'}"

    def _load_agent_state(self) -> dict[str, Any]:
        state = read_json(self.paths.agent_state_file(self.role))
        if isinstance(state, dict):
            return state
        return {}

    def _listener_state_metadata(self, *, connected_bot_id: str = "") -> dict[str, Any]:
        expected_bot_id = str(self.role_config.bot_id or "").strip()
        normalized_connected_bot_id = str(connected_bot_id or "").strip()
        return {
            "listener_configured_role": self.role,
            "listener_resolved_workspace_root": str(self.paths.workspace_root),
            "listener_discord_config_path": str(self.discord_config.config_path or ""),
            "listener_expected_bot_id": expected_bot_id,
            "listener_identity_matches_expected": bool(
                expected_bot_id
                and normalized_connected_bot_id
                and normalized_connected_bot_id == expected_bot_id
            ),
        }

    def _record_startup_notification_state(
        self,
        *,
        status: str,
        error: str,
        attempted_channel: str,
        attempts: int,
        fallback_target: str,
    ) -> None:
        state = self._load_agent_state()
        state.update(
            {
                "startup_notification_status": str(status or "").strip(),
                "startup_notification_error": str(error or "").strip(),
                "startup_notification_channel": str(attempted_channel or "").strip(),
                "startup_notification_attempts": int(attempts or 0),
                "startup_notification_fallback_target": str(fallback_target or "").strip(),
                "startup_notification_updated_at": utc_now_iso(),
            }
        )
        write_json(self.paths.agent_state_file(self.role), state)

    def _record_listener_health_state(
        self,
        *,
        status: str,
        error: str,
        category: str,
        recovery_action: str,
        connected_bot_name: str = "",
        connected_bot_id: str = "",
    ) -> None:
        state = self._load_agent_state()
        state.update(
            {
                "listener_status": str(status or "").strip(),
                "listener_error": str(error or "").strip(),
                "listener_error_category": str(category or "").strip(),
                "listener_recovery_action": str(recovery_action or "").strip(),
                "listener_connected_bot_name": str(connected_bot_name or "").strip(),
                "listener_connected_bot_id": str(connected_bot_id or "").strip(),
                "listener_updated_at": utc_now_iso(),
            }
        )
        state.update(self._listener_state_metadata(connected_bot_id=connected_bot_id))
        if str(status or "").strip() == "connected":
            state["listener_connected_at"] = utc_now_iso()
        else:
            state["listener_last_failure_at"] = utc_now_iso()
        write_json(self.paths.agent_state_file(self.role), state)

    def _record_sourcer_report_state(
        self,
        *,
        status: str,
        client_label: str,
        reason: str,
        category: str,
        recovery_action: str,
        error: str,
        attempts: int,
        channel_id: str,
    ) -> None:
        normalized, state, reset_failure_suppression = build_sourcer_report_state_update_helper(
            agent_state=self._load_agent_state(),
            status=status,
            client_label=client_label,
            reason=reason,
            category=category,
            recovery_action=recovery_action,
            error=error,
            attempts=attempts,
            channel_id=channel_id,
            updated_at=utc_now_iso(),
        )
        if reset_failure_suppression:
            self._last_sourcer_report_failure_signature = ""
            self._last_sourcer_report_failure_logged_at = 0.0
        if isinstance(self._last_backlog_sourcing_activity, dict):
            self._last_backlog_sourcing_activity.update(normalized)
        write_json(self.paths.agent_state_file(self.role), state)

    def _version_controller_sources_dir(self) -> Path:
        return self.paths.internal_agent_root("version_controller") / "sources"

    def _write_version_control_payload_file(self, payload_name: str, payload: dict[str, Any]) -> tuple[str, str]:
        sources_dir = self._version_controller_sources_dir()
        sources_dir.mkdir(parents=True, exist_ok=True)
        payload_file = sources_dir / payload_name
        write_json(payload_file, payload)
        return str(payload_file), str(payload_file.relative_to(self.paths.internal_agent_root("version_controller")))

    @staticmethod
    def _clone_jsonish(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        return json.loads(json.dumps(payload, ensure_ascii=False))

    async def _invoke_version_controller(
        self,
        *,
        request_context: dict[str, Any],
        mode: str,
        scope: str,
        summary: str,
        payload_file: str,
        helper_command: str,
        artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        envelope = MessageEnvelope(
            request_id=str(request_context.get("request_id") or ""),
            sender="orchestrator",
            target="version_controller",
            intent="execute",
            urgency="normal",
            scope=scope,
            artifacts=[str(item).strip() for item in (artifacts or []) if str(item).strip()],
            params={
                "_teams_kind": "internal_version_control",
                "version_control_mode": mode,
                "payload_file": payload_file,
                "helper_command": helper_command,
            },
            body=(
                f"version_control_mode={mode}\n"
                f"helper_command={helper_command}\n"
                f"payload_file={payload_file}\n"
                f"summary={summary}"
            ),
        )
        return await asyncio.to_thread(self.version_controller_runtime.run_task, envelope, request_context)

    async def _run_task_version_controller(
        self,
        *,
        sprint_state: dict[str, Any],
        todo: dict[str, Any],
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = str(request_record.get("request_id") or todo.get("request_id") or "").strip() or "task"
        backlog_id = str(todo.get("backlog_id") or "").strip()
        task_commit_title = str(todo.get("title") or "").strip()
        if not task_commit_title and backlog_id:
            backlog_item = self._load_backlog_item(backlog_id)
            task_commit_title = str(backlog_item.get("title") or "").strip()
        task_commit_summary = str(
            request_record.get("task_commit_summary")
            or result.get("task_commit_summary")
            or result.get("summary")
            or todo.get("title")
            or request_record.get("scope")
            or ""
        ).strip()
        functional_commit_title = (
            task_commit_summary
            if _looks_meta_change_text(task_commit_title) and task_commit_summary
            else task_commit_title
        )
        payload = {
            "mode": "task",
            "project_root": str(self.paths.project_workspace_root),
            "baseline": dict(request_record.get("git_baseline") or {}),
            "sprint_id": str(sprint_state.get("sprint_id") or ""),
            "todo_id": str(todo.get("todo_id") or ""),
            "backlog_id": backlog_id,
            "title": task_commit_title,
            "functional_title": functional_commit_title,
            "summary": task_commit_summary,
        }
        _payload_abs, payload_rel = self._write_version_control_payload_file(
            f"{request_id}.task.version_control.json",
            payload,
        )
        helper_command = build_version_control_helper_command(payload_rel)
        request_context = self._clone_jsonish(request_record)
        request_context["version_control"] = {
            "mode": "task",
            "payload_file": payload_rel,
            "helper_command": helper_command,
            "scope": str(todo.get("title") or request_record.get("scope") or ""),
            "title": task_commit_title,
            "functional_title": functional_commit_title,
            "summary": task_commit_summary,
        }
        request_context["result"] = self._clone_jsonish(result)
        request_record["version_control_status"] = "running"
        request_record["task_commit_status"] = "running"
        append_request_event(
            request_record,
            event_type="version_control_requested",
            actor="orchestrator",
            summary="version_controller로 task 완료 커밋을 위임했습니다.",
            payload={"mode": "task", "payload_file": payload_rel},
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="version_control_requested",
            summary="version_controller로 task 완료 커밋을 위임했습니다.",
            result=result,
        )
        version_result = await self._invoke_version_controller(
            request_context=request_context,
            mode="task",
            scope=str(todo.get("title") or request_record.get("scope") or "task version control"),
            summary=str(result.get("summary") or ""),
            payload_file=payload_rel,
            helper_command=helper_command,
            artifacts=[payload_rel, *[str(item) for item in (result.get("artifacts") or []) if str(item).strip()]],
        )
        append_request_event(
            request_record,
            event_type="version_control_completed",
            actor="version_controller",
            summary=str(version_result.get("summary") or "version_controller 결과를 수신했습니다."),
            payload=version_result,
        )
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="version_control_completed",
            summary=str(version_result.get("summary") or "version_controller 결과를 수신했습니다."),
            result=version_result,
        )
        return version_result

    async def _run_closeout_version_controller(
        self,
        *,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> dict[str, Any]:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip() or "closeout"
        payload = {
            "mode": "closeout",
            "project_root": str(self.paths.project_workspace_root),
            "baseline": dict(sprint_state.get("git_baseline") or {}),
            "sprint_id": sprint_id,
            "commit_message": build_sprint_commit_message(sprint_id),
        }
        _payload_abs, payload_rel = self._write_version_control_payload_file(
            f"{sprint_id}.closeout.version_control.json",
            payload,
        )
        helper_command = build_version_control_helper_command(payload_rel)
        request_context = {
            "request_id": f"{sprint_id}:closeout",
            "status": "queued",
            "current_role": "orchestrator",
            "next_role": "",
            "owner_role": "orchestrator",
            "scope": f"{sprint_id} sprint closeout",
            "body": str(closeout_result.get("message") or "").strip(),
            "artifacts": [],
            "events": [],
            "result": {
                "role": "orchestrator",
                "status": "completed",
                "summary": str(closeout_result.get("message") or "스프린트 closeout commit이 필요합니다.").strip(),
                "artifacts": [
                    str(item).strip()
                    for item in (closeout_result.get("uncommitted_paths") or [])
                    if str(item).strip()
                ],
            },
            "version_control": {
                "mode": "closeout",
                "payload_file": payload_rel,
                "helper_command": helper_command,
                "scope": f"{sprint_id} sprint closeout",
                "summary": str(closeout_result.get("message") or "").strip(),
            },
            "sprint_id": sprint_id,
        }
        return await self._invoke_version_controller(
            request_context=request_context,
            mode="closeout",
            scope=f"{sprint_id} sprint closeout",
            summary=str(closeout_result.get("message") or ""),
            payload_file=payload_rel,
            helper_command=helper_command,
            artifacts=[payload_rel],
        )

    def _local_runtime_session_identity(self, role: str) -> str:
        return local_runtime_identity(self.role, role)

    def _build_role_runtime(
        self,
        role: str,
        sprint_id: str,
        *,
        session_identity: str | None = None,
    ) -> RoleAgentRuntime:
        if role == "research":
            return ResearchAgentRuntime(
                paths=self.paths,
                role=role,
                sprint_id=sprint_id,
                runtime_config=self.runtime_config.role_defaults[role],
                research_defaults=self.runtime_config.research_defaults,
                session_identity=session_identity,
            )
        return RoleAgentRuntime(
            paths=self.paths,
            role=role,
            sprint_id=sprint_id,
            runtime_config=self.runtime_config.role_defaults[role],
            session_identity=session_identity,
        )

    def _runtime_for_role(self, role: str, sprint_id: str) -> RoleAgentRuntime:
        if role == self.role:
            return self.role_runtime
        session_identity = self._local_runtime_session_identity(role)
        key = (role, sprint_id, session_identity)
        cached = self._role_runtime_cache.get(key)
        if cached is not None:
            return cached
        runtime = self._build_role_runtime(
            role,
            sprint_id,
            session_identity=session_identity,
        )
        self._role_runtime_cache[key] = runtime
        return runtime

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    @staticmethod
    def _build_backlog_fingerprint(*, title: str, scope: str, kind: str) -> str:
        return build_backlog_fingerprint(title=title, scope=scope, kind=kind)

    @staticmethod
    def _build_sourcer_candidate_trace_fingerprint(candidate: dict[str, Any]) -> str:
        return build_sourcer_candidate_trace_fingerprint(candidate)

    def _load_scheduler_state(self) -> dict[str, Any]:
        state = read_json(self.paths.sprint_scheduler_file)
        return {
            "active_sprint_id": str(state.get("active_sprint_id") or "").strip(),
            "last_started_at": str(state.get("last_started_at") or "").strip(),
            "last_completed_at": str(state.get("last_completed_at") or "").strip(),
            "last_skipped_at": str(state.get("last_skipped_at") or "").strip(),
            "last_skip_reason": str(state.get("last_skip_reason") or "").strip(),
            "last_sourced_at": str(state.get("last_sourced_at") or "").strip(),
            "last_sourcing_status": str(state.get("last_sourcing_status") or "").strip(),
            "last_sourcing_request_id": str(state.get("last_sourcing_request_id") or "").strip(),
            "last_sourcing_fingerprint": str(state.get("last_sourcing_fingerprint") or "").strip(),
            "last_sourcing_review_status": str(state.get("last_sourcing_review_status") or "").strip(),
            "last_sourcing_review_request_id": str(state.get("last_sourcing_review_request_id") or "").strip(),
            "next_slot_at": str(state.get("next_slot_at") or "").strip(),
            "deferred_slot_at": str(state.get("deferred_slot_at") or "").strip(),
            "last_trigger": str(state.get("last_trigger") or "").strip(),
            "last_blocked_review_at": str(state.get("last_blocked_review_at") or "").strip(),
            "last_blocked_review_request_id": str(state.get("last_blocked_review_request_id") or "").strip(),
            "last_blocked_review_status": str(state.get("last_blocked_review_status") or "").strip(),
            "last_blocked_review_fingerprint": str(state.get("last_blocked_review_fingerprint") or "").strip(),
            "milestone_request_pending": bool(state.get("milestone_request_pending")),
            "milestone_request_sent_at": str(state.get("milestone_request_sent_at") or "").strip(),
            "milestone_request_channel_id": str(state.get("milestone_request_channel_id") or "").strip(),
            "milestone_request_reason": str(state.get("milestone_request_reason") or "").strip(),
        }

    def _save_scheduler_state(self, state: dict[str, Any]) -> None:
        write_json(self.paths.sprint_scheduler_file, state)

    @staticmethod
    def _clear_pending_milestone_request(state: dict[str, Any]) -> None:
        state["milestone_request_pending"] = False
        state["milestone_request_sent_at"] = ""
        state["milestone_request_channel_id"] = ""
        state["milestone_request_reason"] = ""

    @staticmethod
    def _clear_blocked_backlog_review_state(state: dict[str, Any]) -> None:
        state["last_blocked_review_at"] = ""
        state["last_blocked_review_request_id"] = ""
        state["last_blocked_review_status"] = ""
        state["last_blocked_review_fingerprint"] = ""

    @staticmethod
    def _build_idle_sprint_milestone_request_message() -> str:
        return (
            "현재 active sprint가 없습니다. 새 sprint milestone을 알려주세요.\n"
            "예: `start sprint\\nmilestone: sprint workflow initial phase 개선`"
        )

    async def _maybe_request_idle_sprint_milestone(self, *, reason: str) -> bool:
        state = self._load_scheduler_state()
        if str(state.get("active_sprint_id") or "").strip():
            return False
        if bool(state.get("milestone_request_pending")):
            return False
        relay_channel_id = str(self.discord_config.relay_channel_id or "").strip()
        if not relay_channel_id:
            return False
        try:
            await self._send_discord_content(
                content=self._build_idle_sprint_milestone_request_message(),
                send=lambda chunk: self.discord_client.send_channel_message(relay_channel_id, chunk),
                target_description=f"idle-sprint-milestone:{relay_channel_id}",
                swallow_exceptions=False,
            )
        except Exception as exc:
            LOGGER.warning("Failed to send idle sprint milestone request via relay:%s: %s", relay_channel_id, exc)
            return False
        state["milestone_request_pending"] = True
        state["milestone_request_sent_at"] = utc_now_iso()
        state["milestone_request_channel_id"] = relay_channel_id
        state["milestone_request_reason"] = str(reason or "").strip()
        self._save_scheduler_state(state)
        return True

    def _build_sourcer_existing_backlog_context(self) -> list[dict[str, Any]]:
        return build_sourcer_existing_backlog_context_helper(self)

    def _collect_backlog_linked_request_ids(self) -> set[str]:
        return collect_backlog_linked_request_ids_helper(self)

    def _build_backlog_sourcing_findings(self) -> list[dict[str, Any]]:
        return build_backlog_sourcing_findings_helper(self)

    def _fallback_backlog_candidates_from_findings(self, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return fallback_backlog_candidates_from_findings(findings)

    def _iter_backlog_items(self) -> list[dict[str, Any]]:
        return iter_backlog_items(self.paths)

    def _is_non_actionable_backlog_item(self, item: dict[str, Any]) -> bool:
        return is_non_actionable_backlog_item(item, request_loader=self._load_request)

    @staticmethod
    def _is_active_backlog_status(status: str) -> bool:
        return is_active_backlog_status(status)

    @staticmethod
    def _is_actionable_backlog_status(status: str) -> bool:
        return is_actionable_backlog_status(status)

    @staticmethod
    def _is_reusable_backlog_status(status: str) -> bool:
        return is_reusable_backlog_status(status)

    @staticmethod
    def _clear_backlog_blockers(item: dict[str, Any]) -> None:
        clear_backlog_blockers(item)

    @staticmethod
    def _desired_backlog_status_for_todo(todo: dict[str, Any] | None) -> str:
        return desired_backlog_status_for_todo(todo)

    @staticmethod
    def _todo_status_rank(status: str) -> int:
        return todo_status_rank_helper(status)

    @classmethod
    def _sort_sprint_todos(cls, todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sort_sprint_todos_helper(todos)

    def _iter_sprint_task_request_records(self, sprint_id: str) -> list[dict[str, Any]]:
        return iter_sprint_task_request_records_helper(self.paths, sprint_id)

    @staticmethod
    def _todo_status_from_request_record(request_record: dict[str, Any]) -> str:
        return todo_status_from_request_record_helper(request_record)

    def _build_recovered_sprint_todo_from_request(
        self,
        sprint_state: dict[str, Any],
        request_record: dict[str, Any],
    ) -> dict[str, Any]:
        return build_recovered_sprint_todo_from_request_for_service_helper(self, sprint_state, request_record)

    def _merge_recovered_sprint_todo(self, existing: dict[str, Any], recovered: dict[str, Any]) -> dict[str, Any]:
        return merge_recovered_sprint_todo_helper(existing, recovered)

    def _recover_sprint_todos_from_requests(self, sprint_state: dict[str, Any]) -> bool:
        return recover_sprint_todos_from_requests_helper(self, sprint_state)

    @staticmethod
    def _parse_sprint_report_fields(report_body: str) -> dict[str, str]:
        return parse_sprint_report_fields_helper(report_body)

    @staticmethod
    def _parse_sprint_report_list_field(value: str) -> list[str]:
        return parse_sprint_report_list_field_helper(value)

    @staticmethod
    def _parse_sprint_report_int_field(value: str) -> int:
        return parse_sprint_report_int_field_helper(value)

    def _derived_closeout_result_from_sprint_state(self, sprint_state: dict[str, Any]) -> dict[str, Any]:
        return build_derived_closeout_result_from_sprint_state_helper(sprint_state)

    def _refresh_sprint_report_body(self, sprint_state: dict[str, Any]) -> bool:
        return refresh_sprint_report_body_helper(
            sprint_state,
            build_report_body=self._build_sprint_report_body,
        )

    def _apply_backlog_state_from_todo(
        self,
        backlog_item: dict[str, Any],
        *,
        todo: dict[str, Any] | None,
        sprint_id: str,
    ) -> bool:
        return apply_backlog_state_from_todo(backlog_item, todo=todo, sprint_id=sprint_id)

    def _synchronize_sprint_todo_backlog_state(self, sprint_state: dict[str, Any], *, persist_backlog: bool = True) -> bool:
        return synchronize_sprint_todo_backlog_state_helper(
            self,
            sprint_state,
            persist_backlog=persist_backlog,
        )

    def _repair_non_actionable_carry_over_backlog_items(self) -> set[str]:
        return repair_non_actionable_carry_over_backlog_items(self.paths)

    def _drop_non_actionable_backlog_items(self) -> set[str]:
        return drop_non_actionable_backlog_items(self.paths, request_loader=self._load_request)

    def _load_backlog_item(self, backlog_id: str) -> dict[str, Any]:
        return load_backlog_item(self.paths, backlog_id)

    def _save_backlog_item(self, item: dict[str, Any]) -> None:
        save_backlog_item(self.paths, item)

    def _refresh_backlog_markdown(self) -> None:
        self._drop_non_actionable_backlog_items()
        refresh_backlog_markdown(self.paths)

    def _load_sprint_state(self, sprint_id: str) -> dict[str, Any]:
        return load_sprint_state_with_sync_helper(self, sprint_id)

    def _save_sprint_state(self, sprint_state: dict[str, Any]) -> None:
        save_sprint_state_with_sync_helper(self, sprint_state)

    def _sprint_artifact_paths(self, sprint_state: dict[str, Any]) -> dict[str, Path]:
        return sprint_artifact_paths_helper(self.paths, sprint_state)

    @staticmethod
    def _should_start_sprint_research_prepass(
        sprint_state: dict[str, Any],
        *,
        phase: str,
        iteration: int,
        step: str,
    ) -> bool:
        return should_start_sprint_research_prepass_helper(
            sprint_state,
            phase=phase,
            iteration=iteration,
            step=step,
        )

    @staticmethod
    def _sprint_research_prepass_artifacts(sprint_state: dict[str, Any]) -> list[str]:
        return sprint_research_prepass_artifacts_helper(sprint_state)

    def _merge_persisted_sprint_research_prepass(self, sprint_state: dict[str, Any]) -> bool:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if not sprint_id:
            return False
        persisted = self._load_sprint_state(sprint_id)
        prepass = dict(persisted.get("research_prepass") or {}) if persisted else {}
        if not prepass:
            return False
        changed = False
        if dict(sprint_state.get("research_prepass") or {}) != prepass:
            sprint_state["research_prepass"] = prepass
            changed = True
        merged_references = _dedupe_preserving_order(
            [
                *[str(item).strip() for item in (sprint_state.get("reference_artifacts") or []) if str(item).strip()],
                *[str(item).strip() for item in (persisted.get("reference_artifacts") or []) if str(item).strip()],
                *self._sprint_research_prepass_artifacts({"research_prepass": prepass}),
            ]
        )
        if merged_references != [str(item).strip() for item in (sprint_state.get("reference_artifacts") or []) if str(item).strip()]:
            sprint_state["reference_artifacts"] = merged_references
            changed = True
        return changed

    def _render_sprint_kickoff_markdown(self, sprint_state: dict[str, Any]) -> str:
        source_request_id = str(sprint_state.get("kickoff_source_request_id") or "").strip()
        source_request_path = (
            str(self.paths.request_file(source_request_id).relative_to(self.paths.workspace_root))
            if source_request_id
            else "N/A"
        )
        return render_sprint_kickoff_markdown_helper(sprint_state, source_request_path=source_request_path)

    def _render_sprint_milestone_markdown(self, sprint_state: dict[str, Any]) -> str:
        return render_sprint_milestone_markdown_helper(sprint_state)

    def _render_sprint_plan_markdown(self, sprint_state: dict[str, Any]) -> str:
        return render_sprint_plan_markdown_helper(sprint_state)

    def _collect_sprint_request_entries(self, sprint_state: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen_request_ids: set[str] = set()
        for todo in list(sprint_state.get("todos") or []):
            request_id = str(todo.get("request_id") or "").strip()
            if not request_id or request_id in seen_request_ids:
                continue
            seen_request_ids.add(request_id)
            request_record = self._load_request(request_id)
            if not request_record:
                continue
            entries.append({"todo": dict(todo), "request": request_record})
        return entries

    def _render_sprint_spec_markdown(self, sprint_state: dict[str, Any]) -> str:
        return render_sprint_spec_markdown_helper(
            sprint_state,
            request_entries=self._collect_sprint_request_entries(sprint_state),
            workflow_transition_provider=self._workflow_transition,
        )

    def _render_sprint_todo_backlog_markdown(self, sprint_state: dict[str, Any]) -> str:
        return render_sprint_todo_backlog_markdown_helper(sprint_state)

    def _render_sprint_iteration_log_markdown(self, sprint_state: dict[str, Any]) -> str:
        return render_sprint_iteration_log_markdown_helper(
            sprint_state,
            request_entries=self._collect_sprint_request_entries(sprint_state),
            workflow_transition_provider=self._workflow_transition,
        )

    def _inspect_sprint_documentation_closeout(self, sprint_state: dict[str, Any]) -> dict[str, Any]:
        if not str(sprint_state.get("sprint_folder_name") or "").strip():
            return {"status": "verified", "message": "sprint artifact folder가 없어 문서 closeout 검증을 생략했습니다."}
        paths = self._sprint_artifact_paths(sprint_state)
        missing_sections: list[str] = []
        spec_text = paths["spec"].read_text(encoding="utf-8") if paths["spec"].exists() else ""
        iteration_text = paths["iteration_log"].read_text(encoding="utf-8") if paths["iteration_log"].exists() else ""
        if "## Canonical Contract Body" not in spec_text:
            missing_sections.append("spec.canonical_contract_body")
        if "## Workflow Validation Trace" not in iteration_text:
            missing_sections.append("iteration_log.workflow_validation_trace")
        if missing_sections:
            return {
                "status": "planning_incomplete",
                "message": "shared spec/iteration 문서가 canonical 계약 본문과 workflow 검증 추적을 아직 닫지 못했습니다.",
                "missing_sections": missing_sections,
            }
        return {"status": "verified", "message": "shared spec/iteration 문서 closeout 검증을 통과했습니다."}

    def _write_sprint_artifact_files(self, sprint_state: dict[str, Any]) -> None:
        paths = self._sprint_artifact_paths(sprint_state)
        paths["root"].mkdir(parents=True, exist_ok=True)
        paths["index"].write_text(render_sprint_artifact_index_markdown(sprint_state), encoding="utf-8")
        paths["kickoff"].write_text(self._render_sprint_kickoff_markdown(sprint_state), encoding="utf-8")
        paths["milestone"].write_text(self._render_sprint_milestone_markdown(sprint_state), encoding="utf-8")
        paths["plan"].write_text(self._render_sprint_plan_markdown(sprint_state), encoding="utf-8")
        paths["spec"].write_text(self._render_sprint_spec_markdown(sprint_state), encoding="utf-8")
        paths["todo_backlog"].write_text(self._render_sprint_todo_backlog_markdown(sprint_state), encoding="utf-8")
        paths["iteration_log"].write_text(self._render_sprint_iteration_log_markdown(sprint_state), encoding="utf-8")
        report_body = str(sprint_state.get("report_body") or "").strip() or self._render_live_sprint_report_markdown(
            sprint_state
        )
        paths["report"].write_text(report_body.rstrip() + "\n", encoding="utf-8")

    def _required_sprint_preflight_artifact_paths(self, sprint_state: dict[str, Any]) -> list[Path]:
        paths = self._sprint_artifact_paths(sprint_state)
        required = [self.paths.current_sprint_file]
        required.extend(paths[key] for key in SPRINT_SPEC_TODO_REPORT_DOC_KEYS)
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in required:
            normalized = str(path.resolve())
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(path)
        return deduped

    def _missing_sprint_preflight_artifacts(self, sprint_state: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for path in self._required_sprint_preflight_artifact_paths(sprint_state):
            if not path.exists() or not path.read_text(encoding="utf-8").strip():
                missing.append(self._workspace_artifact_hint(path))
        return missing

    @staticmethod
    def _report_section(title: str, lines: Iterable[str] | None) -> ReportSection:
        return report_section_helper(title, list(lines or []))

    @staticmethod
    def _split_report_body_lines(body: str) -> list[str]:
        return split_report_body_lines_helper(body)

    @staticmethod
    def _format_priority_value(value: Any) -> str:
        return format_priority_value_helper(value)

    def _format_backlog_report_line(self, item: dict[str, Any]) -> str:
        return format_backlog_report_line_helper(item)

    def _format_todo_report_line(self, todo: dict[str, Any], *, include_artifacts: bool = False) -> str:
        return format_todo_report_line_helper(todo, include_artifacts=include_artifacts)

    def _build_generic_sprint_report_sections(self, body: str) -> list[ReportSection]:
        return build_generic_sprint_report_sections_helper(body)

    def _build_sprint_kickoff_report_sections(self, sprint_state: dict[str, Any]) -> list[ReportSection]:
        selected_lines = self._build_sprint_kickoff_preview_lines(
            sprint_state,
            limit=max(1, len(sprint_state.get("todos") or []) or len(sprint_state.get("selected_items") or []) or 3),
        )
        return build_sprint_kickoff_report_sections_helper(sprint_state, selected_lines=selected_lines)

    def _build_sprint_todo_list_report_sections(self, sprint_state: dict[str, Any]) -> list[ReportSection]:
        return build_sprint_todo_list_report_sections_helper(sprint_state)

    def _build_sprint_spec_todo_report_sections(self, sprint_state: dict[str, Any]) -> list[ReportSection]:
        backlog_items = self._collect_sprint_relevant_backlog_items(sprint_state)
        artifact_paths = self._required_sprint_preflight_artifact_paths(sprint_state)
        fallback_todo_lines = self._build_sprint_kickoff_preview_lines(
            sprint_state,
            limit=max(1, len(sprint_state.get("selected_items") or []) or 3),
        )
        return build_sprint_spec_todo_report_sections_helper(
            sprint_state,
            backlog_items=backlog_items,
            artifact_hints=[self._workspace_artifact_hint(path) for path in artifact_paths],
            fallback_todo_lines=fallback_todo_lines,
        )

    def _build_terminal_sprint_report_sections(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> list[ReportSection]:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        return build_terminal_sprint_report_sections_helper(
            sprint_state,
            snapshot,
            build_overview_lines=self._build_sprint_overview_lines,
            build_change_summary_lines=self._build_sprint_change_summary_lines,
            build_planned_todo_lines=self._build_sprint_planned_todo_lines,
            build_commit_lines=self._build_sprint_commit_lines,
            build_followup_lines=self._build_sprint_followup_lines,
            build_timeline_lines=self._build_sprint_timeline_lines,
            build_agent_contribution_lines=self._build_sprint_agent_contribution_lines,
            build_issue_lines=self._build_sprint_issue_lines,
            build_achievement_lines=self._build_sprint_achievement_lines,
            build_artifact_lines=self._build_sprint_artifact_lines,
        )

    def _build_sprint_spec_todo_report_body(self, sprint_state: dict[str, Any]) -> str:
        return build_sprint_spec_todo_report_body_helper(
            sprint_state,
            todo_lines=self._build_sprint_kickoff_preview_lines(sprint_state, limit=10),
        )

    async def _send_sprint_spec_todo_report(
        self,
        sprint_state: dict[str, Any],
        *,
        title: str = "📐 스프린트 Spec/TODO",
        judgment: str = "implementation 시작 전 spec/todo canonical 보고를 남겼습니다.",
        next_action: str = "implementation 진행",
        swallow_exceptions: bool = False,
    ) -> None:
        artifact_paths = self._required_sprint_preflight_artifact_paths(sprint_state)
        await self._send_sprint_report(
            title=title,
            body=self._build_sprint_spec_todo_report_body(sprint_state),
            sprint_id=str(sprint_state.get("sprint_id") or ""),
            judgment=judgment,
            next_action=next_action,
            related_artifacts=[self._workspace_artifact_hint(path) for path in artifact_paths],
            log_summary=self._build_sprint_spec_todo_report_body(sprint_state),
            sections=self._build_sprint_spec_todo_report_sections(sprint_state),
            swallow_exceptions=swallow_exceptions,
        )

    def _append_sprint_event(self, sprint_id: str, *, event_type: str, summary: str, payload: dict[str, Any] | None = None) -> None:
        append_sprint_event(self.paths, sprint_id, event_type=event_type, summary=summary, payload=payload)

    def _archive_sprint_history(self, sprint_state: dict[str, Any], report_body: str) -> str:
        return archive_sprint_history_helper(self.paths, sprint_state, report_body)

    def _should_refresh_sprint_history_archive(self, sprint_state: dict[str, Any]) -> bool:
        return should_refresh_sprint_history_archive_helper(sprint_state)

    def _refresh_sprint_history_archive(self, sprint_state: dict[str, Any]) -> bool:
        return refresh_sprint_history_archive_helper(self.paths, sprint_state)

    def _classify_backlog_kind(self, intent: str, scope: str, summary: str = "") -> str:
        return classify_backlog_kind(intent, scope, summary)

    @staticmethod
    def _normalize_backlog_acceptance_criteria(values: Any) -> list[str]:
        return normalize_backlog_acceptance_criteria(values)

    def _select_backlog_items_for_sprint(self) -> list[dict[str, Any]]:
        return select_backlog_items_for_sprint_helper(self)

    def _perform_backlog_sourcing(self) -> tuple[int, int, list[dict[str, Any]]]:
        return perform_backlog_sourcing_helper(self)

    def _prepare_actionable_backlog_for_sprint(self) -> list[dict[str, Any]]:
        return self._select_backlog_items_for_sprint()

    async def _maybe_queue_blocked_backlog_review_for_autonomous_start(
        self,
        state: dict[str, Any],
    ) -> bool:
        return await maybe_queue_blocked_backlog_review_for_autonomous_start_helper(self, state)

    def _backlog_sourcing_interval_seconds(self) -> float:
        return backlog_sourcing_interval_seconds_helper(self, minimum_interval_seconds=BACKLOG_SOURCING_POLL_SECONDS)

    async def _backlog_sourcing_loop(self) -> None:
        await backlog_sourcing_loop_helper(self, poll_seconds=BACKLOG_SOURCING_POLL_SECONDS)

    async def _poll_backlog_sourcing_once(self) -> None:
        await poll_backlog_sourcing_once_helper(self)

    def _discover_backlog_candidates(self) -> list[dict[str, Any]]:
        return discover_backlog_candidates_helper(self)

    async def _scheduler_loop(self) -> None:
        await scheduler_loop_helper(self, poll_seconds=SCHEDULER_POLL_SECONDS)

    async def _poll_scheduler_once(self) -> None:
        await poll_scheduler_once_helper(self)

    def _build_active_sprint_id(self) -> str:
        return build_active_sprint_id()

    def _capture_git_baseline(self) -> dict[str, Any]:
        return capture_git_baseline(self.paths.project_workspace_root)

    def _inspect_git_sprint_closeout(self, baseline: dict[str, Any], sprint_id: str) -> dict[str, Any]:
        return inspect_sprint_closeout(self.paths.project_workspace_root, baseline, sprint_id)

    async def _run_autonomous_sprint(self, trigger: str, *, selected_items: list[dict[str, Any]] | None = None) -> None:
        await run_autonomous_sprint_helper(self, trigger, selected_items=selected_items)
    def _finish_scheduler_after_sprint(self, sprint_state: dict[str, Any], *, clear_active: bool | None = None) -> None:
        finish_scheduler_after_sprint_helper(self, sprint_state, clear_active=clear_active)
    @staticmethod
    def _is_resumable_blocked_sprint(sprint_state: dict[str, Any]) -> bool:
        return is_resumable_blocked_sprint_helper(sprint_state)
    @staticmethod
    def _is_wrap_up_requested(sprint_state: dict[str, Any]) -> bool:
        return is_wrap_up_requested_helper(sprint_state)
    @staticmethod
    def _is_executable_todo_status(status: str) -> bool:
        return is_executable_todo_status_helper(status)
    def _select_restart_checkpoint_todo(
        self,
        sprint_state: dict[str, Any],
    ) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
        return select_restart_checkpoint_todo_helper(self, sprint_state)
    def _mark_restart_checkpoint_backlog_selected(
        self,
        sprint_state: dict[str, Any],
        *,
        backlog_id: str,
    ) -> None:
        mark_restart_checkpoint_backlog_selected_helper(self, sprint_state, backlog_id=backlog_id)
    def _prepare_requested_restart_checkpoint(self, sprint_state: dict[str, Any]) -> bool:
        return prepare_requested_restart_checkpoint_helper(self, sprint_state)
    async def _resume_active_sprint(self, sprint_id: str) -> None:
        await resume_active_sprint_helper(self, sprint_id)
    def _prune_dropped_backlog_from_sprint(self, sprint_state: dict[str, Any], dropped_ids: set[str]) -> bool:
        if not dropped_ids:
            return False
        existing_todos = list(sprint_state.get("todos") or [])
        kept_todos: list[dict[str, Any]] = []
        pruned_count = 0
        for todo in existing_todos:
            backlog_id = str(todo.get("backlog_id") or "").strip()
            status = str(todo.get("status") or "").strip().lower()
            if backlog_id in dropped_ids and status == "queued":
                pruned_count += 1
                continue
            kept_todos.append(todo)
        if pruned_count == 0:
            return False
        kept_ids = {str(todo.get("backlog_id") or "").strip() for todo in kept_todos}
        sprint_state["todos"] = kept_todos
        sprint_state["selected_backlog_ids"] = [backlog_id for backlog_id in sprint_state.get("selected_backlog_ids") or [] if str(backlog_id or "").strip() in kept_ids]
        sprint_state["selected_items"] = [
            item
            for item in sprint_state.get("selected_items") or []
            if str(item.get("backlog_id") or "").strip() in kept_ids
        ]
        self._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="backlog_pruned",
            summary="실행 불가 backlog 항목을 active sprint에서 제거했습니다.",
            payload={"removed_count": pruned_count},
        )
        return True

    async def _claim_request(self, request_id: str) -> bool:
        normalized = str(request_id or "").strip()
        if not normalized:
            return False
        async with self._active_request_ids_lock:
            if normalized in self._active_request_ids:
                return False
            self._active_request_ids.add(normalized)
            return True

    async def _release_request(self, request_id: str) -> None:
        normalized = str(request_id or "").strip()
        if not normalized:
            return
        async with self._active_request_ids_lock:
            self._active_request_ids.discard(normalized)

    async def _resume_pending_role_requests(self) -> None:
        if self.role == "orchestrator":
            return
        async with self._role_resume_lock:
            pending_records = [
                record
                for record in iter_request_records(self.paths)
                if str(record.get("status") or "").strip().lower() == "delegated"
                and str(record.get("current_role") or "").strip() == self.role
            ]
            pending_records.sort(key=lambda record: str(record.get("updated_at") or ""))
            for request_record in pending_records:
                await self._resume_pending_delegated_request(request_record)

    async def _resume_pending_role_requests_loop(self) -> None:
        while True:
            try:
                await self._resume_pending_role_requests()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Pending delegated request resume loop failed for role %s", self.role)
            await asyncio.sleep(ROLE_REQUEST_RESUME_POLL_SECONDS)

    async def _resume_pending_delegated_request(self, request_record: dict[str, Any]) -> None:
        request_id = str(request_record.get("request_id") or "").strip()
        if not request_id or not await self._claim_request(request_id):
            return
        try:
            result = dict(request_record.get("result") or {}) if isinstance(request_record.get("result"), dict) else {}
            if (
                str(result.get("request_id") or "").strip() == request_id
                and str(result.get("role") or "").strip() == self.role
            ):
                result_envelope = MessageEnvelope(
                    request_id=request_id,
                    sender=self.role,
                    target="orchestrator",
                    intent="report",
                    urgency=str(request_record.get("urgency") or "normal"),
                    scope=str(request_record.get("scope") or ""),
                    artifacts=[str(item) for item in result.get("artifacts") or []],
                    params={
                        "_teams_kind": "report",
                        "result": result,
                        "_resumed_after_reconnect": True,
                    },
                    body=json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
                )
                await self._send_relay(result_envelope, request_record=request_record)
                return
            envelope = self._build_delegate_envelope(
                request_record,
                self.role,
                extra_params={
                    **dict(request_record.get("params") or {}),
                    "_resumed_after_reconnect": True,
                },
            )
            await self._process_delegated_request(envelope, request_record)
        finally:
            await self._release_request(request_id)

    def _workspace_artifact_hint(self, path: Path) -> str:
        return workspace_artifact_hint_helper(self.paths, path)

    @staticmethod
    def _normalize_artifact_hint(value: Any) -> str:
        return normalize_artifact_hint(value)

    @classmethod
    def _is_planning_surface_artifact_hint(cls, artifact_hint: Any) -> bool:
        return is_planning_surface_artifact_hint(artifact_hint)

    @classmethod
    def _is_planner_owned_surface_artifact_hint(cls, artifact_hint: Any) -> bool:
        return is_planner_owned_surface_artifact_hint(artifact_hint)

    def _normalize_sprint_todo_artifacts(
        self,
        *artifact_groups: Any,
        workflow_state: dict[str, Any] | None = None,
    ) -> list[str]:
        candidates = self._collect_artifact_candidates(*artifact_groups)
        phase = str((workflow_state or {}).get("phase") or "").strip().lower()
        if phase in {WORKFLOW_PHASE_IMPLEMENTATION, WORKFLOW_PHASE_VALIDATION}:
            return [
                artifact
                for artifact in candidates
                if not self._is_planner_owned_surface_artifact_hint(artifact)
            ]
        return candidates

    def _required_workflow_planner_doc_hints(self, request_record: dict[str, Any]) -> list[str]:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return []
        sprint_artifact_hints: list[str] = []
        if str(workflow_state.get("reopen_source_role") or "").strip().lower() == "qa":
            sprint_id = str(request_record.get("sprint_id") or "").strip()
            sprint_state = self._load_sprint_state(sprint_id) if sprint_id else {}
            if sprint_state:
                artifact_paths = self._sprint_artifact_paths(sprint_state)
                for key in ("spec", "todo_backlog", "iteration_log"):
                    sprint_artifact_hints.append(self._workspace_artifact_hint(artifact_paths[key]))
                sprint_artifact_hints.append(self._workspace_artifact_hint(self.paths.current_sprint_file))
        return required_workflow_planner_doc_hints(
            reopen_source_role=str(workflow_state.get("reopen_source_role") or ""),
            request_artifacts=list(request_record.get("artifacts") or []),
            sprint_artifact_hints=sprint_artifact_hints,
        )

    def _workflow_planner_doc_contract_violation(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> tuple[list[str], list[str], list[str]]:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return ([], [], [])
        role = str(result.get("role") or request_record.get("current_role") or "").strip().lower()
        return workflow_planner_doc_contract_violation(
            workflow_state=workflow_state,
            role=role,
            result_artifacts=list(result.get("artifacts") or []),
            required_hints=self._required_workflow_planner_doc_hints(request_record),
            artifact_exists=lambda artifact: self._resolve_artifact_path(artifact) is not None,
        )

    def _qa_result_requires_planner_reopen(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> bool:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return False
        role = str(result.get("role") or request_record.get("current_role") or "").strip().lower()
        transition = self._workflow_transition(result)
        return qa_result_requires_planner_reopen(
            workflow_state=workflow_state,
            role=role,
            result=result,
            transition=transition,
        )

    def _qa_result_is_runtime_sync_anomaly(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> bool:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return False
        role = str(result.get("role") or request_record.get("current_role") or "").strip().lower()
        transition = self._workflow_transition(result)
        return qa_result_is_runtime_sync_anomaly(
            workflow_state=workflow_state,
            role=role,
            result=result,
            transition=transition,
        )

    @staticmethod
    def _normalize_backlog_file_candidates(values: Any) -> list[Any]:
        return normalize_backlog_file_candidates_helper(values)

    def _collect_backlog_candidates_from_payload(self, payload: Any) -> list[Any]:
        return collect_backlog_candidates_from_payload_helper(payload)

    @staticmethod
    def _planner_backlog_write_receipts(proposals: dict[str, Any]) -> list[dict[str, Any]]:
        return planner_backlog_write_receipts_helper(proposals)

    def _resolve_artifact_path(self, artifact_hint: str) -> Path | None:
        return resolve_artifact_path_helper(self.paths, artifact_hint)

    def _backlog_artifact_candidate_paths(self, request_record: dict[str, Any], result: dict[str, Any]) -> list[str]:
        return backlog_artifact_candidate_paths_helper(request_record, result)

    @staticmethod
    def _collect_artifact_candidates(*sequences: Iterable[Any]) -> list[str]:
        return collect_artifact_candidates_helper(*sequences)

    def _load_backlog_candidates_from_artifact(self, artifact_path: str) -> list[Any]:
        return load_backlog_candidates_from_artifact_helper(self.paths, artifact_path)

    def _sync_planner_backlog_from_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "proposal_items": 0,
            "receipt_items": 0,
            "artifact_items": 0,
            "merged_items": 0,
            "persisted_backlog_items": 0,
            "missing_backlog_artifacts": [],
            "missing_backlog_receipts": [],
            "planner_persisted_backlog": False,
            "verified_backlog_items": 0,
        }

        params = dict(request_record.get("params") or {})
        request_kind = str(params.get("_teams_kind") or "").strip()
        sprint_id = str(params.get("sprint_id") or request_record.get("sprint_id") or "").strip()
        if not sprint_id and request_kind not in {"sourcer_review", "blocked_backlog_review"}:
            return summary

        proposal_candidates = self._collect_backlog_candidates_from_payload(result.get("proposals") or {})
        proposal_items = [item for item in proposal_candidates if isinstance(item, (dict, str))][:120]
        summary["proposal_items"] = len(proposal_items)
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        receipts = self._planner_backlog_write_receipts(proposals)
        summary["receipt_items"] = len(receipts)
        if proposal_items and not receipts:
            summary["missing_backlog_receipts"].append("planner backlog_writes receipt missing")

        verified_backlog_ids: set[str] = set()
        artifact_candidates = self._backlog_artifact_candidate_paths(request_record, result)
        checked_artifact_paths: set[str] = set()
        for receipt in receipts:
            backlog_id = str(receipt.get("backlog_id") or "").strip()
            artifact_path = str(receipt.get("artifact_path") or "").strip()
            verified = False

            if backlog_id:
                persisted_item = self._load_backlog_item(backlog_id)
                if persisted_item:
                    verified_backlog_ids.add(backlog_id)
                    verified = True

            if not verified and artifact_path:
                checked_artifact_paths.add(artifact_path)
                resolved = self._resolve_artifact_path(artifact_path)
                if resolved is None:
                    summary["missing_backlog_artifacts"].append(artifact_path)
                    continue
                backlog_candidates = self._load_backlog_candidates_from_artifact(artifact_path)
                if not backlog_candidates:
                    summary["missing_backlog_artifacts"].append(artifact_path)
                    continue
                summary["artifact_items"] += len(backlog_candidates)
                for item in backlog_candidates:
                    item_backlog_id = str(item.get("backlog_id") or "").strip()
                    if item_backlog_id:
                        verified_backlog_ids.add(item_backlog_id)
                verified = True

            if not verified:
                summary["missing_backlog_receipts"].append(backlog_id or artifact_path or json.dumps(receipt, ensure_ascii=False))

        for artifact_path in artifact_candidates:
            if artifact_path in checked_artifact_paths:
                continue
            checked_artifact_paths.add(artifact_path)
            backlog_candidates = self._load_backlog_candidates_from_artifact(artifact_path)
            if not backlog_candidates:
                if artifact_path not in summary["missing_backlog_artifacts"]:
                    summary["missing_backlog_artifacts"].append(artifact_path)
                continue
            summary["artifact_items"] += len(backlog_candidates)
            artifact_verified = False
            for item in backlog_candidates:
                if isinstance(item, dict):
                    item_backlog_id = str(item.get("backlog_id") or "").strip()
                else:
                    item_backlog_id = str(item or "").strip()
                if not item_backlog_id:
                    continue
                persisted_item = self._load_backlog_item(item_backlog_id)
                if persisted_item:
                    verified_backlog_ids.add(item_backlog_id)
                    artifact_verified = True
            if not artifact_verified and artifact_path not in summary["missing_backlog_artifacts"]:
                summary["missing_backlog_artifacts"].append(artifact_path)

        summary["verified_backlog_items"] = len(verified_backlog_ids)
        summary["persisted_backlog_items"] = len(verified_backlog_ids)
        summary["planner_persisted_backlog"] = bool(verified_backlog_ids)
        return summary

    def _message_attachment_artifacts(self, message: DiscordMessage) -> list[str]:
        return message_attachment_artifacts_helper(self.paths, message)

    @staticmethod
    def _is_attachment_only_save_failure(message: DiscordMessage) -> bool:
        return is_attachment_only_save_failure_helper(message)

    def _normalize_sourcer_review_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return normalize_sourcer_review_candidates(candidates)

    def _normalize_blocked_backlog_review_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return normalize_blocked_backlog_review_candidates(candidates)

    def _collect_blocked_backlog_review_candidates(self) -> list[dict[str, Any]]:
        return self._normalize_blocked_backlog_review_candidates(
            [
                item
                for item in self._iter_backlog_items()
                if str(item.get("status") or "").strip().lower() == "blocked"
            ]
        )

    def _build_sourcer_review_fingerprint(self, candidates: list[dict[str, Any]]) -> str:
        return build_sourcer_review_fingerprint(candidates)

    def _build_blocked_backlog_review_fingerprint(self, candidates: list[dict[str, Any]]) -> str:
        return build_blocked_backlog_review_fingerprint(candidates)

    def _find_open_sourcer_review_request(self, fingerprint: str) -> dict[str, Any]:
        return find_open_sourcer_review_request_helper(self.paths, fingerprint)

    def _find_open_blocked_backlog_review_request(self, fingerprint: str) -> dict[str, Any]:
        return find_open_blocked_backlog_review_request_helper(self.paths, fingerprint)

    def _find_open_sprint_planning_request(
        self,
        *,
        sprint_id: str,
        phase: str,
        step: str = "",
    ) -> dict[str, Any]:
        normalized_sprint_id = str(sprint_id or "").strip()
        normalized_phase = str(phase or "").strip().lower()
        normalized_step = str(step or "").strip().lower()
        if not normalized_sprint_id or not normalized_phase:
            return {}
        latest_request: dict[str, Any] = {}
        latest_updated_at = ""
        for request_record in iter_request_records(self.paths):
            if not self._is_sprint_planning_request(request_record):
                continue
            params = dict(request_record.get("params") or {})
            request_sprint_id = str(
                request_record.get("sprint_id") or params.get("sprint_id") or ""
            ).strip()
            if request_sprint_id != normalized_sprint_id:
                continue
            if str(params.get("sprint_phase") or "").strip().lower() != normalized_phase:
                continue
            if self._initial_phase_step(request_record) != normalized_step:
                continue
            status = str(request_record.get("status") or "").strip().lower()
            if self._is_terminal_internal_request_status(status):
                continue
            updated_at = str(
                request_record.get("updated_at") or request_record.get("created_at") or ""
            ).strip()
            if not latest_request or updated_at >= latest_updated_at:
                latest_request = request_record
                latest_updated_at = updated_at
        return latest_request

    def _render_sourcer_review_markdown(
        self,
        *,
        request_id: str,
        candidates: list[dict[str, Any]],
        sourcing_activity: dict[str, Any],
    ) -> str:
        return render_sourcer_review_markdown(
            request_id=request_id,
            candidates=candidates,
            sourcing_activity=sourcing_activity,
        )

    def _render_blocked_backlog_review_markdown(
        self,
        *,
        request_id: str,
        candidates: list[dict[str, Any]],
    ) -> str:
        return render_blocked_backlog_review_markdown(
            request_id=request_id,
            candidates=candidates,
        )

    def _build_sourcer_review_request_record(
        self,
        candidates: list[dict[str, Any]],
        *,
        sourcing_activity: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = new_request_id()
        normalized_candidates = self._normalize_sourcer_review_candidates(candidates)
        review_dir = self.paths.shared_workspace_root / "sourcer_reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_file = review_dir / f"{request_id}.md"
        review_file.write_text(
            self._render_sourcer_review_markdown(
                request_id=request_id,
                candidates=normalized_candidates,
                sourcing_activity=sourcing_activity,
            ),
            encoding="utf-8",
        )
        record = build_sourcer_review_request_record_helper(
            request_id=request_id,
            candidates=normalized_candidates,
            sourcing_activity=sourcing_activity,
            artifact_hint=self._workspace_artifact_hint(review_file),
            sprint_id=self.runtime_config.sprint_id,
            fingerprint=self._build_sourcer_review_fingerprint(normalized_candidates),
        )
        self._save_request(record)
        return record

    def _build_blocked_backlog_review_request_record(
        self,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        request_id = new_request_id()
        normalized_candidates = self._normalize_blocked_backlog_review_candidates(candidates)
        review_dir = self.paths.shared_workspace_root / "blocked_backlog_reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_file = review_dir / f"{request_id}.md"
        review_file.write_text(
            self._render_blocked_backlog_review_markdown(
                request_id=request_id,
                candidates=normalized_candidates,
            ),
            encoding="utf-8",
        )
        record = build_blocked_backlog_review_request_record_helper(
            request_id=request_id,
            candidates=normalized_candidates,
            artifact_hint=self._workspace_artifact_hint(review_file),
            fingerprint=self._build_blocked_backlog_review_fingerprint(normalized_candidates),
        )
        self._save_request(record)
        return record

    async def _queue_sourcer_candidates_for_planner_review(
        self,
        candidates: list[dict[str, Any]],
        *,
        sourcing_activity: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_candidates = self._normalize_sourcer_review_candidates(candidates)
        if not normalized_candidates:
            return {"request_id": "", "created": False, "reused": False, "relay_sent": False, "fingerprint": ""}
        fingerprint = self._build_sourcer_review_fingerprint(normalized_candidates)
        existing = self._find_open_sourcer_review_request(fingerprint)
        if existing:
            request_id = str(existing.get("request_id") or "").strip()
            self._last_backlog_sourcing_activity["planner_review_request_id"] = request_id
            self._last_backlog_sourcing_activity["planner_review_status"] = "reused"
            self._last_backlog_sourcing_activity["planner_review_candidate_count"] = len(normalized_candidates)
            return {
                "request_id": request_id,
                "created": False,
                "reused": True,
                "relay_sent": True,
                "fingerprint": fingerprint,
            }
        request_record = self._build_sourcer_review_request_record(
            normalized_candidates,
            sourcing_activity=sourcing_activity,
        )
        request_record["status"] = "delegated"
        request_record["current_role"] = "planner"
        request_record["next_role"] = "planner"
        selection = self._build_governed_routing_selection(
            request_record,
            {},
            current_role="orchestrator",
            preferred_role="planner",
            selection_source="sourcer_review",
        )
        request_record["routing_context"] = self._build_routing_context(
            "planner",
            reason="Selected planner because sourcer candidates require planner-owned backlog review and persistence.",
            preferred_role=str(selection.get("preferred_role") or ""),
            selection_source="sourcer_review",
            matched_signals=[
                str(item).strip()
                for item in (selection.get("matched_signals") or [])
                if str(item).strip()
            ],
            override_reason=str(selection.get("override_reason") or ""),
            matched_strongest_domains=[
                str(item).strip()
                for item in (selection.get("matched_strongest_domains") or [])
                if str(item).strip()
            ],
            matched_preferred_skills=[
                str(item).strip()
                for item in (selection.get("matched_preferred_skills") or [])
                if str(item).strip()
            ],
            matched_behavior_traits=[
                str(item).strip()
                for item in (selection.get("matched_behavior_traits") or [])
                if str(item).strip()
            ],
            policy_source=str(selection.get("policy_source") or ""),
            routing_phase=str(selection.get("routing_phase") or ""),
            request_state_class=str(selection.get("request_state_class") or ""),
            score_total=int(selection.get("score_total") or 0),
            score_breakdown=dict(selection.get("score_breakdown") or {}),
            candidate_summary=list(selection.get("candidate_summary") or []),
        )
        append_request_event(
            request_record,
            event_type="delegated",
            actor="orchestrator",
            summary="internal sourcer 후보를 planner backlog review로 전달했습니다.",
            payload={"routing_context": dict(request_record.get("routing_context") or {})},
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="delegated",
            summary="internal sourcer 후보를 planner backlog review로 전달했습니다.",
        )
        relay_sent = await self._delegate_request(request_record, "planner")
        self._last_backlog_sourcing_activity["planner_review_request_id"] = str(request_record.get("request_id") or "")
        self._last_backlog_sourcing_activity["planner_review_status"] = "delegated" if relay_sent else "relay_failed"
        self._last_backlog_sourcing_activity["planner_review_candidate_count"] = len(normalized_candidates)
        return {
            "request_id": str(request_record.get("request_id") or ""),
            "created": True,
            "reused": False,
            "relay_sent": relay_sent,
            "fingerprint": fingerprint,
        }

    async def _queue_blocked_backlog_for_planner_review(
        self,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized_candidates = self._normalize_blocked_backlog_review_candidates(candidates)
        if not normalized_candidates:
            return {"request_id": "", "created": False, "reused": False, "relay_sent": False}
        fingerprint = self._build_blocked_backlog_review_fingerprint(normalized_candidates)
        existing = self._find_open_blocked_backlog_review_request(fingerprint)
        if existing:
            return {
                "request_id": str(existing.get("request_id") or ""),
                "created": False,
                "reused": True,
                "relay_sent": True,
            }
        request_record = self._build_blocked_backlog_review_request_record(normalized_candidates)
        request_record["status"] = "delegated"
        request_record["current_role"] = "planner"
        request_record["next_role"] = "planner"
        selection = self._build_governed_routing_selection(
            request_record,
            {},
            current_role="orchestrator",
            preferred_role="planner",
            selection_source="blocked_backlog_review",
        )
        request_record["routing_context"] = self._build_routing_context(
            "planner",
            reason=(
                "Selected planner because blocked backlog must be explicitly reopened or kept blocked "
                "before future sprint selection."
            ),
            preferred_role=str(selection.get("preferred_role") or ""),
            selection_source="blocked_backlog_review",
            matched_signals=[
                str(item).strip()
                for item in (selection.get("matched_signals") or [])
                if str(item).strip()
            ],
            override_reason=str(selection.get("override_reason") or ""),
            matched_strongest_domains=[
                str(item).strip()
                for item in (selection.get("matched_strongest_domains") or [])
                if str(item).strip()
            ],
            matched_preferred_skills=[
                str(item).strip()
                for item in (selection.get("matched_preferred_skills") or [])
                if str(item).strip()
            ],
            matched_behavior_traits=[
                str(item).strip()
                for item in (selection.get("matched_behavior_traits") or [])
                if str(item).strip()
            ],
            policy_source=str(selection.get("policy_source") or ""),
            routing_phase=str(selection.get("routing_phase") or ""),
            request_state_class=str(selection.get("request_state_class") or ""),
            score_total=int(selection.get("score_total") or 0),
            score_breakdown=dict(selection.get("score_breakdown") or {}),
            candidate_summary=list(selection.get("candidate_summary") or []),
        )
        append_request_event(
            request_record,
            event_type="delegated",
            actor="orchestrator",
            summary="blocked backlog review를 planner로 전달했습니다.",
            payload={"routing_context": dict(request_record.get("routing_context") or {})},
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="delegated",
            summary="blocked backlog review를 planner로 전달했습니다.",
        )
        relay_sent = await self._delegate_request(request_record, "planner")
        return {
            "request_id": str(request_record.get("request_id") or ""),
            "created": True,
            "reused": False,
            "relay_sent": relay_sent,
        }

    def _build_sprint_planning_request_record(
        self,
        sprint_state: dict[str, Any],
        *,
        phase: str,
        iteration: int,
        step: str = "",
    ) -> dict[str, Any]:
        artifact_paths = self._sprint_artifact_paths(sprint_state)
        normalized_step = str(step or "").strip().lower()
        self._merge_persisted_sprint_research_prepass(sprint_state)
        existing_request = self._find_open_sprint_planning_request(
            sprint_id=str(sprint_state.get("sprint_id") or ""),
            phase=phase,
            step=normalized_step,
        )
        if existing_request:
            if self._should_start_sprint_research_prepass(
                sprint_state,
                phase=phase,
                iteration=iteration,
                step=normalized_step,
            ):
                params = dict(existing_request.get("params") or {})
                if not isinstance(params.get("workflow"), dict):
                    params["workflow"] = self._research_first_workflow_state()
                    existing_request["params"] = params
                    existing_request["next_role"] = "research"
                    self._save_request(existing_request)
            return existing_request
        step_title = self._initial_phase_step_title(normalized_step) if normalized_step else ""
        artifacts = _dedupe_preserving_order(
            [
                self._workspace_artifact_hint(self.paths.shared_backlog_file),
                self._workspace_artifact_hint(self.paths.shared_completed_backlog_file),
                self._workspace_artifact_hint(self.paths.current_sprint_file),
                self._workspace_artifact_hint(artifact_paths["kickoff"]),
                self._workspace_artifact_hint(artifact_paths["milestone"]),
                self._workspace_artifact_hint(artifact_paths["plan"]),
                self._workspace_artifact_hint(artifact_paths["spec"]),
                self._workspace_artifact_hint(artifact_paths["todo_backlog"]),
                self._workspace_artifact_hint(artifact_paths["iteration_log"]),
                *[
                    str(item).strip()
                    for item in (sprint_state.get("kickoff_reference_artifacts") or [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for item in (sprint_state.get("reference_artifacts") or [])
                    if str(item).strip()
                ],
                *self._sprint_research_prepass_artifacts(sprint_state),
            ]
        )
        record = build_sprint_planning_request_record_helper(
            sprint_state,
            phase=phase,
            iteration=iteration,
            step=normalized_step,
            request_id=new_request_id(),
            artifacts=artifacts,
            created_at=utc_now_iso(),
            updated_at=utc_now_iso(),
            git_baseline=capture_git_baseline(self.paths.project_workspace_root),
        )
        if self._should_start_sprint_research_prepass(
            sprint_state,
            phase=phase,
            iteration=iteration,
            step=normalized_step,
        ):
            params = dict(record.get("params") or {})
            params["workflow"] = self._research_first_workflow_state()
            record["params"] = params
            record["next_role"] = "research"
        append_request_event(
            record,
            event_type="created",
            actor="sprint_runner",
            summary=(
                f"스프린트 {phase} planning 요청을 생성했습니다."
                if not normalized_step
                else f"스프린트 {phase} planning 요청을 생성했습니다. step={step_title}"
            ),
        )
        self._save_request(record)
        return record

    def _maybe_update_sprint_name_from_result(self, sprint_state: dict[str, Any], result: dict[str, Any]) -> None:
        maybe_update_sprint_name_from_result_helper(self, sprint_state, result)

    def _sync_manual_sprint_queue(self, sprint_state: dict[str, Any]) -> None:
        sync_manual_sprint_queue_helper(self, sprint_state)

    @staticmethod
    def _normalize_trace_list(values: Any) -> list[str]:
        return normalize_trace_list_helper(values)

    def _collect_sprint_relevant_backlog_items(self, sprint_state: dict[str, Any]) -> list[dict[str, Any]]:
        return collect_sprint_relevant_backlog_items_helper(
            sprint_state,
            self._iter_backlog_items(),
        )

    def _validate_initial_phase_step_result(
        self,
        sprint_state: dict[str, Any],
        *,
        request_record: dict[str, Any],
        sync_summary: dict[str, Any],
        result: dict[str, Any] | None = None,
    ) -> str:
        return validate_initial_phase_step_result_helper(
            sprint_state,
            request_record=request_record,
            sync_summary=sync_summary,
            relevant_items=self._collect_sprint_relevant_backlog_items(sprint_state),
            result=result,
        )

    def _record_sprint_planning_iteration(
        self,
        sprint_state: dict[str, Any],
        *,
        phase: str,
        step: str,
        request_record: dict[str, Any],
        result: dict[str, Any],
        phase_ready: bool,
    ) -> None:
        record_sprint_planning_iteration_helper(
            sprint_state,
            created_at=utc_now_iso(),
            phase=phase,
            step=step,
            request_record=request_record,
            result=result,
            phase_ready=phase_ready,
        )

    def _sync_sprint_planning_state(
        self,
        sprint_state: dict[str, Any],
        *,
        phase: str,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> bool:
        return sync_sprint_planning_state_helper(
            self,
            sprint_state,
            phase=phase,
            request_record=request_record,
            result=result,
        )

    def _sync_internal_sprint_artifacts_from_role_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return sync_internal_sprint_artifacts_from_role_report_helper(self, request_record, result)

    def _sync_planner_backlog_review_from_role_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return sync_planner_backlog_review_from_role_report_helper(self, request_record, result)

    def _apply_sprint_planning_result(
        self,
        sprint_state: dict[str, Any],
        *,
        phase: str,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> bool:
        return apply_sprint_planning_result_helper(
            self,
            sprint_state,
            phase=phase,
            request_record=request_record,
            result=result,
        )

    async def _run_initial_sprint_phase(self, sprint_state: dict[str, Any]) -> bool:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if self._is_wrap_up_requested(sprint_state):
            sprint_state["phase"] = "wrap_up"
            self._save_sprint_state(sprint_state)
            return False
        for iteration in range(1, SPRINT_INITIAL_PHASE_MAX_ITERATIONS + 1):
            phase_ready = False
            for step in INITIAL_PHASE_STEPS:
                if self._is_wrap_up_requested(sprint_state):
                    sprint_state["phase"] = "wrap_up"
                    self._save_sprint_state(sprint_state)
                    return False
                request_record = self._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=iteration,
                    step=step,
                )
                result = await self._run_internal_request_chain(
                    sprint_id=sprint_id,
                    request_record=request_record,
                    initial_role="planner",
                )
                request_record = self._load_request(str(request_record.get("request_id") or "")) or request_record
                if str(result.get("status") or "").strip().lower() != "completed":
                    await self._complete_terminal_sprint(
                        sprint_state,
                        status="blocked",
                        closeout_status="planning_incomplete",
                        terminal_title="⚠️ 스프린트 시작 실패",
                        message=(
                            "initial phase planning이 완료되지 않아 sprint를 시작하지 못했습니다. "
                            f"step={self._initial_phase_step_title(step)} | "
                            f"request_id={request_record.get('request_id') or ''} | "
                            f"summary={result.get('summary') or result.get('error') or ''}"
                        ).strip(),
                        clear_active=True,
                    )
                    return False
                phase_ready = self._apply_sprint_planning_result(
                    sprint_state,
                    phase="initial",
                    request_record=request_record,
                    result=result,
                )
                validation_error = str(request_record.get("initial_phase_validation_error") or "").strip()
                if validation_error:
                    await self._complete_terminal_sprint(
                        sprint_state,
                        status="blocked",
                        closeout_status="planning_incomplete",
                        terminal_title="⚠️ 스프린트 시작 실패",
                        message=validation_error,
                        clear_active=True,
                    )
                    return False
                self._save_sprint_state(sprint_state)
            if phase_ready:
                self._save_sprint_state(sprint_state)
                missing_artifacts = self._missing_sprint_preflight_artifacts(sprint_state)
                if missing_artifacts:
                    await self._complete_terminal_sprint(
                        sprint_state,
                        status="blocked",
                        closeout_status="planning_incomplete",
                        terminal_title="⚠️ 스프린트 시작 실패",
                        message=(
                            "initial phase는 완료됐지만 sprint start 전 canonical spec/todo 문서가 아직 닫히지 않았습니다. "
                            f"missing={', '.join(missing_artifacts)}"
                        ).strip(),
                        clear_active=True,
                    )
                    return False
                try:
                    await self._send_sprint_spec_todo_report(sprint_state, swallow_exceptions=False)
                except Exception as exc:
                    await self._complete_terminal_sprint(
                        sprint_state,
                        status="blocked",
                        closeout_status="planning_incomplete",
                        terminal_title="⚠️ 스프린트 시작 실패",
                        message=(
                            "initial phase는 완료됐지만 sprint start 전 spec/todo 보고 전송에 실패했습니다. "
                            f"{str(exc).strip()}"
                        ).strip(),
                        clear_active=True,
                    )
                    return False
                sprint_state["phase"] = "ongoing"
                sprint_state["status"] = "running"
                sprint_state["initial_phase_ready_at"] = utc_now_iso()
                sprint_state["last_planner_review_at"] = utc_now_iso()
                self._save_sprint_state(sprint_state)
                return True
        await self._complete_terminal_sprint(
            sprint_state,
            status="blocked",
            closeout_status="planning_incomplete",
            terminal_title="⚠️ 스프린트 시작 실패",
            message="initial phase에서 실행 가능한 prioritized todo를 만들지 못해 sprint를 중단했습니다.",
            clear_active=True,
        )
        return False

    async def _run_ongoing_sprint_review(self, sprint_state: dict[str, Any], *, force: bool = False) -> None:
        if self._is_wrap_up_requested(sprint_state):
            sprint_state["phase"] = "wrap_up"
            self._save_sprint_state(sprint_state)
            return
        last_review_at = self._parse_datetime(str(sprint_state.get("last_planner_review_at") or ""))
        if not force and last_review_at is not None:
            elapsed_seconds = (utc_now() - last_review_at).total_seconds()
            if elapsed_seconds < max(float(self.runtime_config.sprint_interval_minutes) * 60.0, 1.0):
                return
        request_record = self._build_sprint_planning_request_record(
            sprint_state,
            phase="ongoing_review",
            iteration=len(sprint_state.get("planning_iterations") or []) + 1,
        )
        result = await self._run_internal_request_chain(
            sprint_id=str(sprint_state.get("sprint_id") or ""),
            request_record=request_record,
            initial_role="planner",
        )
        request_record = self._load_request(str(request_record.get("request_id") or "")) or request_record
        if str(result.get("status") or "").strip().lower() != "completed":
            return
        self._apply_sprint_planning_result(
            sprint_state,
            phase="ongoing_review",
            request_record=request_record,
            result=result,
        )
        sprint_state["last_planner_review_at"] = utc_now_iso()
        self._save_sprint_state(sprint_state)

    async def _continue_manual_daily_sprint(self, sprint_state: dict[str, Any], *, announce: bool) -> None:
        await continue_manual_daily_sprint_helper(self, sprint_state, announce=announce)
    async def _continue_sprint(self, sprint_state: dict[str, Any], *, announce: bool) -> None:
        await continue_sprint_helper(self, sprint_state, announce=announce)
    async def _finalize_sprint(self, sprint_state: dict[str, Any]) -> None:
        await finalize_sprint_helper(self, sprint_state)
    async def _fail_sprint_due_to_exception(self, sprint_state: dict[str, Any], exc: Exception) -> None:
        await fail_sprint_due_to_exception_helper(self, sprint_state, exc)
    async def _send_sprint_kickoff(self, sprint_state: dict[str, Any]) -> None:
        await send_sprint_kickoff_for_service_helper(self, sprint_state)

    def _build_sprint_kickoff_preview_lines(self, sprint_state: dict[str, Any], *, limit: int = 3) -> list[str]:
        return build_sprint_kickoff_preview_lines_helper(sprint_state, limit=limit)

    async def _send_sprint_todo_list(self, sprint_state: dict[str, Any]) -> None:
        await send_sprint_todo_list_for_service_helper(self, sprint_state)

    async def _send_sprint_completion_user_report(
        self,
        *,
        title: str,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> bool:
        return await send_sprint_completion_user_report_for_service_helper(
            self,
            title=title,
            sprint_state=sprint_state,
            closeout_result=closeout_result,
        )

    async def _send_terminal_sprint_reports(
        self,
        *,
        title: str,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
        judgment: str = "",
        commit_message: str = "",
        related_artifacts: list[str] | None = None,
    ) -> None:
        await send_terminal_sprint_reports_for_service_helper(
            self,
            title=title,
            sprint_state=sprint_state,
            closeout_result=closeout_result,
            judgment=judgment,
            commit_message=commit_message,
            related_artifacts=related_artifacts,
        )

    async def _send_sprint_report(
        self,
        *,
        title: str,
        body: str,
        sprint_id: str = "",
        status: str = "완료",
        end_reason: str = "없음",
        judgment: str = "",
        next_action: str = "대기",
        commit_message: str = "",
        related_artifacts: list[str] | None = None,
        log_summary: str = "",
        sections: list[ReportSection] | None = None,
        swallow_exceptions: bool = True,
    ) -> None:
        await send_sprint_report_for_service_helper(
            self,
            title=title,
            body=body,
            sprint_id=sprint_id,
            status=status,
            end_reason=end_reason,
            judgment=judgment,
            next_action=next_action,
            commit_message=commit_message,
            related_artifacts=related_artifacts,
            log_summary=log_summary,
            sections=sections,
            swallow_exceptions=swallow_exceptions,
        )

    @staticmethod
    def _planner_initial_phase_report_keys(request_record: dict[str, Any]) -> list[str]:
        return planner_initial_phase_report_keys_helper(request_record)

    def _planner_initial_phase_report_key(
        self,
        request_record: dict[str, Any],
        *,
        event_type: str,
        status: str,
        summary: str,
    ) -> str:
        return planner_initial_phase_report_key_helper(
            request_record,
            event_type=event_type,
            status=status,
            summary=summary,
        )

    def _planner_initial_phase_work_lines(
        self,
        *,
        step: str,
        sprint_state: dict[str, Any],
        proposals: dict[str, Any],
    ) -> list[str]:
        return planner_initial_phase_work_lines_helper(
            step=step,
            sprint_state=sprint_state,
            proposals=proposals,
            backlog_items=self._collect_sprint_relevant_backlog_items(sprint_state) if sprint_state else [],
            format_backlog_line=self._format_backlog_report_line,
            format_todo_line=lambda todo: self._format_todo_report_line(todo, include_artifacts=True),
        )

    def _planner_initial_phase_priority_lines(
        self,
        *,
        step: str,
        sprint_state: dict[str, Any],
        proposals: dict[str, Any],
    ) -> list[str]:
        return planner_initial_phase_priority_lines_helper(
            step=step,
            sprint_state=sprint_state,
            proposals=proposals,
            backlog_items=self._collect_sprint_relevant_backlog_items(sprint_state) if sprint_state else [],
            format_backlog_line=self._format_backlog_report_line,
            format_todo_line=lambda todo: self._format_todo_report_line(todo, include_artifacts=True),
        )

    def _build_planner_initial_phase_activity_sections(
        self,
        request_record: dict[str, Any],
        *,
        step: str,
        step_position: int,
        event_type: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> list[ReportSection]:
        sprint_id = str(request_record.get("sprint_id") or dict(request_record.get("params") or {}).get("sprint_id") or "").strip()
        sprint_state = self._load_sprint_state(sprint_id) if sprint_id else {}
        proposals = dict(payload.get("proposals") or {}) if isinstance(payload, dict) and isinstance(payload.get("proposals"), dict) else {}
        semantic_context = self._build_role_result_semantic_context(payload or {}) if isinstance(payload, dict) else {}
        doc_refs = self._planner_doc_targets(proposals)
        return build_planner_initial_phase_activity_sections_helper(
            request_record,
            step=step,
            step_position=step_position,
            event_type=event_type,
            status=status,
            summary=summary,
            sprint_state=sprint_state,
            proposals=proposals,
            semantic_context=semantic_context,
            backlog_items=self._collect_sprint_relevant_backlog_items(sprint_state) if sprint_state else [],
            doc_refs=doc_refs,
            error_text=str((payload or {}).get("error") or "").strip(),
            format_backlog_line=self._format_backlog_report_line,
            format_todo_line=lambda todo: self._format_todo_report_line(todo, include_artifacts=True),
        )

    def _planner_initial_phase_next_action(self, request_record: dict[str, Any], event_type: str, status: str) -> str:
        return planner_initial_phase_next_action_helper(request_record, event_type, status)

    def _build_planner_initial_phase_activity_report(
        self,
        request_record: dict[str, Any],
        *,
        event_type: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        step = self._initial_phase_step(request_record)
        if not step:
            return ""
        sprint_id = str(request_record.get("sprint_id") or dict(request_record.get("params") or {}).get("sprint_id") or "").strip()
        normalized_event = str(event_type or "").strip().lower()
        semantic_context = self._build_role_result_semantic_context(payload or {}) if isinstance(payload, dict) else {}
        sections = self._build_planner_initial_phase_activity_sections(
            request_record,
            step=step,
            step_position=self._initial_phase_step_position(step),
            event_type=normalized_event,
            status=str(status or ""),
            summary=summary,
            payload=payload,
        )
        return build_planner_initial_phase_activity_report_helper(
            request_record,
            event_type=normalized_event,
            status=status,
            summary=summary,
            semantic_context=semantic_context,
            sprint_scope=self._format_sprint_scope(sprint_id=sprint_id),
            artifacts=[str(item) for item in (request_record.get("artifacts") or []) if str(item).strip()],
            sections=sections,
        )

    async def _maybe_report_planner_initial_phase_activity(
        self,
        request_record: dict[str, Any],
        *,
        event_type: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.role != "planner":
            return
        if not self._is_initial_phase_planner_request(request_record):
            return
        channel_id = str(self.discord_config.report_channel_id or "").strip()
        if not channel_id:
            return
        report = self._build_planner_initial_phase_activity_report(
            request_record,
            event_type=event_type,
            status=status,
            summary=summary,
            payload=payload,
        )
        if not report:
            return
        report_key = self._planner_initial_phase_report_key(
            request_record,
            event_type=event_type,
            status=status,
            summary=summary,
        )
        if report_key in self._planner_initial_phase_report_keys(request_record):
            return
        try:
            await self._send_discord_content(
                content=report,
                send=lambda chunk: self.discord_client.send_channel_message(channel_id, chunk),
                target_description=f"planner-initial-phase:{channel_id}:{request_record.get('request_id') or ''}:{event_type}",
                swallow_exceptions=False,
                log_traceback=False,
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to send planner initial phase report for request %s step=%s event=%s: %s",
                request_record.get("request_id") or "unknown",
                self._initial_phase_step(request_record) or "unknown",
                event_type,
                exc,
            )
            return
        keys = self._planner_initial_phase_report_keys(request_record)
        keys.append(report_key)
        request_record["planner_initial_phase_report_keys"] = keys[-12:]
        self._persist_request_result(request_record)

    def _get_sourcer_report_client(self) -> DiscordClient | None:
        return get_sourcer_report_client_for_service_helper(
            self,
            discord_client_factory=DiscordClient,
            logger=LOGGER,
        )

    def _report_sourcer_activity_sync(
        self,
        *,
        sourcing_activity: dict[str, Any],
        added: int,
        updated: int,
        candidates: list[dict[str, Any]],
    ) -> None:
        report_sourcer_activity_sync_helper(
            self,
            sourcing_activity=sourcing_activity,
            added=added,
            updated=updated,
            candidates=candidates,
            logger=LOGGER,
        )

    async def _resume_uncommitted_sprint_todo(
        self,
        *,
        sprint_state: dict[str, Any],
        todo: dict[str, Any],
        request_record: dict[str, Any],
    ) -> dict[str, Any]:
        return await resume_uncommitted_sprint_todo_helper(
            self,
            sprint_state=sprint_state,
            todo=todo,
            request_record=request_record,
        )
    async def _execute_sprint_todo(self, sprint_state: dict[str, Any], todo: dict[str, Any]) -> None:
        await execute_sprint_todo_helper(self, sprint_state, todo)
    async def _enforce_task_commit_for_completed_todo(
        self,
        *,
        sprint_state: dict[str, Any],
        todo: dict[str, Any],
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return await enforce_task_commit_for_completed_todo_helper(
            self,
            sprint_state=sprint_state,
            todo=todo,
            request_record=request_record,
            result=result,
        )
    def _create_internal_request_record(
        self,
        sprint_state: dict[str, Any],
        todo: dict[str, Any],
        backlog_item: dict[str, Any],
    ) -> dict[str, Any]:
        return create_internal_request_record_helper(self, sprint_state, todo, backlog_item)

    async def _run_internal_request_chain(
        self,
        *,
        sprint_id: str,
        request_record: dict[str, Any],
        initial_role: str,
    ) -> dict[str, Any]:
        return await run_internal_request_chain_helper(
            self,
            sprint_id=sprint_id,
            request_record=request_record,
            initial_role=initial_role,
        )

    @staticmethod
    def _sprint_status_label(status: str) -> str:
        return sprint_status_label_helper(status)

    @staticmethod
    def _sprint_role_display_name(role: str) -> str:
        return sprint_role_display_name_helper(role)

    @staticmethod
    def _limit_sprint_report_lines(lines: Iterable[str], *, limit: int) -> list[str]:
        return limit_sprint_report_lines_helper(lines, limit=limit)

    @staticmethod
    def _format_sprint_report_text(value: Any, *, full_detail: bool = False, limit: int = 240) -> str:
        return format_sprint_report_text_helper(value, full_detail=full_detail, limit=limit)

    def _format_sprint_duration(self, sprint_state: dict[str, Any]) -> str:
        return format_sprint_duration_helper(sprint_state)

    def _load_sprint_event_entries(self, sprint_state: dict[str, Any]) -> list[dict[str, Any]]:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if not sprint_id:
            return []
        return iter_sprint_event_entries(self.paths, sprint_id)

    def _preview_sprint_artifact_path(
        self,
        sprint_state: dict[str, Any],
        value: str,
        *,
        full_detail: bool = False,
    ) -> str:
        return preview_sprint_artifact_path_helper(
            sprint_state,
            value,
            workspace_root=self.paths.workspace_root,
            full_detail=full_detail,
        )

    def _sprint_report_draft(self, sprint_state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        if snapshot is not None:
            draft = _normalize_sprint_report_draft(snapshot.get("planner_report_draft"))
            if draft:
                return draft
        return _normalize_sprint_report_draft(sprint_state.get("planner_report_draft"))

    def _planner_closeout_request_id(self, sprint_state: dict[str, Any]) -> str:
        return planner_closeout_request_id_helper(sprint_state)

    def _relative_workspace_path(self, path: Path) -> str:
        return relative_workspace_path_helper(path, self.paths.workspace_root)

    def _write_planner_closeout_context_file(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> str:
        request_id = self._planner_closeout_request_id(sprint_state)
        source_dir = self.paths.role_sources_dir("planner")
        source_dir.mkdir(parents=True, exist_ok=True)
        payload_path = source_dir / f"{request_id}.closeout_report.json"
        request_ids = [
            str(todo.get("request_id") or "").strip()
            for todo in (snapshot.get("todos") or [])
            if str(todo.get("request_id") or "").strip()
        ]
        payload = build_planner_closeout_context_payload_helper(
            sprint_state=sprint_state,
            closeout_result=closeout_result,
            snapshot=snapshot,
            request_files=[
                self._relative_workspace_path(self.paths.request_file(request_id))
                for request_id in request_ids
            ],
        )
        write_json(payload_path, payload)
        return self._relative_workspace_path(payload_path)

    def _planner_closeout_artifacts(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        context_file: str,
    ) -> list[str]:
        sprint_folder_name = str(
            sprint_state.get("sprint_folder_name") or build_sprint_artifact_folder_name(str(sprint_state.get("sprint_id") or ""))
        ).strip()
        sprint_artifact_files: list[str] = []
        for filename in ("kickoff.md", "milestone.md", "spec.md", "iteration_log.md", "report.md"):
            path = self.paths.sprint_artifact_file(sprint_folder_name, filename)
            if path.exists():
                sprint_artifact_files.append(self._relative_workspace_path(path))
        request_files: list[str] = []
        for todo in snapshot.get("todos") or []:
            request_id = str(todo.get("request_id") or "").strip()
            if request_id:
                request_files.append(self._relative_workspace_path(self.paths.request_file(request_id)))
        return build_planner_closeout_artifacts_helper(
            context_file=context_file,
            sprint_artifact_files=sprint_artifact_files,
            request_files=request_files,
        )

    async def _draft_sprint_report_via_planner(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        context_file = self._write_planner_closeout_context_file(sprint_state, closeout_result, snapshot)
        artifacts = self._planner_closeout_artifacts(sprint_state, snapshot, context_file=context_file)
        request_id = self._planner_closeout_request_id(sprint_state)
        now = utc_now_iso()
        request_context = build_planner_closeout_request_context_helper(
            sprint_state=sprint_state,
            closeout_result=closeout_result,
            request_id=request_id,
            artifacts=list(artifacts),
            created_at=now,
            updated_at=now,
        )
        envelope = MessageEnvelope(
            **build_planner_closeout_envelope_payload_helper(
                request_id=request_id,
                scope=str(request_context.get("scope") or ""),
                artifacts=list(artifacts),
            )
        )
        try:
            runtime = self._runtime_for_role("planner", self.runtime_config.sprint_id)
            result = await asyncio.to_thread(runtime.run_task, envelope, request_context)
        except Exception:
            LOGGER.exception(
                "Planner closeout report drafting failed for sprint %s",
                sprint_state.get("sprint_id") or "unknown",
            )
            return {}
        normalized = normalize_role_payload(result)
        draft = _normalize_sprint_report_draft(
            dict(normalized.get("proposals") or {}).get("sprint_report")
        )
        if not draft:
            LOGGER.warning(
                "Planner closeout report draft missing or invalid for sprint %s",
                sprint_state.get("sprint_id") or "unknown",
            )
        return draft

    async def _prepare_sprint_report_body(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> str:
        sprint_state["planner_report_draft"] = await self._draft_sprint_report_via_planner(sprint_state, closeout_result)
        report_body = self._build_sprint_report_body(sprint_state, closeout_result)
        sprint_state["report_body"] = report_body
        return report_body

    async def _prepare_and_archive_sprint_report(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> str:
        report_body = await self._prepare_sprint_report_body(sprint_state, closeout_result)
        sprint_state.update(
            build_sprint_report_archive_state_helper(
                report_body=report_body,
                report_path=self._archive_sprint_history(sprint_state, report_body),
            )
        )
        return report_body

    async def _complete_terminal_sprint(
        self,
        sprint_state: dict[str, Any],
        *,
        status: str,
        closeout_status: str,
        terminal_title: str,
        message: str,
        clear_active: bool | None = None,
        commit_count: int | None = None,
        commit_shas: list[str] | None = None,
        representative_commit_sha: str | None = None,
        uncommitted_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        sprint_state.update(
            build_sprint_terminal_state_update_helper(
                status=status,
                closeout_status=closeout_status,
                ended_at=utc_now_iso(),
            )
        )
        closeout_result = build_sprint_closeout_result_helper(
            sprint_state=sprint_state,
            status=closeout_status,
            message=message,
            commit_count=commit_count,
            commit_shas=commit_shas,
            representative_commit_sha=representative_commit_sha,
            uncommitted_paths=uncommitted_paths,
        )
        await self._prepare_and_archive_sprint_report(sprint_state, closeout_result)
        self._save_sprint_state(sprint_state)
        await self._send_terminal_sprint_reports(
            title=terminal_title,
            sprint_state=sprint_state,
            closeout_result=closeout_result,
        )
        self._finish_scheduler_after_sprint(sprint_state, clear_active=clear_active)
        return closeout_result

    async def _complete_terminal_sprint_from_closeout_result(
        self,
        sprint_state: dict[str, Any],
        *,
        closeout_result: dict[str, Any],
        terminal_title: str,
        clear_active: bool | None = None,
    ) -> dict[str, Any]:
        sprint_state.update(
            build_sprint_closeout_state_update_helper(
                closeout_result=closeout_result,
                ended_at=utc_now_iso(),
            )
        )
        await self._prepare_and_archive_sprint_report(sprint_state, closeout_result)
        self._save_sprint_state(sprint_state)
        await self._send_terminal_sprint_reports(
            title=terminal_title,
            sprint_state=sprint_state,
            closeout_result=closeout_result,
        )
        self._finish_scheduler_after_sprint(sprint_state, clear_active=clear_active)
        return closeout_result

    def _collect_sprint_delivered_changes(
        self,
        sprint_state: dict[str, Any],
        todos: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        milestone = _collapse_whitespace(
            sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or ""
        )
        changes: list[dict[str, Any]] = []
        for todo in todos:
            status = str(todo.get("status") or "").strip().lower()
            if status not in {"committed", "completed"}:
                continue
            request_record = self._load_request(str(todo.get("request_id") or ""))
            result = dict(request_record.get("result") or {}) if isinstance(request_record, dict) else {}
            semantic_context = self._latest_sprint_change_semantic_context(request_record)
            insights = _normalize_insights(result)
            title = _first_meaningful_text(todo.get("title"), request_record.get("scope"), "Untitled change")
            scope = _first_meaningful_text(request_record.get("scope"), todo.get("summary"), title)
            changes.append(
                build_sprint_delivered_change_helper(
                    milestone=milestone,
                    title=title,
                    scope=scope,
                    semantic_context=semantic_context,
                    insights=insights,
                    artifact_candidates=self._collect_artifact_candidates(
                        todo.get("artifacts"),
                        request_record.get("version_control_paths"),
                        request_record.get("task_commit_paths"),
                        result.get("artifacts"),
                    ),
                    preview_artifact=lambda artifact: self._preview_sprint_artifact_path(
                        sprint_state,
                        artifact,
                        full_detail=True,
                    ),
                    what_changed_fallbacks=(
                        request_record.get("task_commit_summary"),
                        result.get("summary"),
                        todo.get("summary"),
                        title,
                    ),
                )
            )
        return changes

    def _latest_sprint_change_semantic_context(self, request_record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request_record, dict):
            return {}
        for report in reversed(collect_sprint_role_report_events_helper(request_record)):
            payload = dict(report.get("payload") or {})
            if not payload:
                continue
            semantic_context = self._build_role_result_semantic_context(payload)
            if any(
                (
                    semantic_context.get("what_summary"),
                    semantic_context.get("what_details"),
                    semantic_context.get("how_summary"),
                    semantic_context.get("why_summary"),
                )
            ):
                return semantic_context
        result = dict(request_record.get("result") or {})
        return self._build_role_result_semantic_context(result) if result else {}

    def _build_sprint_change_summary_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        return render_sprint_change_summary_lines_helper(
            sprint_state,
            snapshot,
            draft=self._sprint_report_draft(sprint_state, snapshot),
            format_text=self._format_sprint_report_text,
            full_detail=full_detail,
        )

    def _build_machine_sprint_report_lines(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> list[str]:
        todo_status_counts = self._count_by_key(list(sprint_state.get("todos") or []), "status")
        return render_machine_sprint_report_lines_helper(
            sprint_state,
            closeout_result,
            todo_status_counts=todo_status_counts,
            linked_artifacts=collect_sprint_todo_artifact_entries(sprint_state),
            format_count_summary=_format_count_summary,
        )

    def _collect_sprint_report_snapshot(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> dict[str, Any]:
        todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        todo_status_counts = self._count_by_key(todos, "status")
        return build_sprint_report_snapshot_helper(
            sprint_state=sprint_state,
            closeout_result=closeout_result,
            todos=todos,
            delivered_changes=self._collect_sprint_delivered_changes(sprint_state, todos),
            planner_report_draft=self._sprint_report_draft(sprint_state),
            linked_artifacts=collect_sprint_todo_artifact_entries(sprint_state),
            todo_status_counts=todo_status_counts,
            events=self._load_sprint_event_entries(sprint_state),
            duration=self._format_sprint_duration(sprint_state),
            status_label=self._sprint_status_label(str(sprint_state.get("status") or "")),
            format_count_summary=_format_count_summary,
        )

    def _build_sprint_headline(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> str:
        return render_sprint_headline(
            sprint_state,
            snapshot,
            draft=self._sprint_report_draft(sprint_state, snapshot),
            format_text=self._format_sprint_report_text,
            full_detail=full_detail,
        )

    def _build_sprint_overview_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        headline = self._build_sprint_headline(sprint_state, snapshot, full_detail=full_detail)
        return render_sprint_overview_lines(
            sprint_state,
            snapshot,
            headline=headline,
        )

    def _build_sprint_timeline_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        return render_sprint_timeline_lines(
            sprint_state,
            snapshot,
            draft=self._sprint_report_draft(sprint_state, snapshot),
            format_text=self._format_sprint_report_text,
            role_display_name=self._sprint_role_display_name,
            full_detail=full_detail,
        )

    def _build_sprint_planned_todo_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        return build_sprint_planned_todo_lines_helper(
            sprint_state,
            snapshot,
            format_text=self._format_sprint_report_text,
            full_detail=full_detail,
        )

    def _build_sprint_commit_lines(self, snapshot: dict[str, Any]) -> list[str]:
        return build_sprint_commit_lines_helper(snapshot)

    def _build_sprint_followup_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        return build_sprint_followup_lines_helper(
            sprint_state,
            snapshot,
            format_text=self._format_sprint_report_text,
            preview_artifact=lambda state, path: self._preview_sprint_artifact_path(
                state,
                path,
                full_detail=full_detail,
            ),
            full_detail=full_detail,
        )

    def _build_sprint_agent_contribution_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        return render_sprint_agent_contribution_lines(
            sprint_state,
            snapshot,
            draft=self._sprint_report_draft(sprint_state, snapshot),
            format_text=self._format_sprint_report_text,
            role_display_name=self._sprint_role_display_name,
            preview_artifact=lambda state, path: self._preview_sprint_artifact_path(
                state,
                path,
                full_detail=full_detail,
            ),
            team_roles=TEAM_ROLES,
            full_detail=full_detail,
        )

    def _build_sprint_issue_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        return render_sprint_issue_lines(
            sprint_state,
            snapshot,
            draft=self._sprint_report_draft(sprint_state, snapshot),
            format_text=self._format_sprint_report_text,
            role_display_name=self._sprint_role_display_name,
            preview_artifact=lambda state, path: self._preview_sprint_artifact_path(
                state,
                path,
                full_detail=full_detail,
            ),
            full_detail=full_detail,
        )

    def _build_sprint_achievement_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        return render_sprint_achievement_lines(
            sprint_state,
            snapshot,
            draft=self._sprint_report_draft(sprint_state, snapshot),
            format_text=self._format_sprint_report_text,
            full_detail=full_detail,
        )

    def _build_sprint_artifact_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        return render_sprint_artifact_lines(
            sprint_state,
            snapshot,
            draft=self._sprint_report_draft(sprint_state, snapshot),
            format_text=self._format_sprint_report_text,
            preview_artifact=lambda state, path: self._preview_sprint_artifact_path(
                state,
                path,
                full_detail=full_detail,
            ),
            full_detail=full_detail,
        )

    def _build_sprint_progress_log_summary(self, sprint_state: dict[str, Any], closeout_result: dict[str, Any]) -> str:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        return render_sprint_progress_log_summary_helper(
            sprint_state,
            snapshot,
            build_headline=lambda state, snap, detail: self._build_sprint_headline(state, snap, full_detail=detail),
            build_issue_lines=lambda state, snap, detail: self._build_sprint_issue_lines(state, snap, full_detail=detail),
            build_achievement_lines=lambda state, snap, detail: self._build_sprint_achievement_lines(
                state,
                snap,
                full_detail=detail,
            ),
        )

    def _render_sprint_completion_user_report(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
        *,
        title: str,
    ) -> str:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        report_path = self.paths.sprint_artifact_file(
            str(sprint_state.get("sprint_folder_name") or build_sprint_artifact_folder_name(str(sprint_state.get("sprint_id") or ""))),
            "report.md",
        )
        report_path_text = build_sprint_report_path_text_helper(report_path, self.paths.workspace_root)
        return render_sprint_completion_user_report_helper(
            title=title,
            sprint_state=sprint_state,
            snapshot=snapshot,
            report_path_text=report_path_text,
            decorate_title=_decorate_sprint_report_title,
            build_headline=lambda state, snap, detail: self._build_sprint_headline(state, snap, full_detail=detail),
            build_change_summary_lines=lambda state, snap, detail: self._build_sprint_change_summary_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_planned_todo_lines=lambda state, snap, detail: self._build_sprint_planned_todo_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_commit_lines=self._build_sprint_commit_lines,
            build_followup_lines=lambda state, snap, detail: self._build_sprint_followup_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_timeline_lines=lambda state, snap, detail: self._build_sprint_timeline_lines(state, snap, full_detail=detail),
            build_agent_contribution_lines=lambda state, snap, detail: self._build_sprint_agent_contribution_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_issue_lines=lambda state, snap, detail: self._build_sprint_issue_lines(state, snap, full_detail=detail),
            build_achievement_lines=lambda state, snap, detail: self._build_sprint_achievement_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_artifact_lines=lambda state, snap, detail: self._build_sprint_artifact_lines(
                state,
                snap,
                full_detail=detail,
            ),
        )

    def _build_sprint_report_body(self, sprint_state: dict[str, Any], closeout_result: dict[str, Any]) -> str:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        return render_sprint_report_body_helper(
            sprint_state,
            snapshot,
            closeout_result,
            build_overview_lines=lambda state, snap, detail: self._build_sprint_overview_lines(state, snap, full_detail=detail),
            build_change_summary_lines=lambda state, snap, detail: self._build_sprint_change_summary_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_planned_todo_lines=lambda state, snap, detail: self._build_sprint_planned_todo_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_commit_lines=self._build_sprint_commit_lines,
            build_followup_lines=lambda state, snap, detail: self._build_sprint_followup_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_timeline_lines=lambda state, snap, detail: self._build_sprint_timeline_lines(state, snap, full_detail=detail),
            build_agent_contribution_lines=lambda state, snap, detail: self._build_sprint_agent_contribution_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_issue_lines=lambda state, snap, detail: self._build_sprint_issue_lines(state, snap, full_detail=detail),
            build_achievement_lines=lambda state, snap, detail: self._build_sprint_achievement_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_artifact_lines=lambda state, snap, detail: self._build_sprint_artifact_lines(
                state,
                snap,
                full_detail=detail,
            ),
            build_machine_report_lines=self._build_machine_sprint_report_lines,
        )

    def _render_live_sprint_report_markdown(self, sprint_state: dict[str, Any]) -> str:
        todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        todo_status_counts = self._count_by_key(todos, "status")
        linked_artifacts = collect_sprint_todo_artifact_entries(sprint_state)
        return render_live_sprint_report_markdown_helper(
            sprint_state,
            todo_status_counts=todo_status_counts,
            linked_artifacts=linked_artifacts,
            status_label=self._sprint_status_label(str(sprint_state.get("status") or "")),
            format_count_summary=_format_count_summary,
        )

    def _ensure_markdown_file(self, path: Path, header: str) -> None:
        ensure_markdown_file_helper(path, header)

    def _append_markdown_entry(self, path: Path, header: str, title: str, lines: list[str]) -> None:
        append_markdown_entry_helper(path, header, title, lines)

    def _refresh_role_todos(self) -> None:
        refresh_role_todos_helper(self.paths)

    def _append_role_history(
        self,
        role: str,
        request_record: dict[str, Any],
        *,
        event_type: str,
        summary: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        append_role_history_helper(
            self.paths,
            role,
            request_record,
            event_type=event_type,
            summary=summary,
            result=result,
        )

    def _append_role_journal(
        self,
        role: str,
        request_record: dict[str, Any],
        *,
        title: str,
        lines: list[str],
    ) -> None:
        append_role_journal_helper(
            self.paths,
            role,
            request_record,
            title=title,
            lines=lines,
        )

    def _append_shared_workspace_entry(
        self,
        destination: str,
        *,
        request_record: dict[str, Any],
        title: str,
        lines: list[str],
    ) -> None:
        append_shared_workspace_entry_helper(
            self.paths,
            destination,
            request_record=request_record,
            title=title,
            lines=lines,
        )

    def _record_shared_role_result(self, request_record: RequestRecord, result: RoleResult) -> None:
        record_shared_role_result_helper(self.paths, request_record, result)

    async def _handle_orchestrator_message(self, message: DiscordMessage) -> None:
        await handle_orchestrator_message_helper(self, message)

    async def _handle_non_orchestrator_message(self, message: DiscordMessage) -> None:
        await handle_non_orchestrator_message_helper(self, message)

    async def _handle_user_request(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> None:
        await handle_user_request_helper(self, message, envelope, forwarded=forwarded)

    def _backlog_counts(self) -> dict[str, int]:
        if self._drop_non_actionable_backlog_items():
            self._refresh_backlog_markdown()
        repaired_ids = self._repair_non_actionable_carry_over_backlog_items()
        if repaired_ids:
            self._refresh_backlog_markdown()
        items = self._iter_backlog_items()
        return backlog_status_counts(items)

    @staticmethod
    def _count_by_key(items: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            normalized = str(item.get(key) or "").strip().lower()
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
        return counts

    def _render_backlog_status_report(self) -> str:
        counts = self._backlog_counts()
        context = backlog_status_report_context(self._iter_backlog_items())
        return render_backlog_status_report_helper(
            active_items=context["active_items"],
            counts=counts,
            kind_counts=context["kind_counts"],
            source_counts=context["source_counts"],
            format_count_summary=_format_count_summary,
        )

    def _render_sprint_status_report(
        self,
        sprint_state: dict[str, Any],
        *,
        is_active: bool,
        scheduler_state: dict[str, Any],
    ) -> str:
        selected_items = list(sprint_state.get("selected_items") or [])
        todos = list(sprint_state.get("todos") or [])
        todo_status_counts = self._count_by_key(todos, "status")
        selected_kind_counts = self._count_by_key(selected_items, "kind")
        return render_sprint_status_report_helper(
            sprint_state,
            is_active=is_active,
            scheduler_state=scheduler_state,
            todo_status_counts=todo_status_counts,
            selected_kind_counts=selected_kind_counts,
            format_count_summary=_format_count_summary,
        )

    @staticmethod
    def _extract_sprint_folder_name(sprint_state: dict[str, Any] | None) -> str:
        return extract_sprint_folder_name(sprint_state)

    def _ensure_sprint_folder_metadata(self, sprint_state: dict[str, Any]) -> None:
        folder_name = self._extract_sprint_folder_name(sprint_state)
        if not folder_name:
            return
        sprint_state["sprint_folder_name"] = folder_name
        if not str(sprint_state.get("sprint_folder") or "").strip():
            sprint_state["sprint_folder"] = str(self.paths.sprint_artifact_dir(folder_name))

    def _default_attachment_sprint_folder_name(self) -> str:
        return build_sprint_artifact_folder_name(str(self.runtime_config.sprint_id or ""))

    def _resolve_message_attachment_root(self, message: DiscordMessage) -> Path:
        scheduler_state = self._load_scheduler_state()
        active_sprint_id = str(scheduler_state.get("active_sprint_id") or "").strip()
        active_sprint = self._load_sprint_state(active_sprint_id) if active_sprint_id else {}
        folder_name = self._extract_sprint_folder_name(active_sprint)
        if not folder_name and active_sprint_id:
            folder_name = build_sprint_artifact_folder_name(active_sprint_id)
        if not folder_name and is_manual_sprint_start_text(str(message.content or "")):
            folder_name = build_sprint_artifact_folder_name(
                build_active_sprint_id(now=self._message_received_at(message))
            )
        if not folder_name:
            folder_name = self._default_attachment_sprint_folder_name()
        return self.paths.sprint_attachment_root(folder_name)

    @staticmethod
    def _attachment_storage_relative_path(path: Path) -> Path:
        return attachment_storage_relative_path(path)

    def _sprint_attachment_filename(
        self,
        artifact_hint: str,
        *,
        resolved: Path | None = None,
    ) -> str:
        return sprint_attachment_filename(
            artifact_hint,
            resolved=resolved,
            sprint_artifacts_root=self.paths.sprint_artifacts_root,
        )

    def _relocate_artifacts_to_sprint_folder(
        self,
        artifacts: list[str],
        sprint_state: dict[str, Any],
    ) -> list[str]:
        folder_name = self._extract_sprint_folder_name(sprint_state)
        if not folder_name:
            return [str(item).strip() for item in artifacts if str(item).strip()]
        relocated: list[str] = []
        destination_root = self.paths.sprint_attachment_root(folder_name)
        for artifact in artifacts:
            normalized = str(artifact or "").strip()
            if not normalized:
                continue
            resolved = self._resolve_artifact_path(normalized)
            destination: Path | None = None
            attachment_filename = self._sprint_attachment_filename(normalized, resolved=resolved)
            if attachment_filename:
                destination = destination_root / attachment_filename
            if resolved is None or not resolved.exists():
                if destination is not None and destination.exists():
                    artifact_hint = self._workspace_artifact_hint(destination)
                    if artifact_hint not in relocated:
                        relocated.append(artifact_hint)
                    continue
                if normalized not in relocated:
                    relocated.append(normalized)
                continue
            if destination is None:
                destination = destination_root / self._attachment_storage_relative_path(resolved)
            if resolved.resolve() != destination.resolve():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                shutil.move(str(resolved), str(destination))
                resolved = destination
            artifact_hint = self._workspace_artifact_hint(resolved)
            if artifact_hint not in relocated:
                relocated.append(artifact_hint)
        return relocated

    def _normalize_sprint_reference_attachments(self, sprint_state: dict[str, Any]) -> bool:
        folder_name = self._extract_sprint_folder_name(sprint_state)
        if not folder_name:
            return False
        artifact_fields = ("kickoff_reference_artifacts", "reference_artifacts")
        original_by_field: dict[str, list[str]] = {}
        relocation_candidates: list[str] = []

        for field_name in artifact_fields:
            original_values = [
                str(item).strip()
                for item in (sprint_state.get(field_name) or [])
                if str(item).strip()
            ]
            original_by_field[field_name] = original_values
            for artifact in original_values:
                if not self._sprint_attachment_filename(artifact):
                    continue
                if artifact not in relocation_candidates:
                    relocation_candidates.append(artifact)

        if not relocation_candidates:
            return False

        relocated_values = self._relocate_artifacts_to_sprint_folder(relocation_candidates, sprint_state)
        relocation_map = {
            source: destination
            for source, destination in zip(relocation_candidates, relocated_values)
            if str(destination or "").strip()
        }

        changed = False
        for field_name, original_values in original_by_field.items():
            normalized_values = _dedupe_preserving_order(
                [relocation_map.get(value, value) for value in original_values]
            )[:12]
            if normalized_values != original_values:
                sprint_state[field_name] = normalized_values
                changed = True
        return changed

    def _load_latest_sprint_state(self) -> dict[str, Any]:
        sprint_files = sorted(self.paths.sprints_dir.glob("*.json"))
        if not sprint_files:
            return {}
        return read_json(sprint_files[-1])

    def _load_status_target_sprint(self) -> tuple[dict[str, Any], bool, dict[str, Any]]:
        scheduler_state = self._load_scheduler_state()
        sprint_state = self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
        is_active = bool(sprint_state)
        if not sprint_state:
            sprint_state = self._load_latest_sprint_state()
        return sprint_state, is_active, scheduler_state

    def build_sprint_status_message(self) -> str:
        sprint_state, is_active, scheduler_state = self._load_status_target_sprint()
        if not sprint_state:
            return "기록된 sprint가 없습니다."
        return self._render_sprint_status_report(
            sprint_state,
            is_active=is_active,
            scheduler_state=scheduler_state,
        )

    async def start_sprint_lifecycle(
        self,
        milestone_title: str,
        *,
        trigger: str = "manual_start",
        resume_mode: str = "background",
        started_at: datetime | None = None,
        kickoff_brief: str = "",
        kickoff_requirements: list[str] | None = None,
        kickoff_request_text: str = "",
        kickoff_source_request_id: str = "",
        kickoff_reference_artifacts: list[str] | None = None,
    ) -> str:
        active_sprint = self._load_active_sprint_state()
        if active_sprint:
            return (
                "이미 active sprint가 있습니다.\n"
                f"sprint_id={active_sprint.get('sprint_id') or ''}\n"
                f"phase={active_sprint.get('phase') or 'N/A'}\n"
                f"milestone={active_sprint.get('milestone_title') or 'N/A'}"
            )
        normalized_milestone = str(milestone_title or "").strip()
        if not normalized_milestone:
            return "스프린트를 시작하려면 milestone을 알려주세요. 예: `milestone: sprint workflow initial phase 개선`"
        sprint_state = self._build_manual_sprint_state(
            milestone_title=normalized_milestone,
            trigger=trigger,
            started_at=started_at,
            kickoff_brief=kickoff_brief,
            kickoff_requirements=kickoff_requirements,
            kickoff_request_text=kickoff_request_text,
            kickoff_source_request_id=kickoff_source_request_id,
            kickoff_reference_artifacts=kickoff_reference_artifacts,
        )
        scheduler_state = self._load_scheduler_state()
        scheduler_state["active_sprint_id"] = str(sprint_state.get("sprint_id") or "")
        scheduler_state["last_started_at"] = str(sprint_state.get("started_at") or "")
        scheduler_state["last_trigger"] = trigger
        self._clear_pending_milestone_request(scheduler_state)
        self._save_scheduler_state(scheduler_state)
        self._save_sprint_state(sprint_state)
        self._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="started",
            summary="사용자 milestone 기반 manual sprint를 시작했습니다.",
            payload={"milestone_title": sprint_state.get("milestone_title") or ""},
        )
        sprint_id = str(sprint_state.get("sprint_id") or "")
        if resume_mode == "await":
            await self._resume_active_sprint(sprint_id)
        elif resume_mode == "background":
            asyncio.create_task(self._resume_active_sprint(sprint_id))
        refreshed = self._load_sprint_state(str(sprint_state.get("sprint_id") or "")) or sprint_state
        return (
            "manual sprint initial phase를 시작했습니다.\n"
            f"sprint_id={refreshed.get('sprint_id') or ''}\n"
            f"sprint_name={refreshed.get('sprint_name') or ''}\n"
            f"milestone={refreshed.get('milestone_title') or ''}"
        )

    async def stop_sprint_lifecycle(self, *, resume_mode: str = "background") -> str:
        sprint_state = self._load_active_sprint_state()
        if not sprint_state:
            return "현재 active sprint가 없어 종료할 대상이 없습니다."
        status = str(sprint_state.get("status") or "").strip().lower()
        if status in {"failed", "blocked"}:
            self._finish_scheduler_after_sprint(sprint_state, clear_active=True)
            return (
                "terminal sprint를 종료 처리했습니다.\n"
                f"sprint_id={sprint_state.get('sprint_id') or ''}\n"
                f"status={status or 'N/A'}"
            )
        sprint_state["wrap_up_requested_at"] = utc_now_iso()
        self._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="wrap_up_requested",
            summary="사용자가 현재 sprint 종료를 요청했습니다.",
        )
        self._save_sprint_state(sprint_state)
        sprint_id = str(sprint_state.get("sprint_id") or "")
        if resume_mode == "await":
            await self._resume_active_sprint(sprint_id)
        elif resume_mode == "background":
            asyncio.create_task(self._resume_active_sprint(sprint_id))
        refreshed = self._load_sprint_state(str(sprint_state.get("sprint_id") or "")) or sprint_state
        running_todo = any(
            str(todo.get("status") or "").strip().lower() == "running"
            for todo in refreshed.get("todos") or []
        )
        return (
            "현재 sprint를 wrap up 대상으로 표시했고 즉시 전환을 시도합니다.\n"
            f"sprint_id={refreshed.get('sprint_id') or ''}\n"
            + ("현재 실행 중인 task는 현재 주기에서 마무리 후 전환될 수 있습니다." if running_todo else "곧 wrap up을 시작합니다.")
        )

    async def restart_sprint_lifecycle(self, *, resume_mode: str = "background") -> str:
        scheduler_state = self._load_scheduler_state()
        sprint_state = self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
        if not sprint_state:
            sprint_state = self._load_latest_sprint_state()
        if not sprint_state:
            return "재개할 sprint가 없습니다."
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        status = str(sprint_state.get("status") or "").strip().lower()
        if not sprint_id:
            return "재개할 sprint가 없습니다."
        if status == "completed":
            return f"이미 완료된 sprint라 재개할 수 없습니다. sprint_id={sprint_id}"
        if status in {"failed", "blocked"} and not self._is_resumable_blocked_sprint(sprint_state):
            return f"현재 상태에서는 sprint를 재개할 수 없습니다. sprint_id={sprint_id}\nstatus={status or 'N/A'}"
        sprint_state["resume_from_checkpoint_requested_at"] = utc_now_iso()
        self._append_sprint_event(
            sprint_id,
            event_type="restart_requested",
            summary="사용자가 마지막 execution checkpoint부터 sprint 재개를 요청했습니다.",
        )
        self._save_sprint_state(sprint_state)
        scheduler_state["active_sprint_id"] = sprint_id
        scheduler_state["last_trigger"] = "manual_restart"
        self._save_scheduler_state(scheduler_state)
        if resume_mode == "await":
            await self._resume_active_sprint(sprint_id)
        elif resume_mode == "background":
            asyncio.create_task(self._resume_active_sprint(sprint_id))
        refreshed = self._load_sprint_state(sprint_id) or sprint_state
        return (
            "active sprint 재개를 요청했습니다.\n"
            f"sprint_id={refreshed.get('sprint_id') or ''}\n"
            f"phase={refreshed.get('phase') or 'N/A'}\n"
            f"status={refreshed.get('status') or 'N/A'}"
        )

    async def _handle_manual_sprint_start_request(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> None:
        kickoff_payload = self._extract_manual_sprint_kickoff_payload(envelope)
        milestone_title = str(kickoff_payload.get("milestone_title") or "").strip()
        message_text = await self.start_sprint_lifecycle(
            milestone_title,
            trigger="manual_start",
            resume_mode="background",
            started_at=self._message_received_at(message),
            kickoff_brief=str(kickoff_payload.get("kickoff_brief") or "").strip(),
            kickoff_requirements=list(kickoff_payload.get("kickoff_requirements") or []),
            kickoff_request_text=str(kickoff_payload.get("kickoff_request_text") or "").strip(),
            kickoff_source_request_id=str(kickoff_payload.get("kickoff_source_request_id") or "").strip(),
            kickoff_reference_artifacts=list(kickoff_payload.get("kickoff_reference_artifacts") or []),
        )
        sprint_state = self._load_active_sprint_state()
        if sprint_state and envelope.artifacts:
            relocated_artifacts = self._relocate_artifacts_to_sprint_folder(
                list(envelope.artifacts),
                sprint_state,
            )
            if relocated_artifacts:
                sprint_state["reference_artifacts"] = list(relocated_artifacts)[:12]
                sprint_state["kickoff_reference_artifacts"] = list(relocated_artifacts)[:12]
                self._save_sprint_state(sprint_state)
                self._append_sprint_event(
                    str(sprint_state.get("sprint_id") or ""),
                    event_type="reference_artifacts_linked",
                    summary="사용자가 전달한 sprint reference 문서를 sprint folder에 정리했습니다.",
                    payload={"artifacts": relocated_artifacts},
                )
        await self._reply_to_requester(
            {"reply_route": build_requester_route(message, envelope, forwarded=forwarded)},
            message_text,
        )

    async def _handle_manual_sprint_finalize_request(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> None:
        message_text = await self.stop_sprint_lifecycle(resume_mode="background")
        await self._reply_to_requester(
            {"reply_route": build_requester_route(message, envelope, forwarded=forwarded)},
            message_text,
        )

    def _should_request_sprint_milestone_for_relay_intake(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> bool:
        requester_route = build_requester_route(message, envelope, forwarded=forwarded)
        return should_request_sprint_milestone_for_relay_intake(
            intent=str(envelope.intent or ""),
            requester_route=requester_route,
            relay_channel_id=str(self.discord_config.relay_channel_id or ""),
            has_active_sprint=bool(self._load_active_sprint_state()),
        )

    async def _reinterpret_user_envelope(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
    ) -> MessageEnvelope:
        raw_text = str(message.content or "").strip()
        if not raw_text:
            return envelope
        scheduler_state = self._load_scheduler_state()
        active_sprint = self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
        try:
            classification = await asyncio.to_thread(
                self.intent_parser.classify,
                raw_text=raw_text,
                envelope=envelope,
                scheduler_state=scheduler_state,
                active_sprint=active_sprint,
                backlog_counts=self._backlog_counts(),
                forwarded=False,
            )
        except Exception:
            LOGGER.exception("Intent parser failed for message: %s", raw_text)
            return envelope
        classification = normalize_intent_payload(classification)
        merged_params = dict(envelope.params)
        parser_params = classification.get("params")
        if isinstance(parser_params, dict):
            merged_params.update(parser_params)

        interpreted = MessageEnvelope(
            request_id=str(classification.get("request_id") or envelope.request_id or "").strip() or None,
            sender=envelope.sender,
            target="orchestrator",
            intent=str(classification.get("intent") or envelope.intent or "route").strip().lower() or "route",
            urgency=envelope.urgency,
            scope=str(classification.get("scope") or envelope.scope or "").strip() or envelope.scope,
            artifacts=list(envelope.artifacts),
            params={
                **merged_params,
                "_intent_source": "internal_parser",
                "parser_confidence": str(classification.get("confidence") or "").strip(),
                "parser_reason": str(classification.get("reason") or "").strip(),
            },
            body=str(classification.get("body") or envelope.body or "").strip(),
        )
        LOGGER.info(
            "Internal parser classified intake: intent=%s scope=%s request_id=%s confidence=%s",
            interpreted.intent,
            interpreted.scope,
            interpreted.request_id or "",
            interpreted.params.get("parser_confidence") or "",
        )
        return interpreted

    @staticmethod
    def _request_handling_mode(result: dict[str, Any]) -> str:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        handling = dict(proposals.get("request_handling") or {}) if isinstance(proposals.get("request_handling"), dict) else {}
        return str(handling.get("mode") or "").strip().lower()

    @staticmethod
    def _control_action_from_result(result: dict[str, Any]) -> dict[str, Any]:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        action = proposals.get("control_action")
        return dict(action) if isinstance(action, dict) else {}

    def _cancel_request_by_id(self, request_id: str) -> str:
        request_record = self._load_request(request_id)
        if not request_record:
            return "취소할 request_id를 찾을 수 없습니다."
        if str(request_record.get("status") or "").strip().lower() == "uncommitted":
            version_control_paths = [
                str(item).strip()
                for item in (
                    request_record.get("version_control_paths")
                    or request_record.get("task_commit_paths")
                    or []
                )
                if str(item).strip()
            ]
            warning = (
                f"요청은 아직 uncommitted 상태라 취소할 수 없습니다. request_id={request_record['request_id']}\n"
                "task-owned 변경이 남아 있으니 version_controller recovery 또는 수동 git 정리가 필요합니다."
            )
            if version_control_paths:
                warning += "\nremaining_paths=" + ", ".join(version_control_paths)
            return warning
        request_record["status"] = "cancelled"
        append_request_event(
            request_record,
            event_type="cancelled",
            actor="orchestrator",
            summary="사용자 요청으로 취소되었습니다.",
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="cancelled",
            summary="사용자 요청으로 취소되었습니다.",
        )
        return "요청을 취소했습니다."

    async def _run_registered_action_for_request(
        self,
        request_record: dict[str, Any],
        *,
        action_name: str,
        params: dict[str, Any],
    ) -> dict[str, str]:
        if not action_name:
            return {"status": "failed", "summary": "action_name이 필요합니다."}
        if action_name not in self.runtime_config.actions:
            return {"status": "failed", "summary": f"등록되지 않은 action입니다: {action_name}"}
        try:
            execution = await asyncio.to_thread(
                self.action_executor.execute,
                request_id=request_record["request_id"],
                action_name=action_name,
                params=params,
            )
        except Exception as exc:
            return {"status": "failed", "summary": str(exc)}
        request_record["operation_id"] = execution["operation_id"]
        request_record["action_status"] = execution["status"]
        append_request_event(
            request_record,
            event_type="action_execute",
            actor="orchestrator",
            summary=f"{action_name} 액션을 실행했습니다.",
            payload=execution,
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="action_execute",
            summary=f"{action_name} 액션을 실행했습니다.",
        )
        return {
            "status": "failed" if str(execution.get("status") or "").strip().lower() == "failed" else "completed",
            "summary": str(execution.get("report") or "액션을 실행했습니다."),
        }

    async def _apply_control_action(self, request_record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        action = self._control_action_from_result(result)
        if not action:
            return {}
        kind = str(action.get("kind") or "").strip().lower()
        if kind == "sprint_lifecycle":
            command = str(action.get("command") or "").strip().lower()
            milestone_title = str(action.get("milestone_title") or "").strip()
            if command == "start":
                summary = await self.start_sprint_lifecycle(
                    milestone_title,
                    trigger="manual_start",
                    resume_mode="background",
                    started_at=self._request_started_at_hint(request_record),
                    kickoff_brief=str(action.get("kickoff_brief") or "").strip(),
                    kickoff_requirements=_normalize_string_list(action.get("kickoff_requirements")),
                    kickoff_request_text=str(action.get("kickoff_request_text") or "").strip(),
                    kickoff_source_request_id=str(action.get("kickoff_source_request_id") or "").strip(),
                    kickoff_reference_artifacts=_normalize_string_list(action.get("kickoff_reference_artifacts")),
                )
            elif command == "stop":
                summary = await self.stop_sprint_lifecycle(resume_mode="background")
            elif command == "restart":
                summary = await self.restart_sprint_lifecycle(resume_mode="background")
            elif command == "status":
                summary = self.build_sprint_status_message()
            else:
                summary = f"지원하지 않는 sprint lifecycle command입니다: {command or 'N/A'}"
            return {
                "status": "completed",
                "summary": summary,
                "force_complete": True,
            }
        if kind == "cancel_request":
            target_request_id = str(action.get("request_id") or "").strip()
            summary = self._cancel_request_by_id(target_request_id)
            if summary == "취소할 request_id를 찾을 수 없습니다.":
                status = "failed"
                reply_status = "failed"
            elif "취소할 수 없습니다." in summary or "uncommitted 상태" in summary:
                status = "blocked"
                reply_status = "blocked"
            else:
                status = "completed"
                reply_status = "cancelled"
            return {
                "status": status,
                "reply_status": reply_status,
                "request_id": target_request_id,
                "summary": summary,
                "force_complete": True,
            }
        if kind == "execute_action":
            action_name = str(action.get("action_name") or "").strip()
            params = dict(action.get("params") or {}) if isinstance(action.get("params"), dict) else {}
            action_result = await self._run_registered_action_for_request(
                request_record,
                action_name=action_name,
                params=params,
            )
            return {
                "status": str(action_result.get("status") or "completed").strip().lower() or "completed",
                "summary": str(action_result.get("summary") or "").strip(),
                "force_complete": True,
            }
        return {
            "status": "failed",
            "summary": f"지원하지 않는 control_action입니다: {kind or 'N/A'}",
            "force_complete": True,
        }

    async def _apply_role_result(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> None:
        await apply_role_result_helper(
            self,
            request_record,
            result,
            sender_role=sender_role,
        )
    async def _run_local_orchestrator_request(self, request_record: dict[str, Any]) -> None:
        await run_local_orchestrator_request_helper(self, request_record)

    async def _handle_delegated_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        await handle_delegated_request_helper(self, message, envelope)

    async def _process_delegated_request(self, envelope: MessageEnvelope, request_record: dict[str, Any]) -> None:
        await process_delegated_request_helper(self, envelope, request_record)
    async def _handle_role_report(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        await handle_role_report_helper(self, message, envelope)

    async def _forward_user_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        await forward_user_request_helper(self, message, envelope)

    async def _reply_status_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        await reply_status_request_helper(self, message, envelope)

    async def _cancel_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        await cancel_request_helper(self, message, envelope)

    async def _execute_registered_action(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        await execute_registered_action_helper(self, message, envelope)

    async def _delegate_request(self, request_record: dict[str, Any], next_role: str) -> bool:
        return await delegate_request_helper(self, request_record, next_role)
    def _record_relay_delivery(
        self,
        request_record: dict[str, Any],
        *,
        status: str,
        target_description: str,
        attempts: int,
        error: str,
        envelope: MessageEnvelope,
    ) -> None:
        history_summary = record_relay_delivery_helper(
            request_record,
            status=status,
            target_description=target_description,
            attempts=attempts,
            error=error,
            envelope=envelope,
            updated_at=utc_now_iso(),
        )
        if history_summary:
            self._append_role_history(
                "orchestrator",
                request_record,
                event_type="relay_send_failed",
                summary=history_summary,
            )
        self._save_request(request_record)

    def _build_internal_relay_record_id(self, envelope: MessageEnvelope) -> str:
        return build_internal_relay_record_id(envelope)

    def _enqueue_internal_relay(self, envelope: MessageEnvelope) -> str:
        return enqueue_internal_relay(
            self.paths,
            sender_role=self.role,
            envelope=envelope,
            transport=RELAY_TRANSPORT_INTERNAL,
        )

    def _archive_internal_relay_file(self, relay_file: Path, *, invalid: bool = False) -> None:
        archive_internal_relay_file(
            self.paths,
            role=self.role,
            relay_file=relay_file,
            invalid=invalid,
        )

    @staticmethod
    def _deserialize_internal_relay_envelope(payload: Any) -> MessageEnvelope | None:
        return deserialize_internal_relay_envelope(payload)

    def _build_internal_relay_message_stub(
        self,
        envelope: MessageEnvelope,
        *,
        relay_id: str = "",
    ) -> DiscordMessage:
        sender_role = str(envelope.sender or "").strip()
        requester = extract_original_requester(dict(envelope.params or {}))
        sender_bot_id = ""
        sender_config = self.discord_config.agents.get(sender_role)
        if sender_config is not None:
            sender_bot_id = str(sender_config.bot_id or "").strip()
        return render_internal_relay_message_stub(
            envelope,
            current_role=self.role,
            relay_channel_id=self.discord_config.relay_channel_id,
            original_requester=requester,
            sender_bot_id=sender_bot_id,
            relay_id=relay_id,
        )

    async def _process_internal_relay_envelope(
        self,
        envelope: MessageEnvelope,
        *,
        relay_id: str = "",
    ) -> None:
        await process_internal_relay_envelope_helper(
            envelope,
            current_role=self.role,
            relay_id=relay_id,
            build_internal_relay_message_stub=self._build_internal_relay_message_stub,
            handle_role_report=self._handle_role_report,
            handle_user_request=self._handle_user_request,
            handle_delegated_request=self._handle_delegated_request,
            log_malformed_trusted_relay=self._log_malformed_trusted_relay,
        )

    async def _consume_internal_relay_once(self) -> None:
        await consume_internal_relay_once_helper(
            paths=self.paths,
            role=self.role,
            archive_internal_relay_file=self._archive_internal_relay_file,
            process_internal_relay_envelope=self._process_internal_relay_envelope,
            log_exception=LOGGER.exception,
        )

    async def _consume_internal_relay_loop(self) -> None:
        await consume_internal_relay_loop_helper(
            role=self.role,
            consume_internal_relay_once=self._consume_internal_relay_once,
            poll_seconds=INTERNAL_REQUEST_POLL_SECONDS,
            log_exception=LOGGER.exception,
        )

    @staticmethod
    def _parse_json_payload_from_text(raw_text: str) -> Any:
        normalized = str(raw_text or "").strip()
        if not normalized:
            return {}
        try:
            return json.loads(normalized)
        except json.JSONDecodeError:
            parsed_dict = _parse_report_body_json(normalized)
            if parsed_dict:
                return parsed_dict
        return {}

    @staticmethod
    def _extract_semantic_leaf_lines(
        value: Any,
        *,
        prefix: str = "",
        skip_keys: set[str] | None = None,
    ) -> list[str]:
        return extract_semantic_leaf_lines_helper(value, prefix=prefix, skip_keys=skip_keys)

    def _proposal_semantic_details(
        self,
        proposals: dict[str, Any],
        *,
        payload_names: tuple[str, ...],
        transition: dict[str, Any],
    ) -> dict[str, list[str]]:
        return proposal_semantic_details_helper(
            proposals,
            payload_names=payload_names,
            transition=transition,
        )

    @staticmethod
    def _planner_backlog_titles(proposals: dict[str, Any], *, limit: int = 3) -> list[str]:
        return planner_backlog_titles_helper(proposals, limit=limit)

    @staticmethod
    def _planner_doc_targets(proposals: dict[str, Any], *, limit: int = 3) -> list[str]:
        return planner_doc_targets_helper(proposals, limit=limit)

    def _build_role_result_semantic_context(self, result: dict[str, Any]) -> dict[str, Any]:
        return build_role_result_semantic_context_helper(result)

    def _summarize_relay_body(self, envelope: MessageEnvelope) -> list[str]:
        return summarize_relay_body_helper(self, envelope)

    def _build_internal_relay_summary_message(self, envelope: MessageEnvelope) -> str:
        return render_internal_relay_summary_message(
            envelope,
            marker=INTERNAL_RELAY_SUMMARY_MARKER,
            summary_lines=self._summarize_relay_body(envelope),
        )

    async def _send_internal_relay_summary(self, envelope: MessageEnvelope) -> None:
        summary = self._build_internal_relay_summary_message(envelope)
        await self.notification_service.send_internal_relay_summary(
            relay_channel_id=str(self.discord_config.relay_channel_id or "").strip(),
            content=summary,
            request_id=str(envelope.request_id or ""),
        )

    async def _send_relay(self, envelope: MessageEnvelope, *, request_record: RequestRecord | None = None) -> bool:
        return await send_relay_transport_helper(
            envelope,
            request_record=request_record,
            use_internal_relay=self._is_internal_relay_enabled(),
            current_role=self.role,
            relay_channel_id=str(self.discord_config.relay_channel_id or "").strip(),
            target_bot_id=str(self.discord_config.get_role(envelope.target).bot_id or "").strip(),
            enqueue_internal_relay=self._enqueue_internal_relay,
            send_internal_relay_summary=self._send_internal_relay_summary,
            send_discord_relay_envelope=self.notification_service.send_relay_envelope,
            record_relay_delivery=self._record_relay_delivery,
            log_warning=LOGGER.warning,
        )

    def _build_requester_status_message(
        self,
        *,
        status: str,
        request_id: str,
        summary: str,
        related_request_ids: list[str] | None = None,
    ) -> str:
        return build_requester_status_message_helper(
            self.notification_service,
            status=status,
            request_id=request_id,
            summary=summary,
            related_request_ids=related_request_ids,
        )

    async def _reply_to_requester(self, request_record: RequestRecord, content: str) -> None:
        await reply_to_requester_helper(
            self.notification_service,
            request_record,
            content,
            save_request=self._save_request,
        )

    async def _send_channel_reply(self, message: DiscordMessage, content: str) -> None:
        await send_channel_reply_helper(self.notification_service, message, content)

    async def _send_immediate_receipt(self, message: DiscordMessage) -> None:
        await send_immediate_receipt_helper(
            self.notification_service,
            message,
            is_trusted_relay_message=self._is_trusted_relay_message,
        )

    def _build_runtime_signature_suffix(self) -> str:
        return self.notification_service.build_runtime_signature_suffix()

    def _append_runtime_signature(self, content: str) -> str:
        return self.notification_service.append_runtime_signature(content)

    def _cross_process_send_lock(self):
        return self.notification_service.cross_process_send_lock()

    async def _send_discord_content(
        self,
        *,
        content: str,
        send,
        target_description: str,
        prefix: str = "",
        swallow_exceptions: bool = False,
        log_traceback: bool = True,
    ) -> None:
        await send_discord_content_helper(
            self.notification_service,
            content=content,
            send=send,
            target_description=target_description,
            prefix=prefix,
            swallow_exceptions=swallow_exceptions,
            log_traceback=log_traceback,
        )

    def _create_request_record(self, message: DiscordMessage, envelope: MessageEnvelope, *, forwarded: bool) -> RequestRecord:
        request_id = envelope.request_id or new_request_id()
        source_message_created_at = self._message_received_at(message)
        created_at = utc_now_iso()
        record = build_created_request_record_helper(
            message,
            envelope,
            forwarded=forwarded,
            request_id=request_id,
            sprint_id=self.runtime_config.sprint_id,
            source_message_created_at=source_message_created_at.isoformat() if source_message_created_at else "",
            created_at=created_at,
            updated_at=created_at,
        )
        self._save_request(record)
        self._append_role_history(
            "orchestrator",
            record,
            event_type="created",
            summary="요청을 접수했습니다.",
        )
        return record

    def _find_duplicate_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> RequestRecord | None:
        fingerprint = build_duplicate_request_fingerprint(message, envelope)
        for record in iter_request_records(self.paths):
            if is_terminal_request(record):
                continue
            if record.get("fingerprint") == fingerprint:
                return record
        return None

    def _load_request(self, request_id: str) -> RequestRecord:
        return load_request(self.paths, request_id)

    @staticmethod
    def _unlink_if_exists(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _purge_request_scoped_role_output_files(self) -> None:
        request_ids = {
            path.stem
            for path in self.paths.requests_dir.glob("*.json")
            if path.is_file() and path.stem
        }
        if not request_ids:
            return
        for role in TEAM_ROLES:
            for request_id in request_ids:
                self._unlink_if_exists(self.paths.role_sources_dir(role) / f"{request_id}.md")
                self._unlink_if_exists(self.paths.role_sources_dir(role) / f"{request_id}.json")
                self._unlink_if_exists(self.paths.runtime_root / "role_reports" / role / f"{request_id}.md")
                self._unlink_if_exists(self.paths.runtime_root / "role_reports" / role / f"{request_id}.json")

    def _save_request(self, request_record: RequestRecord) -> None:
        save_request(self.paths, request_record, update_timestamp=True)
        self._refresh_role_todos()

    def _persist_request_result(self, request_record: RequestRecord) -> None:
        save_request(self.paths, request_record, update_timestamp=True)

    @staticmethod
    def _is_terminal_request_status(status: str) -> bool:
        return str(status or "").strip().lower() in {"completed", "committed", "cancelled", "failed"}

    def _stale_role_report_reason(
        self,
        request_record: dict[str, Any],
        envelope: MessageEnvelope,
        result: dict[str, Any],
    ) -> str:
        request_status = str(request_record.get("status") or "").strip().lower()
        workflow_state = self._request_workflow_state(request_record)
        workflow_phase = str(workflow_state.get("phase") or "").strip().lower()
        workflow_phase_status = str(workflow_state.get("phase_status") or "").strip().lower()
        sender_role = str(envelope.sender or "").strip().lower()
        result_role = str(result.get("role") or "").strip().lower()
        report_role = sender_role or result_role
        current_role = str(request_record.get("current_role") or "").strip().lower()

        if self._is_terminal_request_status(request_status) or (
            workflow_phase == WORKFLOW_PHASE_CLOSEOUT and workflow_phase_status == "completed"
        ):
            return "request is already closed"
        if report_role and current_role and report_role != current_role:
            return f"request currently expects {current_role}, not {report_role}"
        return ""

    @staticmethod
    def _proposal_nested_string_list(
        proposals: dict[str, Any],
        payload_names: tuple[str, ...],
        field_name: str,
    ) -> list[str]:
        return proposal_nested_string_list_helper(proposals, payload_names, field_name)

    @staticmethod
    def _delegate_task_text(request_record: dict[str, Any]) -> str:
        return delegate_task_text_helper(request_record)

    def _synthesize_latest_role_context(self, result: dict[str, Any]) -> dict[str, Any]:
        return synthesize_latest_role_context_helper(self, result)

    def _build_delegation_context(self, request_record: dict[str, Any], next_role: str) -> dict[str, Any]:
        return build_delegation_context_helper(self, request_record, next_role)

    def _build_delegate_body(self, request_record: dict[str, Any], delegation_context: dict[str, Any]) -> str:
        return build_delegate_body_helper(self, request_record, delegation_context)

    def _format_role_request_snapshot_markdown(
        self,
        *,
        role: str,
        request_record: dict[str, Any],
        delegation_context: dict[str, Any],
    ) -> str:
        return format_role_request_snapshot_markdown_helper(
            self,
            role=role,
            request_record=request_record,
            delegation_context=delegation_context,
        )

    def _write_role_request_snapshot(
        self,
        role: str,
        request_record: dict[str, Any],
        delegation_context: dict[str, Any],
    ) -> str:
        return write_role_request_snapshot_helper(self, role, request_record, delegation_context)

    def _build_delegate_envelope(
        self,
        request_record: dict[str, Any],
        next_role: str,
        *,
        delegation_context: dict[str, Any] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> MessageEnvelope:
        return build_delegate_envelope_helper(
            self,
            request_record,
            next_role,
            delegation_context=delegation_context,
            extra_params=extra_params,
        )

    def _initial_role_for_request(self, request_record: dict[str, Any]) -> str:
        return self._intent_to_role(str(request_record.get("intent") or "route"))

    def _intent_to_role(self, intent: str) -> str:
        return intent_to_role_map(self.agent_utilization_policy).get(str(intent).strip().lower(), "planner")

    def _intent_for_role(self, role: str, fallback_intent: str) -> str:
        if role in TEAM_ROLES:
            return self._agent_capability(role).default_intent
        return fallback_intent
