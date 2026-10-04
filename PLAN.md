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
workflows. Stage 1b covers educator and student gaps. Stage 2
extracts reviewable upstream contributions. Stage 3 develops
dependable daily workflows, educator snapshots and recovery
evidence; it does not depend on Stage 2 being finished. Keep the
existing integrated history; do not reconstruct working `main`
merely to prepare pull requests.

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
- Stage 3: Bart authorized the first repository-local pass and
  the remaining feasible local reliability, workflow, snapshot
  and CI work. Canvas reads are allowed; no writes may reach
  Canvas. Mutation tests use synthetic mocks. Active installation
  changes, commits and publication remain separate.

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
- hot-spot media upload is implemented with bounded local upload
  validation; live persistence acceptance remains pending; and
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

## Stage 3: dependable workflows and educator recovery

**Status: first repository-local pass implemented and verified.**
Bart authorized S3-A, the highest-impact S3-B findings, and S3-C.
No writes may reach Canvas, including provisioning test fixtures
or cleanup. Reads are allowed, but this pass uses synthetic data
and mock transports only. Active client installations and the
later packages remain deferred.

### Purpose, baseline, and sequencing

Make the broad API coverage dependable in actual work for Bart
and colleagues: course preparation, teaching, reviewable grading
and communication, personal planning, and local recovery
evidence.
Keep Linux, Claude Code and Codex as the initial client targets.
Do not assume one preferred daily workflow; provide several
representative paths and use actual experience to refine them.

Start from the completed local increments:

- `ea2396a`: unattended Stage 1 creator completion;
- `5d532fe`: unattended Stage 1b educator/student completion;
- `96d4a6a`: tracked completion and handoff plan.

These establish local implementation evidence, including 2,509
passing Python tests and installed-wheel mocked stdio checks.
They do not establish live PSU behavior, archive restorability,
or successful independent colleague installation.

Stage 2 upstream preparation is independent and secondary.
The authorized Stage 3 repository-local work proceeds without
waiting for upstream review or sandbox availability.
Carry the outstanding Stage 1/1b acceptance rows forward under
their original names; do not count them as already completed or
as newly implemented Stage 3 features.

Suggested priority and dependency order:

| Package | Priority | Main dependency | First deliverable |
|---|---|---|---|
| S3-A: acceptance and fixtures | P0 | Existing fork | Executable offline workflow matrix |
| S3-B: reliable tool outcomes | P0 | S3-A | Verified writes and usable failure recovery |
| S3-C: private local artifacts | P1 | Existing export/map paths | Shared private-file contract |
| S3-D: daily workflows | P1 | S3-A/B; C for identity artifacts | Reviewed client-neutral workflow guides |
| S3-E: student-record snapshots | P1 | S3-C and synthetic schema | Resumable local evidence archive |
| S3-F: comparison and identity lookup | P2 | S3-C/E | Local comparison and selective lookup |
| S3-G: remaining API gaps | P2 | Workflow evidence | Bounded additions with supported contracts |
| S3-H: live pilot and delivery | P1 | Accepted local slices and sandbox | Verified pinned colleague release |

P0 means required before relying on new workflows; P1 is the
main product work; P2 follows demonstrated workflow needs.
S3-H should validate finished slices as the sandbox becomes
available, rather than wait for every P2 item. Do not make a
usable creator release depend on student-data export approval.

### S3-A. Acceptance matrix and reusable synthetic fixtures

Create one maintained matrix, proposed at
`docs/workflow-acceptance.md`, linking to `docs/api-coverage.md`
for capability boundaries rather than duplicating its inventory.
Each row must record:

- actor, Canvas permissions, server profile and feature flags;
- fixture objects, required data and expected tools/parameters;
- expected reads, writes, approvals and final persisted state;
- automated evidence and the tested package revision;
- live evidence by institution, client, revision and date;
- result: not run, passed, failed, blocked, or unsupported;
- cleanup/recovery scope and unresolved limitations.

Build shared synthetic fixtures with multiple pages of results,
multiple sections/groups, missing fields, restricted permissions,
submitted work, partial failures, and changed objects. Include
an instructor, a restricted educator and a student, without real
student records. Keep these fixtures usable through both direct
tool tests and MCP stdio calls against an installed wheel.

