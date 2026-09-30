# Girls' Creation Translation

This repository uses an agent-driven translation workflow. The Codex Agent is
the translator; the Python workflow is deterministic project plumbing.

Read the target work directory's `AGENTS.md` and `agent-prompt.md` first. `next`
only returns a resource list; read source material on demand so the Agent can
choose its own context. The Agent owns its conversation, context, and tool use. Do not implement an Agent,
API request loop, token budget, session scheduler, or per-window process. A
reading window only groups material. Adapters collect and extract real source
text; they must not invent speakers, titles, summaries, or translations.

During translation, use the supplied reading and submission commands, preserve
the exact output dictionary path, and keep drafts under the work directory. Do
not run sync, plan, publish, Git operations, or edit framework/configuration
files. Names and configured term sources are canonical dictionaries; speakers
are context only.
