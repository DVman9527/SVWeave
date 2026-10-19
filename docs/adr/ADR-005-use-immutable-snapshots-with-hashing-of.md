# ADR-005: Use immutable snapshots with hashing of all relevant inputs, not just the Git SHA

- Status: Accepted
- Date: 2026-10-13 (M0)

## Context
Uncommitted edits, include files, defines and filelists all change analysis results. pyslang reports the files a build actually read (SourceManager buffers: 6 files for the demo build, 169 unique files for the UVM build).

## Decision
Each analysis runs against an immutable snapshot whose identity covers the content hashes of every input file, including headers discovered through the actual dependency list, plus the normalized manifest.

## Consequences
Snapshot creation copies or hard-links files. M0 used plain copies under .svweave/spike/. The dependency list must be deduplicated by full path, because slang creates a new buffer for every `include.