Minimum connected scenarios:

| Scenario | Required observed behavior |
|---|---|
| Construct and revise a course unit | Linked page/file/module/assignment/rubric remain coherent; unrelated settings survive edits |
| Author Classic and New Quizzes | Supported question data persists; publication uses the correct backing resource |
| Differentiate dates and organize groups | Dates preserve omission/clear/inherit semantics; membership guards protect submitted work |
| Weekly educator review | Missing, late, ungraded and unavailable data remain distinct; every finding links to its source |
| Grade or correct a batch | Preview/dry-run precedes authorized writes; partial outcomes are itemized and recoverable |
| Follow up on peer reviews or discussions | Draft recipients/content first; confirmation cannot be reused or redirected |
| Personal planning and calendar | Self-only scope, disabled-write behavior and course/module policy dependencies hold |
| Back up and resume | Interrupted exports/downloads reuse identifiers and never silently create duplicates |
| Snapshot and compare records | Declared coverage, missing data and changed records remain explicit |

Acceptance: every scenario has specified fixtures, expected
behavior and a separately marked live row. Existing capabilities
have executable offline checks. Snapshot/comparison scenarios
now have S3-E/F synthetic evidence. Absence of a sandbox, New
Quizzes entitlement or test-student account must produce a
specific blocked row, not a blanket pass or an attempt to use
real students instead.

### S3-B. Reliability, result contracts, and bounded execution

Audit existing behavior before adding another abstraction.
Reuse `core/tool_results.py`, `core/write_outcome.py`, the
confirmation guards and the shared Canvas client. Preserve
existing public signatures unless a documented migration is
necessary.

1. Inventory tool results used by the workflow matrix.
   Distinguish
   preview, verified success, pending asynchronous work, partial
   success, rejected/no-write, and unknown write outcome. Add
   structured fields where workflows currently need to parse
   prose, retaining useful human-readable output and compatible
   existing consumers.
2. Give high-consequence writes priority: grades, messages,
   dates/settings, imports, quiz authoring and membership
   changes.
   Verify returned identity/scope and intended persisted fields.
   Use a fresh read where response echo alone is insufficient;
   report unverified writes without claiming that nothing
   changed.
3. Exercise New Quiz creation/update for each of the twelve
   currently supported writable types. Audit existing tests and
   official request/response schemas at implementation time.
   Cover scoring/feedback, required interaction data, partial
   updates, media, and preservation of unrelated fields. Create
   regression tests for actual uncovered contracts, not merely a
   test that repeats each implementation branch.
4. Define batch failure behavior: validate the whole proposed
   input before starting, bound concurrency, preserve per-item
   outcomes and stop on authentication/policy failures. Retrying
   reads may be safe; retrying an uncertain message, submission,
   grade change or import requires inspecting current state
   first.
5. Reuse export/migration resume patterns for workflows that need
   checkpoints. Persist operation IDs and completed/uncertain
   steps, never credentials or reusable confirmation tokens.
   After restart, re-read targets and obtain a new preview when
   required. Do not introduce a background scheduler.
6. Check discovery, manifest signatures, role filtering, content
   fencing, student-write dependencies and operator policy in
   every slice. A disabled capability must lead to an explanation
   and an available safe path, not a fallback through code
   execution or a different identity.

Acceptance: stale previews, timeout-after-write, partial batches,
revoked permissions, malformed responses and interrupted sessions
have meaningful tests and actionable results. No claim of atomic
rollback or exactly-once behavior may exceed what Canvas exposes.

### S3-C. Private local artifacts and identity-map hardening

Do this before expanding identified student exports. The current
`create_student_anonymization_map` already writes a local CSV;
it is not a new feature to recreate. Audit its overwrite,
permission, destination and identity handling against the
stronger
local-file patterns in `content_exports.py` and credential code.

Define a shared Linux local-artifact contract:

- stdio-only access to an explicitly scoped local destination;
- private directories (0700) and files (0600), including
  temporary files;
- exclusive creation, no silent overwrite, path traversal or
  symlink escape, and no dependence on a permissive process
  umask;
