# CLAUDE.md

Claude Code reads this file to learn how to work in this repository. The
canonical, tool-agnostic engineering rules now live in `AGENTS.md`; read that
first and treat it as the source of truth for all MeshtasticPass engineering
doctrine (architecture, identity rules, persistence, delivery-state
monotonicity, RF/config safety, simulation, async/correlation safety, reconnect
lifecycle, MESH truthfulness, theme/style conventions, test strategy,
validation commands, and git/PR workflow).

The only Claude-specific note worth keeping here: this file is the Claude Code
entry point only. Every durable rule -- including "never merge or open a pull
request unless explicitly instructed" and "report hardware-only validation
honestly" -- is in `AGENTS.md`, and nothing in this file overrides it.
