# Canvas MCP completion and handoff plan

This tracked plan records the agreed scope, completed work, and
remaining acceptance checks. Bart authorized adding it to version
control after the Stage 1 and Stage 1b implementation commits.

## Goal and priorities

Make the integrated fork as complete and dependable as possible
for Bart and colleagues to use for course construction.
Upstreaming is useful but secondary and must not block local
product work.

Stage 1 delivers a verified, installable fork with usable
workflows. Stage 1b subsequently covers educator and student
gaps. Stage 2 extracts reviewable upstream contributions from
that work. Keep the existing integrated history; do not
reconstruct working `main` merely to prepare pull requests.

The integrated fork should continue to provide:

- native, permission-checked per-platform credentials;
- safe Canvas path construction;
- a creator profile with no student-record access;
- assignments, assignment groups, rubrics, pages, and modules;
- Classic Quizzes, New Quizzes, navigation, announcements,
  syllabus, course settings, and course files;
- explicit null-clearing for nullable Canvas fields;
- confirmation and student-work guards for destructive
  operations;
- local Common Cartridge course-content backups; and
- fork-safe CI and release behavior.

## Agreed scope (2026-10-04)

- Initial platform: Linux.
- Clients: Claude and Codex. Use Claude Code and Codex CLI as the
  concrete Linux setup and smoke-test targets.
- Stage 1: creator-only, with broad practical coverage of the
  documented Canvas API. Do not assume a single preferred user
  workflow or limit coverage to familiar question types.
- Audit missing operations and parameters across creator tool
  families. Implement supported, bounded gaps with meaningful
  tests; record larger or uncertain gaps explicitly. API coverage
  does not authorize bypassing role, permission, or safety rules.
- Stage 1b: authorized after committing the remaining autonomous
  Stage 1 work. Complete the repository-local educator/student
  work and commit it; keep live acceptance deferred.
- Upstream preparation remains secondary in Stage 2.

## Unattended Stage 1 checklist

Execute this repository-local subset while Bart is away. Keep
live acceptance and active client changes pending separately.

- [x] Preserve baseline `5bd45a0` and create a local work branch.
- [x] Run isolated Python lint, types, and full tests, plus the
      TypeScript tests and build; fix demonstrated failures.
- [x] Make an initial broad creator API inventory against
      official documentation. Record implemented, missing, and
      excluded operations, parameters, and question-type
      coverage.
- [x] Implement bounded priority gaps found by that audit and add
      regression coverage for consequential behavior.
- [x] Package and validate the session-backup skill for both
      clients, including interruption and export failure
      handling.
- [x] Build a clean non-editable Linux installation and exercise
      MCP startup, discovery, creator filtering, and a mocked
      connected workflow without calling live Canvas.
- [x] Document pinned Linux setup for Claude Code and Codex,
      upgrade/rollback, known gaps, and an acceptance matrix.
- [x] Run completion checks and record results and remaining
      attended work here.

This checklist is an initial unattended pass, not a claim that
all creator API coverage or live acceptance can finish without
Bart. Keep remaining creator gaps in Stage 1, not Stage 1b.

## Execution boundaries

Read `AGENTS.md` before implementation. Follow its rules for
commits, external actions, credentials, and changes outside the
project.

The unattended checklist should select repository-local work:
inspection, isolated automated checks, code and documentation
fixes, acceptance-matrix preparation, backup-workflow
implementation, and clean-install checks using temporary
directories inside the project. Use dummy credentials and
isolated configuration for automated checks.

Keep human-dependent steps separate: unlocking SSH, live Canvas
acceptance, archive import, client restart, colleague testing,
and changes to the active installation outside the project.
Missing SSH access must not stall independent local work. Do not
initiate live Canvas calls, commits, pushes, PRs, or publication
as part of the unattended pass without explicit authorization for
those actions.