- clear behavior for collisions, disk exhaustion, cancellation,
  interrupted writes and previously incomplete artifacts;
- atomic completion markers and integrity metadata where useful;
- no raw student records, identity maps, tokens or signed URLs in
  logs, tool results, exception text or repository test evidence.

Keep raw archival data distinct from display transformations.
Use CSV formula protection for spreadsheet-facing reports and
content fences for model-facing prose. Neither should silently
rewrite the authoritative archive or a future Canvas payload.

Namespace maps by Canvas origin, course and format/algorithm
version; detect wrong-course or stale maps. Review the current
stable pseudonym algorithm and cross-course linkability. Do not
silently change existing pseudonyms or invalidate stored maps.
Pseudonymization is not a promise that free text, IDs or
documents
cannot identify someone.

Acceptance: synthetic tests verify file permissions, path and
transport boundaries, non-overwrite behavior, cleanup of partial
files and model/log redaction. Existing local-map callers receive
an explicit migration path if destination or format changes.

### S3-D. Repeatable educator and student workflows

Extend the existing skills under `skills/` before creating new
ones. Reconcile their tool names, role requirements, approvals
and defaults with the generated manifest. Start with
`canvas-course-qc`, `canvas-bulk-grading`,
`canvas-peer-review-manager`, `canvas-discussion-facilitator`,
`canvas-week-plan` and `canvas-course-backup`.

For each workflow, specify inputs, prerequisite reads, proposed
changes, user review point, exact write scope, verification,
interruption recovery and a concise final report. Separate read
and draft phases from execution. Do not assume every existing
write tool has a confirmation token or dry-run argument.

Deliver these initial workflow packs:

1. **Course readiness and revision.** Review dates, publication,
   assignment groups, module links, files, rubric associations,
   accessibility and quiz settings; produce a proposed repair
   list. Apply only authorized repairs and report each result.
2. **Weekly educator review.** Summarize missing/late/ungraded
   work, peer-review progress and selected outcome summaries.
   Show denominators, observation times and unavailable data.
   Use pseudonymous/minimal output by default; do not infer a
   student's motivation, diagnosis or risk from sparse signals.
3. **Assessment review and correction.** Collect the assignment,
   complete rubric and relevant submission state; prepare a
   reviewable batch; recheck changed attempts before writing.
   Preserve unsupported manual, moderated or provisional grading
   paths as explicit Canvas UI steps.
4. **Communication and follow-up.** Build a deduplicated
   recipient
   list and draft, then show the actual send/delete preview.
   Review delivery/partial outcomes before retrying. Do not turn
   a weekly review into automatic messaging or scheduling.
5. **Dates, sections, groups and calendar.** Show scope and
   effects before changes; respect submitted-work guards, SIS
   restrictions and calendar/appointment exclusions.
6. **Personal planning.** Help students inspect workload, notes,
   submission history and module progress. Explain missing
   student-write tools normally; check the dependent module gate
   for course-linked planner overrides and use explicit date
   bounds for older/future planner targets.

Keep examples portable between Claude and Codex. Code execution
must remain optional and explicitly privileged: a large batch
alone is not a reason to enable it or bypass ordinary tools.
Document a bounded ordinary-tool route wherever practical.

Acceptance: the same intended workflow can be exercised in
installed-wheel offline tests for both client configurations,
with zero writes in read/draft-only scenarios. Actual client
sessions are verified under S3-H; mocked stdio is not a
substitute.

### S3-E. Local student-record snapshots

Build a separate educator-only feature. Common Cartridge remains
a content archive, and creator mode must not gain student-record
access. The first snapshot milestone is evidence preservation and
comparison; automatic restoration is not part of it.

Proposed first data contract:

| Data family | Initial treatment |
|---|---|
| Course/assignment/rubric definitions | Store enough versioned context to interpret the captured records |
| Enrollment, section and group references | Minimal scoped identifiers/roles/state; names and contact details are separate opt-ins |
| Submission and grade state | Status, attempts, timestamps, scores and rubric assessments visible to the authorized educator |
| Peer-review and outcome state | Selected assignment/outcome references and visible completion/results |
| Submission bodies, comments and attachments | Separate explicit inclusion flags; excluded from the default snapshot |
| Inbox and discussion content | Excluded from the first archive; a later workflow must justify capture scope |
| Credentials and signed download URLs | Never archived |

