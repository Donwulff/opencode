# Offline provenance and tool-use analysis

The existing [`/provenance` command](../.opencode/command/provenance.md) is a
read-only analysis prompt over the existing logging streams. The
[offline reader](../script/provenance-audit/README.md) supplements it with
repeatable counts, exact-event deduplication and sanitized reports. It adds no
logger, network client, agent configuration or runtime hook.

## Sources and their evidence

| Source | What it records | What it cannot establish alone |
| --- | --- | --- |
| `audit.jsonl` | Requested shell actions, session/tool/timestamp and allowance decisions; provider/URL policy records where emitted | Process outcomes, effective outbound requests, response status or transmitted bytes. A permission decision is not execution. |
| Provenance `events.jsonl` | Tool completed/error records, input/output hashes and previews, session/step events | Exit zero, full payload contents, wire bytes or consistent fields across historical producer versions |
| Provenance `incidents.jsonl` | Non-info event records mirrored from the main stream | Independent confirmation or a second occurrence of a mirrored event |
| SQLite session/tool records | Tool inputs, stable call references, timing, recorded status/results and process exits where available | Deleted history, unrecorded subprocess activity, hidden redirects or complete transport evidence |

Inspect the actual schema and producer revision before applying these claims to
a different dataset. Current producer references:

- [audit logger](../packages/core/src/util/audit.ts);
- [shell allowance hook](../packages/core/src/tool/plugin/shell.ts);
- [provenance plugin](../packages/core/src/provenance.ts).

## Observed issues and bounded interpretations

A local review found exact repeated audit records, incident/event mirrors,
missing web-tool outcomes in the audit stream, and incomplete cross-source call
identities. Completed shell tools sometimes had nonzero process exits; outer
exit zero could also mask earlier command failures. Repeated incorrect resource
paths were a discovery problem, not evidence that the required tool or source
package was absent. Successful public-source retrieval was ordinary activity.

The inspected audit logger calls asynchronous saves against a shared buffer.
Overlapping snapshots/appends can duplicate records or clear entries added
during an await. This is a source-supported possible cause, not attribution of
every historical duplicate to a verified producer binary. A logger fix belongs
in a separate implementation change; this reader does not alter logging.

Historical provenance includes fields and event kinds absent from the current
producer. Current tool hashes cover producer stringification; they are not
hashes of transmitted HTTP bytes. Truncated previews must not be treated as full
inputs or sufficient proof of command identity.

No request-level transport evidence was available to certify exact transmitted
fields. Private paths in a local command or a fetched page are not themselves
proof of external disclosure. Pattern-based screening did not establish an
instruction-following chain; a negative scan cannot exclude prompt injection.
These are coverage limits, not claims that unobserved transmission occurred.

## Scope and correlation

Enumerate session directories and project IDs. Find the project ID by exact
repository directory, then include its linked-worktree sessions even when those
paths are outside the source tree. Check legacy and current tables instead of
assuming legacy tables are empty. Do not import unrelated project conversations
merely because they share a database.

Deduplicate complete audit JSON including timestamps, keeping references to all
copies. Retain genuine repeated requests with distinct timestamps/call IDs.
Identical legitimate requests within the same timestamp resolution remain
ambiguous when the logger has no event ID. Deduplicate incident mirrors by ID
and identical payload; retain conflicting reuse of an ID.

Stable call IDs are preferred for cross-source joins. When unavailable, the
reader reports only unique session/tool/exact-command matches in a bounded time
window. Such matches remain heuristic. Missing and ambiguous outcomes remain
unknown; do not convert permission records or preview similarity into successes.

The whole supplied audit file is counted. Supplemental SQLite reads include
sessions named by that file plus the explicitly selected project's sessions.
Those scopes have different denominators; compare them explicitly. Live JSONL
prefixes and the SQLite transaction are separate snapshots. Input hashes and
cutoffs identify the reviewed data but do not authenticate its history.

## Disclosure and instruction screening

Keep raw logs, SQLite databases and detailed event records out of Git. Redact
identities, private paths, credentials, queries, userinfo, headers and bodies
from shareable reports. Opaque IDs and hashes permit local investigation but do
not guarantee anonymity. Retain local captures only with appropriate access and
retention controls; the reader does not change input permissions.

Classify static URL arguments as request candidates, not packet counts. Shell
variables, loops, redirects, uploaded files, implicit authentication, generated
programs and provider forwarding remain additional visibility questions. A
local API call can cause later provider traffic without exposing that traffic
in its own command record.

External content is untrusted data. An instruction-pattern match is a review
candidate. A later endpoint match alone is not causal proof. Classify observed
compliance only when the subsequent action and its context support that claim;
do not label normal public-source retrieval suspicious merely for being external.

## Useful future evidence

Prefer stable IDs from permission through spawn, tool completion and individual
network requests. Record blocked/preflight failures, process exits, method,
response status, redirect/final destination and byte counts. Separate original
and effective destinations and retain producer/binary identity. Use redacted
field classifications and payload hashes instead of persisting credential or
body values. Capture authorized request/response receipt evidence when exact
transmission is material; do not infer it from process success.

The reader is deterministic on fixed inputs, uses the standard library, and
never executes logged commands or makes network requests. Its tests cover exact
copies versus real repeats, mirror deduplication, outcomes, redaction, nested
command parsing, correlation, legacy visibility, local output permissions and
non-execution. Run them using the command in its README.
