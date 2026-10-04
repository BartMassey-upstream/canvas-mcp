# Workflow acceptance

This matrix records synthetic evidence for Stage 3 S3-A.
Capability boundaries are maintained in
[API coverage](api-coverage.md). A passing offline row covers
only the stated assertions. It does not establish institutional
permissions, entitlement, or behavior in a particular AI client.

## Evidence identity and safety

Baseline revision: `96d4a6a6c4ca1ecadd27e52ff7af787d452da6d3`,
with the local Stage 3 working-tree changes, tested 2026-10-04.
The clean-wheel build, package digest, and installed-wheel
results belong in the Stage 3 execution record in `PLAN.md`.
Retain the digest alongside an acceptance run; a working-tree
revision alone does not identify a rebuilt package.

All records are invented. The shared transport accepts only
`https://canvas.invalid/api/v1/` and intercepts every request.
Unexpected origins, routes, and every write fail closed in the
connected read/draft suite. Focused mutation tests use their own
in-memory mocks. No test in this matrix authorizes a live write.

Results use `passed`, `failed`, `blocked`, `not run`, or
`unsupported`. Snapshot/comparison work now has synthetic
capture, integrity, resume and conservative comparison checks.
Full workflow aspirations are listed as limitations when the
executable evidence tests only an existing baseline.

## Actors and common configuration

- I: synthetic instructor, Canvas `TeacherEnrollment`, with
  grading and roster rights; educator or creator server profile
  as specified below. Creator excludes student records.
- R: synthetic restricted educator, `TeacherEnrollment`, with
  roster reads denied and grading permission false; educator
  server profile. A missing permission is an error, not an empty
  roster or an invitation to use another identity.
- S: synthetic student, `StudentEnrollment`, caller ID 101;
  student server profile, self reads and shared course content.

Connected runs set `EXECUTE_TYPESCRIPT_ENABLED=false`,
`ENABLE_DATA_ANONYMIZATION=false`, `STUDENT_WRITE_TOOLS` empty,
and `ACCESSIBILITY_CHECKERS=ufixit`. Local stdio write policy
permits registered educator tools for preview tests; a separate
read-only policy test passes `ALLOWED_WRITE_TOOLS` empty and
asserts that write and draft tools disappear. Focused student
write tests enable only the flag under examination and use
mocked course policy. Calendar authoring uses the educator
profile; personal calendar/planner writes require their own
student flags. No feature is enabled to bypass a denied route.

## Offline matrix

| ID | Scenario | Actor/profile | Result and automated evidence |
|---|---|---|---|
| A1 | Construct and revise a unit | I/creator | Passed baseline: [creator MCP](../tests/test_creator_workflow.py), [content](../tests/tools/test_stage1_content.py), [rubrics](../tests/tools/test_rubrics.py) |
| A2 | Author Classic and New Quizzes | I/creator | Passed focused baseline: [Classic](../tests/tools/test_stage1_quiz_pages.py), [New Quizzes](../tests/tools/test_new_quizzes.py), [media](../tests/tools/test_new_quiz_media.py), [readback contracts](../tests/tools/test_stage3_new_quizzes.py) |
| A3 | Differentiate dates and organize groups | I,R/educator | Passed focused baseline: [assessment](../tests/tools/test_educator_assessment.py), [groups](../tests/tools/test_course_groups.py) |
| A4 | Weekly educator review | I,R/educator | Passed baseline: [connected review](../tests/workflows/test_connected_workflows.py); source-linked assignment analytics tested; live review blocked |
| A5 | Grade or correct a batch | I,R/educator | Passed focused baseline: [bulk grading](../tests/tools/test_bulk_grading.py), [rubric safety](../tests/tools/test_rubric_grading_safety.py), [batch preflight](../tests/tools/test_stage3_bulk_preflight.py) |
| A6 | Follow up on peer reviews/discussions | I/educator | Passed baseline: [connected draft](../tests/workflows/test_connected_workflows.py), [messaging](../tests/tools/test_messaging.py), [peer reviews](../tests/tools/test_peer_reviews.py), [discussions](../tests/tools/test_discussions.py) |
| A7 | Personal planning and calendar | S/student; I/educator | Passed baseline: [connected planning](../tests/workflows/test_connected_workflows.py), [planning](../tests/tools/test_student_planning.py), [calendar](../tests/tools/test_course_calendar.py) |
| A8 | Back up and resume | I/creator | Passed focused baseline: [exports](../tests/tools/test_content_exports.py), [migrations](../tests/tools/test_content_migrations.py) |
| A9 | Snapshot and compare records | I,R/educator | Offline capture/resume, integrity and scope tests: [capture](../tests/tools/test_record_snapshots.py), [boundaries](../tests/core/test_record_snapshot_boundaries.py), [review](../tests/tools/test_snapshot_review.py), [attachments](../tests/tools/test_snapshot_attachments.py); live acceptance blocked |