Specify the schema before the exporter. Use a versioned JSON
manifest, streamed record files and an optional attachment tree.
The manifest should include origin/course, package/schema
versions, selected scope and filters, start/end times, captured
counts, per-family completion/errors, file sizes/digests and
consistency limitations. Keep identities and credentials out of
filenames and model-visible resume summaries.

Implement and test:

1. A scope/destination preview showing which data classes will be
   read and retained. Use synthetic records for development.
   Do not treat existing roster permission as approval to store
   arbitrary extra data or reveal it to an AI client.
2. Course-scoped paginated collection with bounded requests,
   cancellation and streaming output. Mark permission-limited
   or unavailable families; an empty response is not proof that
   no records exist across the whole course.
3. Resumption bound to origin/course, scope and schema. Preserve
   already verified files, deduplicate records, and handle
   expired pagination/download state explicitly. Revalidate
   identity and permissions before continuing.
4. Optional attachment download with per-file/total limits,
   validated destinations/redirects, safe filenames and hashes.
   Resolve temporary URLs internally and never send Canvas
   credentials to an external storage host.
5. Consistency reporting for a course changing during capture.
   Canvas reads across endpoints are not a transaction. Record
   observation windows, changes detected and required recapture;
   never call a best-effort snapshot an atomic point-in-time
   copy.
6. A local verifier that checks schema, expected files, digests,
   duplicates and completion flags without any Canvas access.
   Report complete, partial, interrupted or corrupt accurately.
7. Retention and removal instructions for approved storage,
   including temporary files, secondary copies and identity maps.
   Do not promise physical secure erasure on SSDs or backups.
   Do not invent encryption/key management; document the approved
   storage protection actually in use.

Privacy modes must state what leaves the server, what remains in
local files, and which fields still allow identification. Start
with synthetic/minimal metadata fixtures; treat even pseudonymous
student archives as restricted. Identified real-data collection
requires the institution/client/storage conditions below to be
settled before use, without blocking the offline implementation.

Acceptance: interrupted multi-page exports resume correctly;
malformed or partial data cannot pass as complete; the verifier
finds corruption and missing files; model-facing output contains
only approved summaries and artifact references. Tests cover
large synthetic courses without loading every record into model
context or keeping every attachment in memory.

### S3-F. Comparison, recovery guidance and selective identity

Add a local comparison report before any restoration writer.
Compare compatible snapshots by stable scoped IDs, distinguish
added/removed/changed/unavailable records, and treat a permission
change or partial archive as uncertainty rather than mass
deletion.
Validate archives as untrusted local input; reject path escapes,
oversized expansion and malformed manifests without executing
archive content or fetching embedded URLs.

Maintain a recovery matrix for each captured family:

- preserved evidence only;
- a supported manual recovery procedure;
- a separately reviewed API recovery candidate;
- not reconstructible through the supported API.

Do not replay submissions, attempts, grades, enrollments,
messages
or peer-review state automatically. A future recovery writer
needs
its own source/target mapping, fresh preview, conflict policy,
student-work checks and explicit authorization. Content import
into a disposable target stays a separate, existing workflow.

If named follow-up is needed, prefer a local human-facing lookup
of selected pseudonyms from the protected map. Verify origin,
course and mapping version; handle missing/stale entries. Return
only a success/status summary to the model by default. An MCP
response containing names is itself disclosure to the client and
must not be described as a purely local lookup. Never expose the
entire mapping as a convenience step.

Acceptance: synthetic comparisons and identity lookups preserve
scope, detect incomplete evidence, and cannot silently identify
people to the model. Real named-student use remains conditional
on the approved institutional workflow.

### S3-G. Evidence-driven capability and discovery gaps

Revisit `docs/api-coverage.md` against the workflow matrix and
current official API documentation. Record each missing operation
or parameter as supported-and-needed, institution-dependent,
unsupported, or intentionally excluded, with its reason.
Implement supported gaps in small reviewed slices after the
higher-priority reliability work. Do not invent a missing feature
merely because a different tool name would be convenient.

