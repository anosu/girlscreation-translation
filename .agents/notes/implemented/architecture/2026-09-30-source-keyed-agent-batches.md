# Agent Note: Submit translations by resource and original text

Status: implemented

## Problem

The previous Agent workflow kept bounded reading windows and short submission numbers after it had already moved to one Codex session. The Agent had to claim each window, render source through a custom helper, submit numbered values, and finish the window. Thousands of per-key answer files added repeated filesystem work. This duplicated Codex's own reading and context management while making translation depend on a scheduler-shaped interface.

## Decision

The adapter still extracts source faithfully. The framework still snapshots source, plans exact missing output keys, validates submissions, and publishes only after complete preflight. The plan no longer assigns reading windows. It lists resource snapshots and tasks, leaving order, context inspection, and optional subagent use to one Codex session.

The Agent submits batches shaped as resource ID to exact original-text/translation pairs. The framework maps each pair to its output file and dictionary path, validates it, and atomically updates one answers.json per target. A repeated original in the same dictionary location shares one task; identical originals in different locations remain independent. A later batch may correct an accepted value. One final check verifies the whole plan and source snapshot before publication. Existing names and configured terminology still guide the Agent; the old term-proposal command is not part of the translation path.

Repository AGENTS.md holds durable project rules. The generated task prompt contains the current plan location, output protocol, style, and quality checks. Codex retains its built-in instructions and manages its own tools and context; no model window size or resource truncation is configured by the framework.

## Alternatives considered

One model process per work packet isolated failures but repeatedly paid startup and instruction costs. One process consuming internal windows reduced that cost, yet retained navigation and numbering overhead with no translation benefit. Having the Agent edit published dictionaries directly would be simpler on the surface but would lose a reliable pre-publication check and make partial failures harder to distinguish from accepted output. Per-key answer files made small writes easy but made large plans expensive to reload and maintain.

## Consequences

The interface has two routine commands, submit and status, and no next/read/search/finish/revise/propose lifecycle. Large jobs can submit multiple resources per call and resume from the single answer file. The work-cache and plan versions advance; canceled old window answers are not migrated. Existing published dictionaries and adapter resources do not change. Full-source reads can still be large, but Codex chooses its reading strategy rather than receiving framework-imposed truncation.