Retain all existing confirmation boundaries. Creating an export
is itself a Canvas write. A plan to test a workflow does not
authorize its live mutations.

## Handoff snapshot before Stage 1

- `main` is clean at `5bd45a0` and matches the locally known
  `origin/main`.
- `main` contains all completed feature work.
- Fork release tag `v1.13.0+bart.1` points to `389c573`, just
  before the latest release-skill documentation commit.
- All named topic branches have same-named `origin` tracking
  branches.
- The repository-local `git publish-bart` alias atomically pushes
  all branches plus exactly `v1.13.0+bart.1`. It must be
  refreshed whenever a new release tag is made.
- Repository-local `push.followTags` is `false`. Keep it that
  way.
- No upstream pull requests were visible in the read-only check
  made during this handoff.
- The local `uv` installation is editable and imports this
  checkout, but its distribution metadata still says `1.12.0`.
  The repository itself is versioned `1.13.0+bart.1`.
- The launcher is `/home/bart/.local/bin/canvas-mcp-server`. That
  directory was not on the noninteractive agent shell's `PATH`,
  so use the absolute path when needed. Bart's interactive shell
  has it.

The last locally fetched `upstream/main` is `562eef8`. A fresh
fetch was attempted on 2026-10-04, but the sandbox could not
prompt for the SSH key passphrase. Treat all upstream comparisons
as stale until Bart fetches successfully.

## Stage 1: complete and verify the integrated fork

### 1. Establish an integrated baseline

1. Record the starting revision and working-tree state. Preserve
   the integrated revision with a local backup ref before
   changes. Keep `PLAN.md` current and preserve unrelated local
   work.
2. Work from integrated `main`, using a local branch or isolated
   worktree where useful. Fix integrated behavior first and
   retain clear change boundaries for later upstream extraction.
3. Run the baseline checks below. Record failures, skipped
   coverage, and environmental limits; do not infer success from
   test presence.
4. Inspect current upstream changes when fetching is possible:

   ```bash
   git fetch upstream --prune
   git fetch origin --prune
   ```

   Prioritize relevant security, compatibility, and correctness
   fixes. Integrate deliberately and recheck affected behavior.
   Fetching or integrating upstream is not a prerequisite for
   local verification.

5. Inspect local runtime configuration without printing the
   token:

   - API URL: `https://canvas.pdx.edu/api/v1`
   - timezone: `America/Los_Angeles`
   - role: `creator`
   - token: platform config-directory `token` file, mode `0600`

   Record any needed active-install repair for the attended
   steps.

Automated baseline and completion gates:

```bash
$HOME/.local/bin/uv run --frozen ruff check src/ tests/
$HOME/.local/bin/uv run --frozen mypy src/
$HOME/.local/bin/uv run --frozen pytest
npm ci
npm test
npm run build
git diff --check
```

Use focused checks during fixes, then run the affected full gates
on integrated code. Run meaningful behavior tests; do not add
tests that merely mirror implementation. Review any checks that
require real credentials before execution and keep them out of
unattended runs.

Verify these invariants:

- `creator` exposes no roster, submission, grading, analytics,
  peer review, conversation, discussion, identity-map, or
  code-execution tools.
- User-controlled Canvas path segments use the shared path
  builder in both Python and TypeScript APIs.
- Canvas-authored text is fenced on reads, and writes reject
  reflected fence markers.
- Destructive tools preview and confirm. Assignment, quiz, and
  quiz question deletion require explicit student-work overrides
  when work exists.
- Nullable updates distinguish omission from explicit clearing.
- Tool policy, annotations, role filtering, documentation, and
  `tools/TOOL_MANIFEST.json` agree.
- Fork CI does not publish to Azure, PyPI, or the MCP Registry.

Exercise configuration discovery and precedence with isolated
fixtures for Linux, macOS, Windows, and migration fallbacks.
Native platform checks remain necessary where filesystem
permissions or Windows DACL behavior cannot be established on the
current host.