Hot-spot media upload is already implemented. Remaining New Quiz
work is schema/round-trip and live acceptance evidence, plus a
fresh check for supported stimulus/item-bank authoring APIs.
Absent supported evidence, retain the exclusion without scraping
private endpoints or automating the Canvas UI.

Audit the Python MCP catalog and TypeScript code API separately.
Show which workflow operations are available on each surface;
broad MCP coverage does not imply TypeScript parity. Do not
promise a wrapper for every tool. Any future shared backend or
TypeScript expansion must preserve permission, operator policy,
confirmation and content boundaries; the current privileged
execution path is not an acceptable substitute for those
controls.

Keep quiz-taking, group submissions, impersonation, SIS/account
administration, appointment booking and other recorded exclusions
intact unless Bart explicitly changes the relevant scope/policy.
Rich MCP App interfaces, additional operating systems, hosted
institutional deployment and new AI-provider integrations are
separate follow-on choices, not Stage 3 release requirements.

Acceptance: every new capability has supported API evidence,
meaningful behavior tests, correct roles/write gates and current
manifest/docs; unsupported claims do not reappear in skills.

### S3-H. Sandbox pilot, real clients and colleague delivery

When a sandbox is ready, record its Canvas origin and explicit
course IDs, the available permissions/features and test actors.
Prefer two disposable course shells: a source for construction
and a separate import/recovery target. If only one is available,
run the safe applicable rows and leave recovery blocked. Do not
create courses, enroll people or use real students implicitly.

Before live actions, prepare the concrete fixture/change list,
source/target/destination and cleanup inventory for approval.
Reuse authorization for the exact approved scope; do not ask
again for routine steps it already covers. A newly available
sandbox is not, by itself, permission to populate or delete it.
Only delete objects whose fixture IDs and ownership are verified;
never treat an arbitrary unpublished course as disposable.

Run the pilot in this order:

1. Verify the pinned installed revision and read-only discovery
   in actual Claude and Codex sessions. Repair active installs
   only under the separate outside-project authorization.
2. Run creator construction/edit/readback, publication
   boundaries,
   Classic/New Quiz cases, files and course-setting rows. Compare
   Canvas API state with the UI rather than only tool prose.
3. Run content export/resume/download and a representative import
   into the distinct target. Record what survives: linked files,
   module links, rubrics, settings and each relevant quiz type.
4. Use authorized test actors to exercise educator permissions,
   student writes, group/submission guards, draft/confirmation
   and
   synthetic grade/message cases. Keep unavailable role cases
   explicitly blocked instead of weakening the checks.
5. Exercise student-record capture/comparison with synthetic test
   records before any approved real-data use. Confirm permission,
   schema and storage limits in the actual environment.
6. Have a colleague install the pinned Linux package and follow
   the quick-start without Bart's checkout or undocumented shell
   state. Record first-run errors and repair the instructions.

Classify failures by implementation, institution feature, token
scope, client behavior or documented unsupported operation.
Reproduce real defects as synthetic regressions where possible.
Record irreversible effects and unknown outcomes explicitly;
cleanup is not proof of a complete rollback.

Release accepted slices through the existing fork release process
only when requested. Check package/version consistency, migration
notes, install/upgrade/rollback guidance and applicable CI gates.
Do not wait for upstream PRs to merge. Keep identified archives,
identity maps and live evidence out of distributable artifacts.

### Decisions deferred until they are needed

No further answer is needed for the authorized local first pass.
Use these defaults; real-use decisions remain deferred:

| Decision | Working default | Required before real use |
|---|---|---|
| First workflows | Read-only readiness and weekly review, then reviewed changes | Bart may reprioritize from sandbox experience |
| Test data | Synthetic fixtures and disposable test actors | Approved course IDs, actors and exact live action scope |
| Snapshot purpose | Local evidence/comparison; no automatic restoration | Agreement on retained data families for real courses |
| Identity exposure | Minimal/pseudonymous reports; local-only maps | Approved client and named-student disclosure workflow |
| Storage | Project-local synthetic artifacts only | Authorized private destination, access, retention and storage protection |
| Client/platform | Linux; Claude Code and Codex | Approved active-install changes and actual sessions |
| Publication | Local reviewable increments | Separate commit/release/push or PR authorization |