These evidence paths remain the maintained test inventory.
The execution record must name the subset actually run; linked
suites are not a substitute for a test result. New Quiz, artifact, recovery and snapshot results are recorded
with their implementation slices.

## Row contracts

### A1: linked draft unit

Fixtures: course 42, assignment group 12, assignment 34,
module 56/item 78, plus page/file/rubric fixtures in the focused
suites. Calls: `create_assignment_group(name="Practice")`,
`create_assignment(assignment_group_id=12, published=false)`,
`create_module(published=false)`, `add_module_item` with the
returned assignment ID, `update_assignment(clear_due_at=true)`,
and `get_course_structure`.

Expected reads: course resolution and module tree. Expected
mock writes: five in the connected creator baseline; focused
suites additionally exercise page/file/rubric authoring.
Approval: explicit live authoring authorization would be needed;
this run modifies only mocks. Final state: linked module item,
cleared due date, unchanged group and draft publication state.
Recovery: inspect returned IDs before retrying creation; remove
only synthetic objects. Limitation: there is no single connected
page/file/module/assignment/rubric transaction or rollback claim.

### A2: quiz authoring

Fixtures: Classic Quiz questions, New Quiz backing assignments,
interaction/scoring/feedback and media definitions, malformed
responses and existing published state. Calls: quiz create/read,
question or New Quiz item create/update/read, and publication
through the resource documented by API coverage.

Expected reads: current quiz/item/backing assignment, followed
by persisted-state verification where supported. Expected mock
writes: exact requested fields and supported type definitions.
Approval: authoring/publication authorization; destructive
question changes use tool preview/confirmation as applicable.
Final state: intended interaction data and feedback survive;
unrelated fields survive partial updates. Recovery: re-read the
item and backing assignment after uncertain writes. Limitation:
New Quiz entitlement and real Canvas schema behavior require a
separate institutional row; focused mocks cannot prove them.

### A3: differentiated dates and protected groups

Fixtures: sections 51/52, groups 61/62, inherited and explicit
assignment override dates, cross-course targets, existing
submissions and changed preview targets. Calls: section/group
reads, assignment override create/update/delete, and course
group membership tools with their explicit target IDs.

Expected reads: permissions, current target/collection, submitted
work checks and verification. Mock writes follow confirmation.
Approval: preview and identical confirmation for consequential
changes. Final state: omitted dates persist, explicit null clears,
inherit removes the override; unsafe membership changes leave
submitted work and membership intact. Recovery: obtain a fresh
preview after target/permission changes; inspect any uncertain
membership change. No atomic cross-object rollback is promised.

### A4: weekly review

Shared fixtures: four learners across two pages of roster and
submission results, missing work, late ungraded work, graded
work, excused work with absent optional fields; restricted
roster 403. Calls: `get_my_enrollments`,
`get_assignment_analytics(course_identifier="SYNTH/42",
assignment_id=34)`, and `list_submissions`.