### 2. Define and close workflow gaps

Create a small acceptance matrix with one row per workflow and
separate evidence for:

- implemented tools and any missing capability;
- automated verification, including the command and result;
- live PSU Canvas verification, revision, and date;
- known limitations and required manual steps.

Do not mark live acceptance complete based on mocked tests. Audit
practical API coverage across all creator families, then exercise
these connected workflows. No preferred daily workflow or narrow
set of question types has been assumed:

1. Build a course unit: assignment group, assignment, associated
   rubric, page, module, linked files, visibility, and dates.
   Inspect the final course structure and group membership.
2. Create and update assignments, including clearing due, unlock,
   and lock dates without changing unrelated fields.
3. Round-trip exposed Classic Quiz settings, including timing,
   shuffling, attempts, and result visibility.
4. Create and update New Quizzes using their backing assignment
   IDs. Verify dates, group, points, timing, shuffling, attempts,
   and result visibility. Test partial updates and explicit
   clearing. Publication uses `update_assignment`.
5. Create and update supported New Quiz question types. Exercise
   `delete_new_quiz_item` preview and confirmation, including the
   student-work guard, with appropriate test fixtures.
6. Create, read, and update announcements in creator mode. Verify
   syllabus editing and its replacement confirmation boundary.
7. Reorder navigation and update course settings, checking
   readback and behavior for institution- or SIS-managed fields.
8. Upload/link, rename, move, lock, and hide course files. Check
   deletion previews and the linked-module-item safeguard.
9. Export course content, download it, and assess recovery using
   a representative import into a separate disposable course.

Give New Quizzes early attention. Read objects back after writes
and compare persisted fields with the intended change and Canvas
UI. The current New Quiz create/update tools return write
responses; these alone do not establish persistence. File updates
similarly need verification of folder, lock, and visibility
fields. Add tool readback behavior where observed failures
justify it.

Known New Quiz limits require confirmation and clear
documentation:

- stimulus and item-bank entries are readable but not authored;
- hot-spot media upload is not exposed; and
- publication is controlled through the backing assignment.

Do not silently work around these limits. Pursue additional API
coverage only when it is supported and needed for an agreed
workflow.

### 3. Package the session-backup workflow

Move this work into the first pass. The export tools exist;
provide a repository-local skill that makes the full workflow
repeatable:

1. Establish course and destination, reusing explicit session
   scope.
2. Obtain the required approval before creating the course
   export.
3. Poll using returned backoff guidance, with bounded waiting and
   a clear report when the export remains pending or fails.
4. Preserve the export ID so an interrupted workflow can resume
   without blindly creating duplicate exports.
5. Download without overwriting an existing archive.
6. Report completion, path, size, SHA-256 digest, and
   limitations.
7. Support distinguishable beginning/end-of-session archives.

Cover `waiting_for_external_tool`, interruption/resumption,
expired or failed exports, and download collisions in appropriate
tests. Keep export IDs and status separate from temporary signed
URLs.

An archive and digest establish successful download, not complete
restorability. Test representative archive contents and, with
approval, import into a separate disposable course through a
supported interface. Record what survives, particularly New
Quizzes, linked files, and settings. Do not imply Common
Cartridge includes student records or provides a complete
rollback. Avoid blanket claims that editing is now safe.

### 4. Make installation repeatable for colleagues

Prepare a clean, non-editable installation pinned to a fork
revision or release, independent of the developer checkout.
During unattended work, build and test using project-local
temporary environments and configuration; do not replace the
active launcher or user settings.

Verify package version, entry point, startup, creator tool
registry, and packaged resources. Check that setup instructions
install the fork rather than unintentionally installing the
upstream package.

Reconcile README, `env.template`, and tool documentation with
actual behavior. Update the fork-status table for current
features and ancestry without waiting for branch reconstruction.
Document:

