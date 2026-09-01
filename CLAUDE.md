# CLAUDE.md

Claude Code reads this file to learn how to work in this repository. The
canonical, tool-agnostic engineering rules live in `AGENTS.md`, imported here
so Claude Code loads them directly rather than being merely asked to go read
them:

@AGENTS.md

Treat `AGENTS.md` as the source of truth for all MeshtasticPass engineering
doctrine (architecture, identity rules, persistence, delivery-state
monotonicity, RF/config safety, simulation, async/correlation safety, reconnect
lifecycle, MESH truthfulness, theme/style conventions, test strategy, CI
behavior, validation commands, and git/PR workflow).

The only Claude-specific note worth keeping here: this file is the Claude Code
entry point only. Every durable rule -- including "do not merge your own pull
request unless explicitly instructed" and "report hardware-only validation
honestly" -- is in `AGENTS.md`, and nothing in this file overrides it.
