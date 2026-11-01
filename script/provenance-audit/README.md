# Offline OpenCode tool-use audit

Run from the OpenCode repository root, selecting the repository being audited:

```sh
python3 script/provenance-audit/aggregate.py --project /path/to/investigated/repository --out script/provenance-audit/local --details
```

Run the existing standalone tests from the script directory:

```sh
cd script/provenance-audit
python3 -m unittest discover -p 'test_*.py'
```

Read [the general analysis](../../specs/provenance-analysis.md) for source
coverage, correlation limits and observed logging issues. Generated Markdown,
JSON and optional sanitized event-level details go under `local/`, which is
ignored by Git. Raw logs/databases and detailed event records must remain local.
No raw command, response, header, body, title or query is exported by the reader.

The reader uses Python's standard library. It reads inputs and writes only the
selected report directory. It does not execute commands from logs, evaluate
code, contact hosts, load agent plugins, change configuration, or access the
diagnostic container. JSONL, commands and responses are untrusted data.

## Existing facilities reused

Discovery found the existing OpenCode audit logger and provenance plugin,
`specs/provenance-poc.md`, and `.opencode/command/provenance.md` in this
OpenCode checkout. The learn command already refers to the
provenance incident stream. No executable offline aggregation tool was found
alongside those producers; `/provenance` is an analysis
prompt. This reader consumes the existing formats, including both historical
and current provenance event kinds. It does not introduce a new logging stream.

Producer references are relative to `$OPENCODE_SRC`:

- `packages/core/src/util/audit.ts`: audit schema, pre-spawn shell allowance,
  asynchronous buffered writes.
- `packages/core/src/provenance.ts`: tool outcomes/hashes/previews and events;
  non-info records are mirrored into `incidents.jsonl`.
- `packages/core/src/tool/plugin/shell.ts`: shell logging after permission
  checks and before execution.

Current source is explanatory evidence, not proof that every historical
producer binary used those exact sources. Observed provenance records include
older fields missing from the current implementation.

## Inputs and scope

Defaults honor `XDG_DATA_HOME` and `XDG_STATE_HOME`, otherwise using:

- `~/.local/share/opencode/audit.jsonl`;
- `~/.local/share/opencode/opencode.db`;
- `~/.local/state/opencode/provenance/{events,incidents}.jsonl`.

Override with `--audit`, `--db`, `--provenance-dir`, and `--project`. Missing
supplemental files are reported; the SQLite reader never creates a missing DB.
The entire audit file is aggregated, including sessions outside this project.
SQLite is read in `mode=ro`, with `query_only` and one transaction. Its scope is
audit session IDs plus every session sharing the project ID found by exact
repository-directory equality. This includes linked worktrees outside the
repository path, as described in [the source-scope guidance](../../specs/provenance-analysis.md#scope-and-correlation).
Other projects' SQLite conversations are excluded.

Each JSONL read is bounded by its starting byte size. Snapshot hashes and the
selected SQLite tool-state hash identify the analyzed inputs. JSONL streams
and SQLite do not share an atomic snapshot. Active agents can append records
or finalize streaming messages later, so rerunning against live inputs can
change counts. For exact replay, supply securely retained input snapshots;
the reader does not export a raw DB/log snapshot.

## Counting and correlation

- Audit deduplication compares the complete canonical JSON event, including
  timestamp. Every removed copy retains its original/copy line references.
  Different timestamps remain separate even when commands are identical.
  There is no logger event ID: genuinely separate, identical events within
  the same millisecond cannot always be distinguished from logger duplicates.
- Provenance mirrors are deduplicated by identical event ID and payload.
  Conflicting reuse of an ID is retained and reported.
- SQLite tools are counted as separate message/content records with call IDs.
  Repeated inputs are grouped for review, not removed. Routine status checks,
  controls and regression runs can legitimately repeat.
- An audit shell event is joined only to exactly one unused SQLite record
  having the same session, tool and exact command within five seconds of its
  recorded run/creation time. Ambiguity and unmatched records remain explicit.
  Provenance outcomes and available call IDs are inventoried separately;
  truncated previews are not treated as full inputs or approximate joins.
- A completed shell tool with nonzero exit is a process failure. Exit zero
  does not establish earlier pipeline/curl/HTTP success. Tool errors and
  diagnostic signals remain separate from transmitted-byte evidence.
- Network counts are static literal URL arguments per recognized invocation,
  with repeated invocations retained. Loops, redirects and variables are not
  expanded. Nested `sh/bash -c/-lc` strings are parsed locally as text.
  Dynamic destinations remain unresolved. A `--data` URL is a payload, not
  an additional destination. These are candidates, not HTTP-request totals.
- Command categories are exclusive heuristic labels; nested container/network
  work may fit several purposes. An audit `allowed` flag records a permission
  decision, not the outcome or a network receipt. Webfetch/search are absent
  from this audit stream and are counted from supplemental records.

## Redaction and instruction screening

Reports omit complete commands, content, headers/bodies and every URL query,
fragment and userinfo value. Paths are displayed only for known public Apache
source locations; other paths are redacted. Unknown external hostnames are
pseudonymized because DNS labels can themselves contain identifying data.
Local/private literal destinations are labeled separately; DNS is never queried.
The automatic identity seed is the local account name. Supply an optional
local file of additional names/identifiers with `--identifiers FILE`; values
are used for detection and never exported. Unknown personal names, encoded
payloads and dynamically constructed data can escape detection.

Opaque session/message IDs, line numbers, call IDs and hashes remain for local
investigation. These references allow correlation; they are not a guarantee
of anonymity. New report files are created with mode 0600; input permissions
are unchanged.

The scanner checks instruction-pattern windows in available external fetch/
search/curl/wget result text and looks for later exact endpoint matches in the
same session within 30 minutes. This generates review candidates; an endpoint
match alone cannot establish causality or disclosure. Normal source retrieval
is not suspicious. A negative scan does not establish absence of injection,
particularly for truncated or missing responses. The assessment separately
states what behavior was actually observed.