- installation and credential setup on agreed operating systems;
- MCP registration for the clients colleagues actually use;
- how to verify the installed fork revision/version;
- upgrading and returning to the previous known-good version; and
- known limitations and common startup failures.

Refresh Bart's editable installation early in the attended work,
after baseline checks and authorization for the outside-project
installation change:

```bash
$HOME/.local/bin/uv tool install --force --editable .
$HOME/.local/bin/canvas-mcp-server --help
$HOME/.local/bin/canvas-mcp-server --test
```

The final command makes a live Canvas read; leave it out of
unattended checks unless explicitly authorized. Check client
registration using the absolute launcher path and restart the
client after installation so acceptance exercises the intended
server and registry.

Have a colleague follow the pinned-install instructions on Linux
without relying on Bart's checkout or shell configuration. Record
actual platform coverage rather than claiming untested support.

### 5. Perform attended live acceptance

Use a dedicated disposable PSU course. An unpublished course
alone does not establish that it has no valuable content or
student work. Select a separate disposable target for
recovery/import testing. Obtain explicit approval for live writes
and deletions, including export creation, and start editing
sessions with a completed export.

Exercise the acceptance matrix through actual MCP tools and
compare persisted results against the Canvas UI. Keep checks
involving existing student work in mocked fixtures unless a
suitable test course and exact live actions have been authorized.

Also exercise agent prompts, with explicit expected behavior:

- "Show the course structure and flag unpublished setup gaps."
- "Which assignments and quizzes are in each assignment group?"
- "Back up this course locally and report the archive, digest,
  and coverage limitations."
- "Propose clearing this assignment's due date without changing
  it."
- "Show all settings for this Classic Quiz and this New Quiz."
- "Explain what creator mode prevents you from seeing or
  changing."

A proposed assignment-date change must make zero writes:
`update_assignment` has neither a preview token nor a dry-run
option. Prompts must select appropriate tools, preserve
confirmation boundaries, and never fall back to excluded
student-record or ordinary discussion tools.

### Stage 1 completion criteria

- Integrated Python and TypeScript gates pass; environmental
  limits and skipped coverage are explicitly reported.
- Agreed representative workflows work on PSU Canvas, with
  readback evidence and documented gaps.
- The session-backup workflow is usable and recovery limitations
  are recorded from representative checks.
- A colleague can independently install and use a pinned fork
  version on an agreed platform.
- Documentation matches the delivered behavior and installation.

An unattended pass may finish its extracted checklist while Stage
1 remains pending live acceptance or colleague testing. Report
those remaining steps explicitly; do not call Stage 1 complete
prematurely.

## Unattended Stage 1 results (2026-10-04)

Work branch: `feature/stage1-creator-completion`. Baseline
preserved as `backup/stage1-20261004` at `5bd45a0`. No commits,
pushes, live Canvas calls, active-install replacement, or Stage
1b work occurred. Changes remain locally reviewable.

### Implemented in this pass

- Four course-folder tools: list, get, create, and update. Reads
  and moves verify course ownership; submission and user/group
  folders are excluded. Dates can be explicitly cleared. Invalid
  responses and uncertain writes are reported without suggesting
  blind retries.
- `get_quiz_question` and Classic question group IDs, correct/
  incorrect/neutral feedback, and text after answers. Readback
  includes these fields, with authored text fenced. Undocumented
  group-removal semantics are not invented.
- Empty HTTP 204 responses are successful empty results, not JSON
  failures. Regression tests verify one mutation dispatch and no
  retry, while malformed other responses remain errors.
- A shared `skills/canvas-course-backup/SKILL.md`, validated and
  independently reviewed against interruption, pending export,
  ambiguous creation, filename collision, and end-snapshot cases.
- Linux fork setup for Claude Code and Codex in README, including
  pinned revision installation, skill setup, and rollback.
- Policy, trust fencing declarations, creator registry tests,
  manifest parameters, and tool documentation for the additions.
  Creator now exposes 92 tools with default accessibility tools.