Do not assume PSU-approved AI or storage arrangements from a
server role or an anonymization flag. If a separate approved
client/provider is selected for named-student work, keep that
configuration outside this repository and separately authorized.
Choosing that route is not a prerequisite for synthetic tests or
creator-mode improvements.

### First authorized implementation pass and exit criteria

The authorized first autonomous slice is S3-A plus the
highest-impact S3-B findings, followed by S3-C. Work on synthetic
fixtures and repository-local changes;
produce a reviewable diff and test evidence before requesting
any live or installation action. Do not start S3-E by harvesting
real course records to discover what an archive should contain.

Suggested later review boundaries are workflow/skill updates,
student snapshot format/collector, local verifier/comparison,
and optional selective identity lookup. Keep each independently
reviewable; commit only when requested. Include applicable
Python, TypeScript, manifest, security and installed-wheel checks
for the changed surface, without rerunning unrelated suites on
every documentation edit.

Track these completion gates separately:

- [x] S3-A matrix, baseline workflow tests and future scenario
      specifications complete.
- [x] S3-B prioritized consequential outcomes and recovery paths
      verified synthetically; remaining breadth noted below.
- [x] S3-C private artifact/map writer contract implemented and
      tested; report/dataset migration and S3-F lookup complete.
- [x] S3-D workflow guides verified against current tool behavior
      and synthetic scenarios; real client sessions remain open.
- [x] S3-E snapshot schema, capture/resume, verifier and optional
      bounded attachment downloads implemented and tested.
- [x] S3-F comparisons and selected local identity workflow
      verified synthetically.
- [x] S3-G workflow coverage/discovery crosswalk recorded;
      supported additions and deliberate exclusions documented.
- [ ] S3-H applicable sandbox rows and both real clients
      verified.
- [ ] Independent colleague Linux installation acceptance
      recorded.
- [ ] Accepted release scope, remaining limitations and recovery
      evidence documented; publication performed only if
      requested.

Report repository-local completion separately from operational
acceptance. Record blocked live rows and explicitly declined
optional scope without pretending either has passed. The
first-pass record below is historical. The second-pass record tracks the subsequently authorized local work separately
from sandbox, client and colleague acceptance.


### First-pass execution record (2026-10-04)

Branch: `feature/stage3-first-pass`, based on `96d4a6a`.
This is an uncommitted local increment; no release, push or
active client installation was performed. The previously drafted
Stage 3 plan is retained and now records the authorization.
No writes reached Canvas. Connected read/draft tests reject all
write methods; authoring tests use synthetic mocked responses.
No live course or student records were needed.

Implemented scope:

- S3-A: [workflow acceptance](docs/workflow-acceptance.md), nine
  scenario contracts with separately blocked live rows, shared
  fail-closed synthetic transport, and connected educator,
  restricted-educator, student and messaging-preview tests.
  Existing focused suites provide the broader baseline;
  snapshot/comparison rows remain planned for S3-E/F.
- S3-B, first subset: New Quiz and question create/update now
  independently read the returned definition and verify identity
  and requested persisted fields. Tests cover all twelve
  supported question-type forwarding contracts, nested values,
  stale or malformed responses, and uncertain outcomes.
  These tests do not establish live institution-specific schema
  acceptance or every scoring/property combination.
- S3-B, first subset: grading validates the entire batch's
  supported input shape, IDs, finite values, comment provenance
  and bounded concurrency before requests. Dry-run uses the same
  validation. Canvas still interprets letter/percent grades and
  applies its own permissions and assignment/rubric constraints.
- S3-B, shared transport: automatic 429 retries now apply only to
  reads. A rate-limited write returns an uncertain outcome after
  one attempt; callers must inspect before retrying.
- S3-C: a reusable private Linux artifact writer and hardened
  identity-map export. Each new bundle has private permissions,
  exclusive files, a durable completion manifest, hashes,
  canonical origin/course/version metadata and capture time.
  Symlink components, traversal, collisions and mismatched cached
  pseudonyms are rejected. Raw JSON and spreadsheet-safe CSV
  remain separate. Legacy flat maps are untouched; migration is
  documented in `tools/README.md` and the educator guide.