Expected reads: paginated courses/roster/submissions and the
assignment; zero writes and no approval required for synthetic
reads. Final state: unchanged. Analytics distinguishes two
submitted, one graded, one missing, one late and one excused;
roster denial becomes an MCP error, never "no students".
Recovery: retain the failed source and rerun after permission
restoration. Limitations: legacy analytics is human-readable and
now includes source links, observation times and explicit
unavailable-field counts. This baseline covers assignment
analytics; a multi-family weekly report also needs the selected
peer-review/outcome calls described by the workflow guides.

### A5: batch grading and correction

Fixtures: existing rubric IDs/ratings, whole-input validation,
multiple submissions, dry-run requests and per-item failures.
Calls: rubric reads and `bulk_grade_submissions(dry_run=true)`
before the corresponding authorized mocked write; rubric grading
uses exact criterion/rating IDs.

Expected reads: assignment/rubric/submission context. Expected
mock writes: only validated target submissions; per-item errors
remain visible. Approval: review the dry-run and explicitly
approve grades before a live batch. Final state: successful
items updated and failed/uncertain items distinguished.
Recovery: re-read each uncertain submission before retry;
never infer rollback or exactly-once writes. Limitation: this
matrix maps focused suites rather than a new connected batch
UI or a durable batch journal.

### A6: follow-up draft and guarded delivery

Fixtures: missing/late submission records, peer-review status,
discussion entries, outbound recipients/content and changed
arguments. Calls: review tools followed by
`send_conversation(recipient_ids=["101"], subject=...,
body=...)` without a token. Shared connected tests stop at the
draft; focused suites test confirmation/reuse with mocks.

Expected reads: review context. Draft expects zero writes and
`preview=true`, `nothing_sent=true`; the token binds recipients,
content, attachments and delivery flags. Approval: show the draft
and get explicit approval before the identical confirmation call.
Final state in connected tests: no conversation created.
Recovery: inspect current state after uncertain delivery and
obtain a fresh preview; never blindly resend. No native Canvas
peer-review reminder action is claimed by Inbox messaging.

### A7: self planning and calendar

Fixtures: student 101, two courses, overdue internal assignment,
external-tool assignment with no local submitted state, disabled
flags and course/module policy fixtures. Calls: `get_my_profile`,
`get_my_enrollments`, `get_my_submission_status(42)` and focused
planner/calendar operations.

Expected reads: only self and accessible course content;
no roster/submission-collection queries in student connected
runs. Expected writes: zero when flags are disabled; focused
flag-enabled writes remain self scoped and policy checked.
Approval: follow tool confirmation for editing/deleting personal
objects and explicit authorization for live writes. Final state:
unchanged in connected runs; external work remains "Canvas does
not report external-tool submission state", not overdue work.
Recovery: report disabled tools/course policy; use permitted
reads and local drafts. Do not switch identity or execute code.

### A8: backup and resume

Fixtures: export/migration IDs, pending/failed/completed states,
paginated migration issues, interrupted downloads and occupied
targets. Calls: export creation/list/status/download and migration
preview/status; resume with the recorded existing operation ID.

Expected reads: current export/migration/progress and issues;
local download verifies digest and refuses overwrite. Expected
mock writes: one authorized creation, not a duplicate on resume.
Approval: explicit export creation and migration preview/confirm;
local artifact writes use documented private-file policy.
Final state: existing operation tracked, artifact verified, issues
retained even for a completed migration. Recovery: retain IDs,
poll once per call, re-read before new creation; never persist
credentials or reusable confirmation tokens. Local artifact
hardening is tracked separately in S3-C.

### A9: snapshot, compare and selected local identity

Fixtures: synthetic course/caller IDs, selected assignments,
multiple pages, restricted permissions, changed duplicate records,
optional content/identities, corrupt/private files, stale maps and
scoped attachment references. Calls: `capture_record_snapshot`
preview then identical local-write confirmation, resume with the
same scope, `verify_record_snapshot`, `compare_record_snapshots`,
`lookup_student_identities` for selected pseudonyms, and
`download_snapshot_attachments` with selected references.