### Verification evidence

- Ruff and mypy passed. Full Python suite: 1,968 passed and 22
  skipped in 52.54 seconds. Skip details are recorded in
  `_stage1/logs/completion-pytest.log`: optional timezone and
  audit dependencies, Windows checks, live-account requirements,
  and pre-existing unimplemented test placeholders. No skipped
  behavior is claimed as verified.
- TypeScript: 98 tests passed and build passed. No TypeScript
  implementation changed in this pass.
- Connected mocked workflow: MCP discovery, assignment group,
  draft assignment, draft module, module item, explicit date
  clearing, and structure readback passed through real MCP and
  HTTP serialization with `httpx.MockTransport`.
- Clean non-editable Linux wheel: launcher, package metadata,
  resources, real stdio discovery, creator exclusions, and new
  question/folder reads passed with mocked Canvas HTTP.
- Existing local config was inspected without reading the token:
  PSU URL, creator role, Los Angeles timezone, native token mode
  `0600`, and Codex absolute launcher/creator arguments match.
- Actual interactive Claude/Codex sessions and PSU persistence
  were not tested. CLI configuration syntax was checked against
  installed command help and official client documentation.
- Initial dotenv-disabled harness caused one configuration test
  failure; it passed with the harness corrected. This was not a
  product bug. The full suite was rerun with dotenv enabled.

Local detailed evidence is in `_stage1/logs/`, with source-linked
API notes in `_stage1/notes/creator-api.md` and `quiz-api.md`.
These ignored artifacts are local working records, not package
contents. The wheel excludes these records and configurations.

### Acceptance matrix and remaining creator work

All rows below still need live PSU acceptance. Automated evidence
comes from the full suite, focused regressions, and the connected
workflow above; it does not establish institution behavior.

| Family | Local coverage | Remaining Stage 1 work |
|---|---|---|
| Folders/files | Folder list/get/create/update; file tools pass | Safe folder deletion/copy; live move/lock readback |
| Assignments | Create/update/groups, null clearing, linked module journey | Attempts/position/external-tool fields and other documented authoring options |
| Assignment groups | List/create/update/delete with assignment moves | Single-group read and validated drop rules |
| Classic Quizzes | Definitions, question CRUD, feedback/group IDs | Question groups, ordering, search; calculated-question schema verification |
| New Quizzes | Existing settings and 12 question types | Hot-spot upload; required interaction-data validation; per-type live evidence |
| Pages | Existing CRUD/settings/content reads | Duplication, revisions, scheduling with account feature checks |
| Modules | Existing CRUD/items/structure | Single-object reads, search, iframe dimensions |
| Rubrics | Existing reads/create/association and guarded replacement | Review association options; structural/destructive changes remain guarded |
| Syllabus/settings | Existing syllabus and typed settings tests | Home-page selection; live SIS-managed-field behavior |
| Navigation | Existing list/update tests | Pagination/immutable-tab validation and live ordering |
| Exports/migrations | Existing export/migration tests and backup skill | Migration recovery listing; archive inspection and real import recovery |
| Linux clients | Clean installed-wheel MCP smoke; documented commands | Actual Claude/Codex sessions and independent colleague setup |

This is a broad initial inventory, not an exhaustive endpoint or
parameter audit. Expand it as Stage 1 continues. In particular,
content-only families outside the current tools, such as learning
outcome authoring, still need a scope/API review. Do not claim
that current creator coverage is complete.

Next creator implementation priorities are home-page selection,
assignment authoring options, group drop rules, Classic question
groups/order, page history/duplication, and migration recovery.
Live acceptance and backup restoration remain independent gates.
Remaining creator work stays in Stage 1; it must not be deferred
to Stage 1b just because that later stage covers other roles.

## Commit checkpoint and remaining interaction (2026-10-04)

Bart authorized committing the completed work. The work branch
now contains:

- `cc51a72`: initial creator additions, backup skill, and Linux
  client documentation.
- `557b6ff`: weekly CI fixes, patched dependencies, and daily
  transitive dependency updates through Dependabot's uv support.

Current validation: 1,971 Python tests passed, 22 skipped; Ruff,
mypy, and the CI-equivalent dependency audit passed. The audit
used the existing exception only. No merge, push, active-install
change, or live Canvas action occurred. The plan was still
untracked at this checkpoint.

Remaining local creator implementation needs no further scope
decision. Human-dependent acceptance still needs:

- Two disposable PSU courses: one for authoring tests and a
  separate import/recovery target, plus a backup destination.
- Authorization for the specific live Canvas test actions,
  including exports, writes, imports, and cleanup deletions.
- Authorization to refresh the active user installation and
  client setup, followed by fresh Claude/Codex sessions and
  Canvas UI checks of persisted results.
- A colleague to independently follow the Linux installation
  instructions and report results.

SSH unlocking may be needed for remote comparison if fetching
still fails. Publishing the CI fixes remains a separate action;
these local commits do not change scheduled runs on origin.

## Remaining unattended Stage 1 completion (2026-10-04)

Bart authorized finishing and committing repository-local work,
then proceeding to Stage 1b. The initial backlog above has now
been implemented or given an explicit scope boundary in
`docs/api-coverage.md`. The initial inventory is retained as a
historical record, not the current remaining-work list.

This increment adds 35 tools: creator coverage rises from 92 to
127. It includes assignment authoring options/drop rules/home
selection, Classic groups/order/calculated definitions, page
history/reversion/scheduling, module details, file/folder copies
and empty-folder deletion, New Quiz media upload, course-owned
outcomes, migration recovery/issue reads and confirmed local
Common Cartridge imports. The generated tool manifest records
live signatures and profile visibility.

The installed-wheel mocked stdio smoke passed with 127 creator
tools. TypeScript: 98 tests and build passed. Ruff and mypy passed.
Python: 2,195 passed and 22 skipped. Committed as `ea2396a`
(`completed unattended Stage 1 creator coverage`).
All evidence remains repository-local; no live Canvas calls,
active-install changes, merges, pushes or publication occurred.
Live persistence/recovery, actual client sessions and colleague
installation checks remain explicitly deferred by Bart.

## Stage 1b: educator and student completion

Bart authorized this stage after the remaining unattended Stage
1 work is committed. Live Canvas acceptance, active client setup,
and independent colleague acceptance are deferred for both stages.
Stage 1 continues to exclude student-record workflows.

Compare documented educator and student API capabilities against
what the completed fork exposes, including missing operations and
parameters. Prioritize practical gaps, implement reasonable
supported additions, and verify role permissions, privacy,
anonymization, confirmation, and student-write feature gates.

Keep operator/instructor restrictions and quiz-taking exclusions
intact unless an explicit policy decision changes them. An API
endpoint's existence alone does not justify exposing it. Record
unsupported or intentionally excluded capabilities and reasons.

Define and record practical API coverage when this stage starts.
Preserve operator and course policies, privacy and confirmation
boundaries. Commit verified repository-local work independently
from Stage 1; do not publish or change the active installation.

## Unattended Stage 1b results (2026-10-04)

Completed the authorized repository-local pass after Stage 1
commit `ea2396a`. This pass adds 69 tools across five modules:

- 17 educator assessment tools: differentiated overrides,
  sections, submission details, late policies, peer-review
  unassignment and outcome-result/rollup reads;
- 11 collaborative course-group/category/membership tools;
- 9 communication tools, including own discussion state,
  confirmed moderation and course-bound inbox operations;
- 5 course-calendar tools for ordinary individual events;
- 13 self-only planning/progression reads and 14 individually
  enabled personal writes, including calendar events.

