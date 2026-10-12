# Canvas MCP upstream review October 12 2026

The fork meets Bart's current needs, and a colleague is trying
it. The immediate work is a bounded stabilization pass. Broader
feature adoption and upstream contribution work are deferred
until Bart has time or a student takes responsibility.

The comparison used fork `567bd31`, upstream `7c4229c`, and
common base `562eef8`. Upstream had 206 additional commits and
the fork 60, including merges and documentation. Comparing
source trees changed 73 files, with 16,106 inserted and 9,934
removed lines. These counts describe divergence, not defects.
Upstream includes v1.14.0 and subsequent fixes.

## Stabilization findings

The following problems were present at the reviewed fork
commit. The current stabilization pass addresses them; the
validation record below distinguishes code changes from a
released or installed version.

### Peer review assignment could fabricate student work

`assign_peer_review` scanned one page of 100 submissions. A
reviewee beyond that page looked absent, which triggered a
POST creating a placeholder submission on the student's behalf.
A synthetic reproduction confirmed the unintended write.

The fix reads the reviewee's submission directly, validates the
returned student and submission IDs, and refuses a missing
record. It never creates a student submission. Upstream fixed
this in [PR 436][436].

### Duplicate course codes could select the wrong target

The last course with an identical code overwrote earlier
entries in the alias cache. A synthetic list containing two
courses with code `SAME` resolved to the second course.
Canvas permissions do not prevent a wrong-target write when
the caller can edit both courses.

The fix rejects duplicate exact codes and requires a numeric
or explicit SIS identifier. Only a complete course-list read
establishes alias uniqueness; filtered lists and individual
reads can remember display labels but cannot select aliases.
This adapts [PR 482][482] while retaining the fork's strict,
exact-code identifier contract.

### HTTP course and policy caches crossed caller boundaries

Process-wide aliases and display labels were reused across
Canvas credentials. A second synthetic HTTP caller received
the first caller's cached ID and private label without reading
its own course list. Upstream fixed course metadata isolation
in [PR 480][480].

Course-policy verdicts were also keyed only by course ID. A
cached grant skipped the current caller's syllabus-read check.
[PR 491][491] proposes fixing this upstream and was still open
when reviewed. Canvas continues to authorize the final request;
these findings do not establish a Canvas permission bypass.

The fork now uses request-local HTTP course metadata, discarded
on credential replacement and request cleanup. HTTP policy
checks re-read policy without reading or populating the shared
stdio verdict cache. Single-credential stdio caching remains.

### Complete content reads omitted grading instructions

`get_rubric` shortened criterion descriptions to 200 characters
and rating descriptions to 100. Discussion replies were also
shortened with `include_full_content=True`. A synthetic rubric
lost instructions near the end of its description.

Full rubric and discussion reads now preserve the complete
text. Discussion listing previews remain explicitly described
as previews. Complete-content tools request the larger result
allowance supported by Claude. Upstream addressed these issues
in [PR 462][462].

### Error diagnostics bypassed content fencing

Canvas HTTP error bodies were returned before anonymization,
and existing student tools printed those diagnostics without
provenance fencing. A synthetic error containing an instruction
and an email address reached the model as ordinary error text.
This demonstrates the output boundary defect, not that a
student can control every Canvas error response.

HTTP failures now expose status and existing write-outcome
evidence without response bodies. Unexpected exception messages
are reduced to their types; known local response-limit messages
remain useful. Student and peer-review output paths bound and
fence other diagnostics. This adapts the protection in
[PR 481][481] to existing fork tools.

### Peer review listings and submission status were misleading

`list_peer_reviews` used the reviewee's `user_id` as the
reviewer, read only one review page per submission, and silently
skipped failed reads. It now uses `assessor_id`, follows all
pages, and explicitly reports incomplete results. See
[PR 436][436].

Submission status inferred overdue work from dates even when
Canvas returned `missing=false`. Graded paper work reproduced
this error. The fix follows Canvas's missing flag and identifies
graded, excused, and offline work separately. External-tool
submission status remains explicitly uncertain. See
[PR 423][423].

## Upstream features beyond the current scope

- [Guarded content edits][437]: expected timestamps or body
  hashes, unique find/replace, required fragments, and read-back
  verification. These would be useful for preventing overwritten
  page, assignment, discussion, and syllabus edits.
- [Grade scenarios][473]: weights, drop rules, hypothetical
  scores, and target-grade calculations. Overall grade and
  submission-status reads are already available in the fork.
- [Student feeds][471]: cross-course announcements and activity
  covering discussions, messages, grades, and comments.
- [Own-group reads][472] and [group discussions][433]: student
  memberships and topics that exist only in group spaces.
  Existing educator group administration is a different workflow.
- [Student quiz awareness][474]: own submission state, attempts
  used and remaining, and kept score. The fork already supports
  substantial Classic and New Quiz authoring.
