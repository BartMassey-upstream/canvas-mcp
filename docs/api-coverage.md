# Canvas API coverage and acceptance

This is the practical course-level scope for the integrated
fork. It distinguishes supported operations from deliberate
boundaries; it is not a claim to implement every Canvas route.
Tools follow the caller's Canvas permissions. Account, SIS,
billing, authentication administration, and course lifecycle
management are outside this scope.

## Stage 1: creator capabilities

| Family | Supported practical workflows | Boundaries |
|---|---|---|
| Assignments | Definitions, creation, partial updates, dates/clearing, attempts, ordering, submission modes, external launch settings, annotation files, grade visibility | Integration-only/LTI deep linking and institution-managed fields omitted |
| Assignment groups | List/detail, create/update, weight, drop rules, guarded deletion with assignment moves | Rules apply through update; never-drop IDs must belong to the group |
| Classic Quizzes | Definition/question CRUD, feedback, calculated definitions, random question groups, bank links, safe ordering, search | Calculated answers must be precomputed; bank authoring lacks a supported public API |
| New Quizzes | Definitions/settings, twelve writable question types, item reads/deletion, local hot-spot media upload | Stimulus and item-bank authoring are read-only in the public API; publish through the backing assignment |
| Pages | CRUD, front page, settings, duplication, revision list/read, confirmed restoration, scheduled publication | Scheduling depends on the institution's feature settings; historical editor identities omitted |
| Modules | CRUD, single module/item reads, full structure, item search, external links, iframe dimensions on creation | Learner progression/reset operations are outside creator mode |
| Files/folders | Upload/download/read, visibility/dates, folder CRUD, file/folder copies, guarded file and empty-folder deletion | No recursive force-delete or overwrite-copy; submission/user/group folders excluded |
| Rubrics | Definitions, creation/import, association options, ID-preserving text/point updates | Structural replacement and destructive assessment-link changes excluded |
| Course settings | Syllabus, navigation, course dates, documented settings, home-page selection with readback | Home/Settings tabs immutable; SIS/account restrictions still apply |
| Outcomes | Course-owned definitions and group organization, linking, guarded unlink/empty-group deletion | No student outcome results or mutation of account/global/shared definitions |
| Exports/imports | Common Cartridge export/download/digest, resumable backup skill, confirmed course copy, local cartridge import, migration history/status/issues | Arbitrary URL imports and institution-specific migrators omitted; recovery remains unverified live |
| Accessibility | Built-in scans and fixes, optional UFIXIT integration | Add-on tools depend on the institution's deployment |

The machine-readable [tool manifest](../tools/TOOL_MANIFEST.json)
includes signatures and profile visibility. Regenerate it after
changing tools:

```bash
uv run scripts/update_tool_manifest.py
uv run scripts/update_tool_manifest.py --check
```

The manifest describes all feature-gated tools. Actual discovery
also reflects operator write policy and enabled features.

### Explicit safety boundaries

Destructive actions retain previews and single-use confirmations.
Folder deletion is intentionally empty-only: delete files through
their individual previews first, then remove empty folders. Copy
operations rename collisions. Classic ordering refuses changes
that would silently move questions between groups.

Local uploads are unavailable over hosted HTTP. New Quiz media
accepts PNG, JPEG, GIF and WebP up to 20 MiB, sent to validated
HTTPS Canvas/S3 destinations without a Canvas token or redirects.
Nonstandard storage needs separate transport support. Signed
upload URLs never appear in tool results.

Outcome edits are restricted to definitions owned by the course.
Account/global outcomes and outcome results are not available
through creator authoring tools. Existing rubrics retain their
criterion and rating IDs; replacing assessment structure is not
an alternative spelling of editing rubric text.

### Evidence and deferred acceptance

Automated tests exercise request serialization, partial updates,
confirmation replay/change rejection, ownership checks, error
paths, credential boundaries, role filtering, and content fencing.
A mocked stdio smoke test uses a clean installed wheel. These
checks contact no live Canvas instance.

The following acceptance work is explicitly deferred:

- PSU Canvas persistence and UI comparisons in disposable courses;
- actual archive import/recovery coverage, particularly New Quizzes;
- refreshing the active installation and actual Claude/Codex sessions;
- an independent colleague following the Linux setup instructions.

No claim of complete restorability follows from an export digest.
A successful automated test does not establish institution feature
availability or permission behavior.

## Stage 1b: educator and student capabilities

These tools extend the educator/all and student/all profiles.
Creator remains a course-authoring profile without these student
records or personal workflows.