Student discussion actions have four additional individual flags.
There are 21 student-write names in total, including the original
three. All remain disabled by default. Course-linked planner
create/update also requires the module-completion operator flag
and course policy, because Canvas synchronizes that state even
for a dismissal-only update. Older/future planner targets can be
resolved using explicit date bounds.

Registry counts with UFIXIT available:

| Profile | Default | All feature gates |
|---|---:|---:|
| Student | 55 | 76 |
| Creator | 127 | 127 |
| Educator | 210 | 212 |
| All | 229 | 248 |

The coverage matrix and intentional exclusions are maintained in
`docs/api-coverage.md`. The manifest records actual signatures and
profile visibility. This is broad practical course-level coverage,
not a promise to wrap every Canvas administration or integration
endpoint. Existing operator restrictions and academic-integrity
boundaries remain in place.

Verification completed:

- Full Python suite: 2,509 passed, 22 skipped.
- Ruff passed; mypy passed across 64 source files.
- Generated manifest consistency check passed.
- Built a wheel and installed it non-editably in the isolated
  `_stage1/clean` environment; verified its five modules match
  current sources and that local credentials/evidence are absent.
- Installed-wheel mocked stdio reads passed in creator (127),
  educator (210) and student (55) profiles with anonymization on.
- TypeScript remains unchanged from the Stage 1 increment,
  whose 98 tests and build passed.

Detailed local evidence is in `_stage1/logs/` and audit/review
notes in `_stage1/notes/`. No live Canvas changes, active user
installation changes, merges or pushes occurred. The three live,
client-installation and colleague acceptance gates remain deferred
at Bart's request. No additional user decision blocks this local
pass. Committed as `5d532fe`
(`completed unattended Stage 1b educator and student coverage`).

## Fork releases

Release the integrated fork when it meets the agreed readiness
gates; upstream PR preparation is not a release dependency.
Publication and commits still require explicit authorization.

For the next fork release, increment rather than move the
existing tag. Choose a version reflecting the upstream version
actually integrated, synchronize version files and `uv.lock`, and
refresh `git publish-bart` to pin exactly that tag. Keep
`push.followTags=false`. Review the alias's all-branches scope
before using it; a fork release need not publish unfinished topic
branches.

Invoke the repository release-drill skill only when Bart requests
the drill. It does not itself authorize a push. Run the fork CI
matrix when publication is authorized and diagnose any platform
failures.

## Stage 2: prepare upstream contributions

Begin this secondary track after the first-pass product work is
stable or when Bart explicitly reprioritizes it. Preserve the
integrated fork and extract changes into separate branches; do
not rebuild `main` as a prerequisite for PR preparation.

Before rewriting published topic branches, create backup refs or
record their commit IDs. Never push without exact authorization.
If rewritten branch publication is approved, use
`--force-with-lease`.

### Branch inventory at handoff

These branches are cleanly based on the last known upstream
state:

- `config`: three credential/configuration commits;
- `pathsafe`: one path-construction commit;
- `nullnull`: one nullable-field commit; and
- `creator`: `pathsafe` plus ten creator commits.

These branches currently include unrelated integrated history and
need to be rebuilt before they become reviewable pull requests:

- `fileops`: isolate `d656e49`;
- `newquiz`: isolate `7633f8c` on `creator`;
- `exports`: isolate `23ceacc` and `ed94ec0`;
- `settings`: isolate `9453d44` on `creator`;
- `ci-fixes`: isolate `cd56212` and `20a806c`; and
- `release-skill`: fork-local release and documentation history.

The generic installation documentation commit is `e82f692`. It
should be separated from the fork-status prose if it is proposed
upstream.

### Upstream branch extraction

Use the newly fetched `upstream/main`, not the stale local
snapshot. The preferred dependency structure is:

```text
upstream/main
├── config
├── pathsafe
│   └── creator
│       ├── fileops
│       ├── newquiz
│       ├── settings
│       └── exports
├── nullnull
└── fork-safe-ci
```