Expected Canvas writes: zero, including confirmation calls.
Expected Canvas reads: current course/caller/permissions and only
the selected record-family routes. Expected local writes: private
record pages/checkpoints, selected identity files and separately
approved attachment files. Default model outputs contain counts,
coverage, observation times and artifact references; no names,
emails, submission bodies or signed download URLs.

Approval: review explicit local-retention scope and destination.
Partial/denied families never imply mass deletions. Corruption,
wrong scope/caller and stale maps fail validation. Resume preserves
verified pages; expired cursors and detected consistency changes
require explicit recapture. Optional downloads revalidate the
current submission reference, limit bytes and redirects, and
never forward Canvas credentials to external storage.

Final state: locally verified complete, partial or interrupted
archives, with no claim of transactionality or automatic restore.
The installed-wheel actor workflow exercises capture, verify and
comparison with synthetic course/assignment context; focused
suites exercise sensitive-field and attachment boundaries.
Real records, client/storage approval and institutional acceptance
remain separate blocked rows.

## Separate live matrix

Every live row is separately blocked. Institution, client,
package revision and execution date are `not run` for each row;
no institution or client has been inferred from synthetic data.
There is no authorized Canvas write destination for this slice.

| ID | Result | Required evidence before changing status |
|---|---|---|
| A1 live | Blocked | Authorized sandbox and course authoring permissions; institution/client/revision/date and created IDs |
| A2 live | Blocked | Authorized sandbox plus Classic/New Quiz entitlement; institution/client/revision/date and backing resource checks |
| A3 live | Blocked | Authorized sandbox with synthetic sections/groups and submitted test work; institution/client/revision/date |
| A4 live | Blocked | Authorized synthetic instructor/restricted accounts and source-linked report; institution/client/revision/date |
| A5 live | Blocked | Authorized sandbox/test learners and grading approval; institution/client/revision/date and per-item recovery |
| A6 live | Blocked | Authorized test recipients and exact send approval; institution/client/revision/date and delivery evidence |
| A7 live | Blocked | Authorized test-student account and configured flags/policies; institution/client/revision/date |
| A8 live | Blocked | Authorized sandbox/export scope and copy target; institution/client/revision/date and operation IDs/digest |
| A9 live | Blocked | Authorized synthetic course records and institution/client/revision/date |

Cleanup for every live row must name only the approved sandbox
objects and local artifacts. A blocked row does not authorize
using real students or a production course as a substitute.

## Reproducing the connected baseline

Run with isolated configuration and dummy credentials:

```sh
export XDG_CONFIG_HOME="$PWD/_stage3/config"
export CANVAS_API_TOKEN=stage3-dummy-token
export CANVAS_API_URL=https://canvas.invalid/api/v1
export ACCESSIBILITY_CHECKERS=ufixit
export UV_CACHE_DIR="$PWD/_stage1/cache/uv"
/home/bart/.local/bin/uv run --frozen pytest -q \
  tests/workflows tests/test_creator_workflow.py
```

`tests/workflows/synthetic_canvas.py` is shared by direct/MCP
tests and the stdio fixture server. It includes instructor,
restricted educator and student actors, paginated records,
sections/groups, submitted work, omitted fields, changed objects
and configurable per-route failures. Transport self-tests prove
that unexpected routes/origins and write attempts fail closed.

After building and installing the wheel in a project-local clean
venv, run the driver using that venv's interpreter:

```sh
/home/bart/.local/bin/uv run --frozen python \
  tests/workflows/run_stdio_acceptance.py \
  --python _stage3/clean-venv/bin/python \
  --output _stage3/stdio-installed-workflows.json
```

The driver starts all three actors through MCP stdio, verifies
role discovery and read/draft behavior, and records requests,
zero writes and the imported package path. The server script
adds only a synthetic audit tool; it does not edit the installed
package or add the repository's `src` directory to `sys.path`.
Verify the recorded package path is inside the clean venv before
calling this installed-wheel evidence. Record the actual wheel
digest and revision separately in `PLAN.md`.