- Distribution inspection found ignored scratch dependency tests
  entering the source archive. Explicit build exclusions now
  keep scratch directories, local maps and the local virtualenv
  out of distributions. Both distribution formats were inspected.

Validation of the final source:

- Python: **2,670 passed, 22 skipped**. Skips retain their
  existing optional/live-platform requirements.
- Ruff: passed across `src/` and `tests/`.
- Mypy: passed across all 65 source files.
- Generated tool manifest: regenerated and consistency checked.
- Built sdist and wheel; installed locked runtime dependencies
  and the wheel into isolated `_stage3/clean`.
- Installed-wheel MCP stdio: instructor, restricted educator and
  student workflows passed. Imported paths were verified inside
  that environment; every run recorded zero Canvas writes.
- TypeScript and active Claude/Codex installations were unchanged;
  the prior TypeScript evidence remains the applicable baseline.

Tested wheel: `canvas_mcp-1.13.0+bart.1-py3-none-any.whl`.
SHA-256:
`e7c81328b80f0a48c070ffaabe0e0eadfce6b3537d8493341c61d972b4094288`.
Rebuild after any source change and record a new digest before
using this working-tree version as release evidence.

At the end of the first pass, remaining S3-B included consistent
structured outcomes across
other tool families, stopping later batches on revoked access,
itemized uncertain/partial grading outcomes, and broader
consequential-write verification. At that point, report/export
writer migration, map validation/lookup and weekly-review source
links/unavailable-field counts were still outstanding.
At that point, S3-D through S3-H, live Canvas mutations, actual
client sessions and colleague acceptance were pending. The
second-pass record below supersedes this local backlog.


### Second-pass execution record (2026-10-04)

Bart authorized the remaining feasible local work and the CI
packaging gate. This extends the first-pass working tree on
`feature/stage3-first-pass`; it does not commit, publish, install
into active clients, or mutate Canvas. All connected evidence
uses synthetic fixtures with Canvas writes rejected.

Implemented scope:

- S3-B: batch grading independently reads each target before and
  after one dispatch. Per-row `expected_attempt` binds a reviewed
  proposal; changed/missing attempts skip before writing. Null
  attempts require explicit never-submitted evidence. Structured
  recovery and the compatible text footer distinguish verified,
  rejected, unknown and unattempted rows. Feedback/rating fields
  must persist too. Authorization failures stop later batches;
  already dispatched requests remain subject to readback. No
  automatic retries of uncertain writes were added.
- S3-B: message responses cannot claim success from malformed
  data; asynchronous acceptance remains queued, not delivered.
  Course-date reads require matching identity and explicit fields
  even when clearing dates. Group membership writes verify the
  persisted user/group/state and distinguish pending membership.
- S3-B/D: assignment analytics includes source links, observation
  times, unavailable-field counts and explicit denominator gaps.
  Absent evidence does not become a known missing submission or
  zero score.
- S3-C: peer-review report/dataset saves use private, unique
  bundles and hashes; no overwrite. Migration notes tell scripts
  to use returned paths. Distribution exclusions cover snapshots,
  maps, reports, exports and scratch/virtualenv directories.
- S3-D: six client-neutral workflow guides now cover reviewed
  grading, course readiness, weekly review, peer-review follow-up,
  discussions and backups. Synthetic review checked current
  signatures, preview/confirmation and partial-failure recovery.
- S3-E: versioned scoped snapshots with opt-in text/identities,
  bounded response reads, immutable private pages/checkpoints,
  resume, permission/scope checks, unavailable-field counts and
  explicit nontransactional consistency limits. Expired cursors,
  corruption, changed observations and scope limits cannot turn
  into a complete archive. See [the format and recovery
  contract](docs/record-snapshots.md).
- S3-E: separately approved local attachment downloads select
  only captured references, recheck current submission/context,
  stream with per-file/aggregate bounds and preserve partial
  evidence. Only public Canvas/S3 HTTPS targets are supported;
  pinned public IPs, restricted redirects and Canvas-only
  credentials protect the downloader. No arbitrary URL fetcher,
  automatic retry/resume or restoration writer was introduced.