- [Student Inbox writes][476]: recipient discovery and limited,
  gated student send/reply flows. Shared inbox reads already
  exist; fork conversation replies require educator permissions.
- [Calendar aggregation][475]: automatic active-course and group
  aggregation and a full-event read. The fork already has planner
  tools and personal or individually selected course calendars.
- [Exact date metadata][438]: raw dates, assignment overrides,
  and checkpoint dates that summaries can obscure.
- [Anonymous discussion reads][442]: an optional GraphQL fallback
  for topics that Canvas REST returns as 404.

The initial review incorrectly classified [PR 487][487]'s
single-student submission reader as missing. The fork already
provides `get_submission_details` with permission checks,
optional content/comments/history, and central privacy handling.
Upstream has a narrower text-only presentation and visibility
filters. Compare those policies if needed; the underlying read
capability is already present in the fork.

## Additional findings retained for future maintenance

- Anonymous-topic update preflight from [PR 446][446] remains
  absent. The fork directly attempts REST updates. This is
  independent of adopting GraphQL reads.
- Inbox pagination disclosure and course-overview sampling
  disclosure from [PR 436][436] remain follow-up work.
- Windows timezone data and audit-handler closure from
  [PR 460][460] remain follow-up work. Linux is the initial
  supported workflow target; current native Windows CI focuses
  on configuration and token security.
- Anonymization needs upstream's additional participant/group
  shape handling before adopting its group and recipient tools.
  The existing discussion formatters discard the problematic
  participant list, so ordinary output exposure was not
  established.
- Credential `repr` printed tokens. The small fix proposed in
  still-open [PR 490][490] is included in this stabilization
  pass. No active token logging path or leaked token was
  established.
- Still-open [PR 494][494] concerns a GraphQL routing cache that
  the fork does not have. Reassess it if adopting that feature.
- The new [OAuth scope inventory][486] is documentation and a
  proposed institutional pilot, not implemented OAuth support.

PyJWT 2.15.1, urllib3 2.8.0, and multidict 6.9.1 security
upgrades are already present. Preserve the fork's stronger
write-retry, structured outcome, empty-response, bounded-read,
and path-construction protections when adapting upstream work.

## Validation and release status

Regression tests exercise synthetic Canvas responses, actual
registered tools, concurrent credential contexts, and real
httpx mock transports. Python 3.11, 3.12, and 3.13 each passed
3,028 tests with 22 skips. Ruff, mypy, manifest validation,
distribution inspection, and four installed-wheel synthetic
MCP workflows passed. The subsequent full local CI also passed
98 TypeScript tests, formal proofs, the dedicated security
suite, dependency audit, and the secret-scan gate. No live
Canvas operations were performed. The
[current handoff](../PLAN.md) records advisory findings,
checks requiring GitHub runners, and local evidence paths.

Bart subsequently requested a local commit. Release and
installation remain separate actions: the installed
client/server version and colleague's installation are not
changed by this work.

Use `7c4229c` as the baseline for the next upstream review.
Review next week when requested; no recurring automation is
configured. The [maintenance plan](fork-maintenance-plan.md)
preserves the broader proposal and student handoff.

[423]: https://github.com/vishalsachdev/canvas-mcp/pull/423
[433]: https://github.com/vishalsachdev/canvas-mcp/pull/433
[436]: https://github.com/vishalsachdev/canvas-mcp/pull/436
[437]: https://github.com/vishalsachdev/canvas-mcp/pull/437
[438]: https://github.com/vishalsachdev/canvas-mcp/pull/438
[442]: https://github.com/vishalsachdev/canvas-mcp/pull/442
[446]: https://github.com/vishalsachdev/canvas-mcp/pull/446
[460]: https://github.com/vishalsachdev/canvas-mcp/pull/460
[462]: https://github.com/vishalsachdev/canvas-mcp/pull/462
[471]: https://github.com/vishalsachdev/canvas-mcp/pull/471
[472]: https://github.com/vishalsachdev/canvas-mcp/pull/472
[473]: https://github.com/vishalsachdev/canvas-mcp/pull/473
[474]: https://github.com/vishalsachdev/canvas-mcp/pull/474
[475]: https://github.com/vishalsachdev/canvas-mcp/pull/475
[476]: https://github.com/vishalsachdev/canvas-mcp/pull/476
[482]: https://github.com/vishalsachdev/canvas-mcp/pull/482
[480]: https://github.com/vishalsachdev/canvas-mcp/pull/480
[481]: https://github.com/vishalsachdev/canvas-mcp/pull/481
[486]: https://github.com/vishalsachdev/canvas-mcp/pull/486
[487]: https://github.com/vishalsachdev/canvas-mcp/pull/487
[490]: https://github.com/vishalsachdev/canvas-mcp/pull/490
[491]: https://github.com/vishalsachdev/canvas-mcp/pull/491
[494]: https://github.com/vishalsachdev/canvas-mcp/pull/494