| Family | Supported practical workflows | Boundaries |
|---|---|---|
| Differentiated assignments | Override list/detail/create/update/delete for student, section or group targets | Every write is confirmed; omitted dates are preserved; clearing a date and inheriting the assignment date are separate operations |
| Sections | List/detail/create/update and confirmed empty-section deletion | No SIS-managed or cross-listed section changes; no enrollment creation, removal or role changes |
| Assessment records | Individual submission detail with optional content, bounded history and comments; late-policy read/create/update; outcome scores/rollups; peer-review unassignment | Educator permission required; central anonymization applies; signed attachment URLs and embedded profiles omitted |
| Course groups | Group-set settings, group creation/editing, membership reads/add/remove and empty-only deletion | Course-owned collaborative groups only; known SIS-managed groups refused; no tags, SIS imports, implicit member moves or membership changes after submitted group work |
| Discussions | Own subscription/read state; confirmed entry edits/deletion and topic deletion | Moderator permissions checked at runtime; student edits require ownership, an enabled flag and course policy; attached entries must be edited in Canvas to preserve attachments |
| Conversations | Confirmed reply, state/star/subscription changes and removal from the caller's inbox | Bound to a verified course context and moderator permission; no student conversation-write variant |
| Personal planning | Planner items, notes and completion/dismissal overrides; note and override writes | Self only; writes individually disabled by default; course policy also applies where completion changes module progress |
| Calendar and dashboard | Course and personal event reads/authoring, favorite courses and bookmarks | Personal writes individually gated; no appointments, recurring-series changes or calendar-wide bulk mutations |
| Student progress | Module progress/sequence, own submission history and attachment metadata | No arbitrary user selector, observer impersonation, quiz-taking, group submissions or signed download URLs |

The operator's `ALLOWED_WRITE_TOOLS` still determines whether
write tools are available. `STUDENT_WRITE_TOOLS` separately enables
individual student actions; it defaults to empty. Course policy is
checked again before course-bound student writes. Course-linked
planner override creates and updates also require the
`mark_module_item_done` operator flag and course permission,
because Canvas synchronizes the forwarded completion state. Registering the
`all` profile does not grant Canvas permissions or exempt a student
from these checks.

New group, assessment and communication changes use previews with
single-use tokens and re-read their targets when confirmed. Group
membership changes refuse unknown submission status and implicit
moves between groups. Group-set deletion requires an empty group inventory.
These checks reduce unintended changes; Canvas does not offer an
atomic preview-and-write transaction across multiple endpoints.
Canvas may hide SIS metadata from an educator, so absent SIS
fields do not certify that a group was never institution-managed.
Group deletion checks visible collaborations and content; legacy
external-provider content may not be enumerable. Concurrent group
or content additions after preflight can still be deleted by the
native API. Keep those objects quiescent during deletion.

### Deliberate remaining boundaries

These are scope or policy decisions, not unimplemented promises:

- Account administration, SIS enrollment synchronization,
  authentication, developer keys and course lifecycle management
  remain outside a course-level assistant's scope.
- External-tool configuration, institution-specific integrations,
  grade passback, SIS imports and differentiation tags require
  separate deployment and permission decisions.
- Quiz attempts, group submissions, assessment-structure replacement
  and student impersonation remain excluded by existing policy.
- Appointment booking/cancellation and automated student allocation
  can reserve scarce resources or change other people's schedules
  and groups; no general-purpose endpoint bypass is provided.
- Nonempty group deletion and section cross-listing can destroy or
  relocate records. Use Canvas's own workflows for those operations.
- Student group self-signup is excluded because changing group
  membership can affect classmates' assignments. Educator tools
  provide explicit, checked membership changes.

The deferred live acceptance checks above apply to Stage 1b too.
Local tests establish tool behavior against documented contracts,
not the exact features or permissions of a particular institution.

### Repository validation

The completed local Stage 1b pass passed 2,509 Python tests with
22 skips, Ruff, mypy across 64 source files, and the generated
manifest consistency check. A clean installed wheel passed mocked
stdio reads in creator, educator and student profiles with
anonymization enabled. The wheel's five new modules were checked
against the working sources; local credentials and evidence were
excluded. These tests did not contact a live Canvas instance.

## Sources

Audited against the official Canvas API and, where the published
schema is incomplete, linked Instructure source. Descriptions
above summarize implemented scope and local design decisions.