- S3-F: local verification checks schema, scope, private modes,
  counts and hashes for record and attachment bundles. Record
  comparisons require matching origin/course/caller/scope and
  report unknown additions/removals under incomplete coverage.
  Selected identity lookup validates map integrity, algorithm and
  freshness, then saves only selected identities privately. It
  returns no names to the model.
- S3-G: [API coverage](docs/api-coverage.md) now distinguishes MCP
  workflow coverage from TypeScript helpers and retains the
  recorded unsupported/institution-dependent exclusions.
- S3-H preparation/CI: the required Linux test job now builds and
  audits sdist/wheel contents, installs hash-locked dependencies
  and the wheel in a clean environment, then exercises synthetic
  MCP stdio workflows for educator, restricted educator, student
  and creator. Snapshot capture/verify/compare are connected
  checks. The driver rejects source-checkout imports and records
  zero Canvas writes.

Validation of the final implementation:

- Python 3.13: **2,946 passed, 22 skipped**. Existing optional and
  platform/live skips remain explicit; CI covers Python 3.11–3.13.
- Ruff passed across `src/` and `tests/`; Mypy passed for all 70
  source files. Generated manifest consistency and diff checks
  passed. Six edited workflow skills passed format validation.
- Built sdist/wheel and passed distribution inspection. Installed
  hash-locked runtime dependencies and the wheel in isolated
  `_stage3/final-clean`; no active installation changed.
- Installed-wheel MCP stdio: all four profiles passed, including
  two local snapshot captures, verification and comparison. Every
  recorded Canvas request was GET, and every run had zero writes.
  Package imports resolved inside the clean installation.
- TypeScript source was unchanged; existing Stage 1b build/test
  evidence remains applicable. GitHub CI itself has not run this
  uncommitted patch.

Tested wheel: `canvas_mcp-1.13.0+bart.1-py3-none-any.whl`.
SHA-256:
`25be43eff27e8bb54b82fffffa2b0a5d4df9057ee55d744a7e8e56ac36d04a8a`.
Rebuild and retest after implementation changes; the digest
identifies this local tested artifact, not a published release.

Remaining boundaries:

- The prioritized reliability fixes are complete locally. A
  universal structured-outcome/readback retrofit across every
  legacy writer is not claimed. Individual write families still
  use their documented contracts; new failures should get scoped
  regression fixes. Readback is not an atomic Canvas transaction
  or proof against later concurrent edits.
- The real sandbox acceptance matrix, actual Claude/Codex
  sessions, colleague installation and institution-specific
  New Quiz/import/permission behavior remain unverified. These
  retain the existing deferred Stage 1/1b requirements.
- Real record retention, named-student client disclosure and
  storage decisions still require an approved environment/scope.
  Snapshot hashes provide local integrity, not authenticated
  origin or a restorable backup of all Canvas state.
- Attachments on unsupported storage hosts and large captures
  beyond bounded limits remain explicit exclusions. Record
  comparisons do not restore data or compare attachment bytes.
- CI changes are validated locally; a GitHub run requires a
  separately authorized commit/push. Active installations,
  releases, upstream extraction and publication remain separate.


## Handoff checklist

- Keep the tracked `PLAN.md` current as decisions and evidence
  change. Stage 3 local implementation and CI checks are complete;
  operational acceptance remains separate.
- Preserve the completed Stage 1/1b local commits and recorded
  test results. Revalidate affected behavior after actual changes.
- Keep Linux, Claude/Codex, profile boundaries and colleague
  usability as the product priorities.
- Leave Stage 1/1b live Canvas, recovery, actual-client and
  independent colleague acceptance pending until performed.
- Bart is arranging a sandbox. Once available, record its scope
  and prepare concrete live actions for authorization; do not
  assume course availability authorizes mutations or cleanup.
- Keep active installation changes and real-data client/storage
  decisions separate from repository-local development.
- Preserve the completed Stage 3 local validation evidence. Keep
  every real Canvas mutation blocked until separately authorized,
  including test setup and cleanup.
- Treat upstream extraction as the separate, secondary Stage 2
  track. Preserve integrated history and unrelated local work.
- Obtain the required authorization for commits, releases,
  pushes, PRs and other external actions.