Notes:

- Keep `pathsafe` before `creator`; creator route construction
  depends on it.
- Keep `nullnull` standalone. The creator-specific null fixes
  already live in creator commits and were intentionally handled
  separately.
- `fileops`, `newquiz`, `settings`, and `exports` touch creator
  role registration, so stacking them on `creator` is the least
  surprising review structure. They should be sibling PRs, not
  stacked on one another.
- Split `cd56212`: its Windows token-ACL fix depends on `config`,
  while its Azure workflow guards can stand alone. Combine the
  workflow part with `20a806c` as a fork-safe CI PR.
- Keep the Bart-specific release tag, `publish-bart` alias, fork
  status table, and release-drill skill out of upstream PRs. They
  are fork operations, not product behavior.
- Update each PR branch's README, `AGENTS.md`, tool
  documentation, and generated manifest only for tools present on
  that branch.

Suggested upstream PR order:

1. `pathsafe`
2. `config`
3. `nullnull`
4. fork-safe CI
5. `creator` based on `pathsafe`
6. `fileops`, `newquiz`, `settings`, and `exports` as separate
   PRs based on `creator`

If upstream will not review stacked PRs, split creator-profile
plumbing from the individual tool families or wait for `creator`
to land before rebasing its child branches.

For each extracted branch, run focused checks and applicable full
Python/TypeScript gates, check its diff against its actual base,
and reconcile generated manifests with tools present on that
branch. Integrated-fork success does not establish
standalone-branch success.

Ask separately before creating upstream PRs. Upstream review
delays must not block continued fork maintenance or local
releases.

## Deferred work

### Student-data backups

Common Cartridge exports intentionally omit enrollments,
submissions, grades, interactions, and other student records. If
educator-mode work later needs a local safety snapshot, design
this as a separate feature, not an extension of creator mode.

At minimum, define:

- exactly which records are exported;
- a documented machine-readable archive format and manifest;
- pagination, attachment handling, checksums, and resumability;
- private-file permissions, destination restrictions, and refusal
  over remote HTTP transport;
- anonymized versus identified modes;
- retention and secure-deletion guidance; and
- restoration expectations, since Canvas APIs may not support a
  full replay of student state.

Treat this as sensitive FERPA data and obtain institutional
approval for the AI client and storage workflow before using
identified exports.

### Additional New Quiz coverage

The Stage 1 audit confirmed a documented hot-spot media upload
flow. Keep implementation in the remaining creator backlog; it
needs explicit upload-host and response validation. Stimulus and
item-bank authoring still require supported API evidence. Do not
bypass supported APIs with browser automation.

### Educator workflows

Creator mode is suitable for course setup without student
records. If weekly named-student check-ins are later required,
separately evaluate PSU-approved AI tooling, educator mode,
anonymization boundaries, and a controlled local
re-identification workflow.

If re-identification is needed, prefer a narrow local lookup tool
that maps selected pseudonyms only after explicit user direction.
Do not send the complete identity map to the model, and do not
add this tool to creator mode. Configure `agy` against the
PSU-approved Gemini account separately from this repository if
that institutional route is chosen.

## Handoff checklist

- Keep the tracked `PLAN.md` current.
- Follow the recorded Linux, Claude/Codex, creator-only scope.
- Execute the extracted unattended Stage 1 checklist.
- Preserve the starting integrated revision and unrelated work.
- Establish Python and TypeScript baselines and fix relevant
  failures.
- Build the workflow acceptance matrix and close priority gaps.
- Package and test the repeatable session-backup workflow.
- Verify a pinned clean install and reconcile setup
  documentation.
- Record attended installation, live acceptance, recovery, and
  colleague-testing steps that remain outstanding.
- Fetch upstream when available; do not stall local work on SSH.
- Keep PR reconstruction secondary and preserve integrated
  `main`.
- Obtain explicit authorization for commits and external actions.
