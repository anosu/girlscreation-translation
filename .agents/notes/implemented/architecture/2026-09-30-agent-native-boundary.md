# Agent Note: Keep context ownership with the translation Agent

Status: implemented

## Problem

The workflow had two misleading controls: a hard character cap clipped the
resource shown at window bootstrap, and configuration exposed a model context
window. Both made the framework look like an API request scheduler even though
translation runs inside one Codex Agent session with its own context handling.

## Decision

The Agent owns its conversation, context, and continuity. The framework does
not set a model context budget or start a new process for a reading window.
Bootstrap contains only the selected resource list. The Agent reads each
resource on demand; an unbounded `read` result is complete by default. The
`read` command still supports explicit character pagination for an Agent that
chooses to limit one tool result; its `complete` and `next_offset` fields make
that choice observable. Repository and work-directory `AGENTS.md` files state this contract
and keep translation instructions separate from deterministic workflow code.

## Alternatives considered

Keeping a framework-side context cap would bound command output, but it can
silently hide part of a VN scene and duplicates the Agent's context policy.
Removing pagination entirely would avoid that ambiguity, but an explicit option
is useful when a caller deliberately requests smaller terminal output.

## Consequences

Agents receive complete source material by default and can decide when paging
is useful. Projects no longer carry a context-size setting that the framework
must interpret. Very large resources may produce a large bootstrap message;
that is an Agent/runtime concern, while the tool protocol remains explicit and
recoverable. The change is a breaking configuration change: remove
`backends.<name>.codex.context_window`.