- [Assignments](https://developerdocs.instructure.com/services/canvas/resources/assignments)
- [Assignment groups](https://developerdocs.instructure.com/services/canvas/resources/assignment_groups)
- [Quizzes](https://developerdocs.instructure.com/services/canvas/resources/quizzes)
- [Quiz groups](https://developerdocs.instructure.com/services/canvas/resources/quiz_question_groups)
- [New Quiz items](https://developerdocs.instructure.com/services/canvas/resources/new_quiz_items)
- [Pages](https://developerdocs.instructure.com/services/canvas/resources/pages)
- [Modules](https://developerdocs.instructure.com/services/canvas/resources/modules)
- [Files](https://developerdocs.instructure.com/services/canvas/resources/files)
- [Rubrics](https://developerdocs.instructure.com/services/canvas/resources/rubrics)
- [Courses](https://developerdocs.instructure.com/services/canvas/resources/courses)
- [Navigation tabs](https://developerdocs.instructure.com/services/canvas/resources/tabs)
- [Outcomes](https://developerdocs.instructure.com/services/canvas/resources/outcomes)
- [Outcome groups](https://developerdocs.instructure.com/services/canvas/resources/outcome_groups)
- [Content migrations](https://developerdocs.instructure.com/services/canvas/resources/content_migrations)
- [Content exports](https://developerdocs.instructure.com/services/canvas/resources/content_exports)
- [Sections](https://developerdocs.instructure.com/services/canvas/resources/sections)
- [Late policy](https://developerdocs.instructure.com/services/canvas/resources/late_policy)
- [Groups](https://developerdocs.instructure.com/services/canvas/resources/groups)
- [Group categories](https://developerdocs.instructure.com/services/canvas/resources/group_categories)
- [Discussions](https://developerdocs.instructure.com/services/canvas/resources/discussion_topics)
- [Conversations](https://developerdocs.instructure.com/services/canvas/resources/conversations)
- [Planner](https://developerdocs.instructure.com/services/canvas/resources/planner)
- [Calendar events](https://developerdocs.instructure.com/services/canvas/resources/calendar_events)
- [Bookmarks](https://developerdocs.instructure.com/services/canvas/resources/bookmarks)
- [Favorites](https://developerdocs.instructure.com/services/canvas/resources/favorites)
- [Outcome results](https://developerdocs.instructure.com/services/canvas/resources/outcome_results)

## Stage 3: local evidence and reliability

Educator/all profiles add scoped record capture, local integrity
verification, conservative comparison, selected identity lookup,
and explicitly selected attachment downloads. These tools are
local stdio only. Capture and downloads use Canvas GETs; local
writes remain subject to the operator's tool policy. See
[record snapshots](record-snapshots.md) for schema, privacy,
limits, resume behavior and recovery boundaries.

Grades now have per-target readback/recovery outcomes. Course-date
verification checks identity as well as requested values. Message
responses distinguish queued work, observed conversation creation
and uncertain delivery. These checks do not prove that a student
read a message or make multi-target changes transactional.

### MCP and TypeScript surfaces

The TypeScript modules call Canvas directly. They are a smaller,
privileged API surface, not wrappers that inherit Python MCP
confirmation and policy checks. Code execution remains off by
default, excluded from creator mode, and unnecessary for the
ordinary-tool workflows below. Missing TypeScript coverage is
intentional unless a concrete workflow needs a safe shared API.

| Workflow | Python MCP | TypeScript named helpers |
|---|---|---|
| Course discovery/context | Course/profile/enrollment tools | `listCourses`, `getCourseDetails` |
| Submission review | Submission detail/history/analytics | `listSubmissions` |
| Reviewed grading | Rubric grading and batch recovery | `gradeWithRubric`, `bulkGrade`; direct execution does not inherit MCP previews or the new recovery wire contract |
| Discussion work | Topics/entries/replies/own-state tools | `listDiscussions`, `postEntry`, `bulkGradeDiscussion` |
| Inbox follow-up | Preview/confirmation and conversation tools | `sendMessage`; privileged direct write |
| Creator authoring | Assignments/pages/modules/files/rubrics/quizzes/settings | No corresponding named authoring helpers |
| Student planning/calendar | Self-scoped gated tools | No corresponding named helpers |
| Snapshots/identity/local recovery | Capture/verify/compare/lookup/selected attachment tools | No corresponding named helpers |
| Course export/import | Guarded export, download, migration/import tools | No corresponding named helpers |

The generic TypeScript HTTP helpers are not evidence of safe
workflow parity. Do not use them to bypass a missing MCP tool,
role, permission, confirmation or operator policy. Supported
New Quiz stimulus/item-bank authoring is still absent from the
public authoring contract checked for this work; the exclusion
remains. Quiz-taking, group submissions, impersonation, account/
SIS administration and appointment booking also remain excluded.
