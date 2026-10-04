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
