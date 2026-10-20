<!-- svweave:begin -->
## SVWeave MCP (SystemVerilog code intelligence)

This project has the SVWeave MCP server registered as `svweave` (14 `svw_*` tools; static slang
analysis of one immutable build, no simulation, no EDA tool). The specialist that knows how to use
it is `.claude/agents/svweave-mcp.md` (`svweave-mcp`).

* **Use it for** SystemVerilog/UVM structure questions: instance hierarchy, port connections,
  where something is declared or referenced, what a change or a `git diff` touches, planning a
  rename, the static SVA/bind inventory, static build health. Route these to `svweave-mcp`
  instead of grepping the tree.
* **Do not use it for** simulation or waveform/FSDB debug, formal, coverage closure, timing/CDC,
  UVM factory/`config_db` resolution, or anything that needs an EDA result: SVWeave reports those
  as `NOT_RUN`.

Two hard rules for every AI in this project:

1. A `PARTIAL`, `STALE`, `HISTORICAL` or `truncated` SVWeave answer is never proof of absence;
   cite `build_id`, completeness and freshness with each conclusion.
2. An AI can never approve an edit. It may `prepare`/`validate`/`abort` through `svw_edit` (only
   when editing is enabled, off by default); a human runs `svweave edit approve` in a real
   terminal. Never edit sources behind the gateway's back when a controlled edit was requested.
<!-- svweave:end -->
