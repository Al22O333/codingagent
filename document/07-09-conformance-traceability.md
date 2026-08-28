# Coding Agent v1 — 07–09 Conformance Traceability

This Lean mapping records deterministic implementation evidence for the frozen
07–09 contracts. The architecture documents remain the requirement owners.

## 07 — Context and Prompt Policy

| Requirement | Implementation | Deterministic evidence |
| --- | --- | --- |
| Stable Base Prompt and semantic guidance | `src/coding_agent/prompt.py` | `tests/test_context.py` |
| Effective System Prefix ordering and corrective guidance | `prompt.py`, `context.py`, `runtime.py` | `test_context.py`, `test_runtime_limits_and_recovery.py` |
| Completed-Run task/final continuity only | `context.py` | `test_context.py`, `test_runtime.py` |
| Atomic ToolCall/ToolResult eviction and protected latest unit | `context.py` | `test_context.py` |
| Sticky per-Run `history_incomplete` and truncation notice | `context.py`, `prompt.py` | `test_context.py`, `test_observability.py` |
| Mandatory context overflow becomes terminal Runtime failure | `context.py`, `runtime.py` | `test_context.py`, `test_runtime.py` |
| Per-Tool bounded model projection without outcome/call-id changes | `projection.py` | `test_projection.py`, `test_runtime_tool_dispatch.py` |
| Shell streams use independent bounded head/marker/tail projection | `projection.py` | `test_projection.py` |
| Runtime Secret does not re-enter ordinary model observations | `cli.py`, `shell.py`, `runtime.py` | `test_cli.py`, `test_observability.py` |

## 08 — Verification and Testing Policy

| Requirement | Implementation / evidence path | Deterministic evidence |
| --- | --- | --- |
| Provider-neutral protocols and strict Tool schemas | `protocol.py`, `tooling.py` | `test_protocol.py`, `test_tooling.py` |
| Concrete ModelClient serialization, normalization, usage, and errors | `openai_client.py` | `test_openai_client.py` |
| Runtime loop, batch fail-stop, budgets, retry, and corrective recovery | `runtime.py` | `test_runtime*.py` |
| Workspace containment, symlink handling, and path classification | `workspace.py` | `test_workspace.py` |
| Direct bounded Tool contracts | Tool modules | `test_read_file.py`, `test_discovery.py`, `test_search_text.py`, `test_edit_file.py`, `test_create_file.py`, `test_shell.py` |
| ALLOW / CONFIRM / DENY and explicit-constraint no-side-effect paths | `policy.py`, `constraints.py`, `runtime.py` | `test_policy.py`, `test_constraints.py`, `test_permission_confirmation.py`, `test_runtime_tool_dispatch.py` |
| Clarification and exact-action confirmation lifecycle | `runtime.py`, `interaction.py` | `test_ask_user.py`, `test_permission_confirmation.py` |
| Full coding loop observes failed verification, adjusts, then passes | Runtime plus real local Tools | `test_deterministic_e2e.py` |
| Interrupted Run cleanup permits a later same-Session Tool loop | `runtime.py`, `context.py` | `test_runtime_terminal_failures.py`, `test_deterministic_e2e.py` |
| Live provider smoke remains explicitly configured and optional in ordinary CI | `test_openai_client_integration.py` | opt-in smoke evidence; real-model acceptance belongs to M4 |

## 09 — CLI, Observability, and Configuration

| Requirement | Implementation | Deterministic evidence |
| --- | --- | --- |
| CLI-over-environment-over-default validated configuration | `config.py`, `cli.py` | `test_config.py`, `test_cli.py` |
| Required provider/workspace startup invariants fail closed | `config.py`, `workspace.py`, `openai_client.py`, `cli.py` | `test_config.py`, `test_workspace.py`, `test_openai_client.py`, `test_cli.py` |
| Concrete budgets, Shell backend/timeout, and Tool limits | `config.py`, `cli.py`, Tool modules | `test_config.py`, `test_cli.py`, direct Tool tests |
| Optional synchronous read-only observer with isolated failure | `runtime.py` | `test_observability.py` |
| Bounded Secret-safe Normal/Debug rendering and normalized usage only | `cli.py`, `runtime.py` | `test_cli.py`, `test_observability.py` |
| Distinct clarification and exact-action permission presentation | `cli.py`, `runtime.py` | `test_cli.py`, interaction tests |
| Empty input, exit commands, EOF, and top-level interruption | `cli.py` | `test_cli.py` |
| FAILED/CANCELLED/COMPLETED Runs return to the interactive Session | `cli.py`, `runtime.py`, `context.py` | `test_cli.py`, `test_runtime_terminal_failures.py`, `test_deterministic_e2e.py` |

## Acceptance Boundary

The deterministic suite establishes contract and full-path implementation
evidence. It does not claim model quality, universal language support, OS-level
sandboxing, or real-provider availability. Live-provider and representative
real-model acceptance remain explicit M4 activities and do not replace these
deterministic tests.
