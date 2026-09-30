# Girls' Creation Translation

This repository uses an agent-driven translation workflow. The Codex Agent is
the translator; the Python workflow is deterministic project plumbing.

The task prompt names the plan and source snapshot. The Agent chooses how to
read complete scenes and group submissions by resource, using exact original
text as keys. Keep drafts under the work directory and use validated submission
commands; publication preserves each resource's output dictionary path.
The Agent owns its conversation, context, and tool use. Do not implement an
Agent, API request loop, token budget, or session scheduler. Adapters collect
real source text; they do not invent speakers, titles, summaries, or translations.

The Agent may use native subagents for independent work when useful; the main
Agent owns terminology, review, shared writes, and submission. Do not run
sync, plan, publish, Git operations, or edit framework/configuration files.
Names and configured term sources are canonical dictionaries; speakers are
context only.
