# Local record snapshots

Snapshots preserve scoped evidence for an educator. They do not
restore Canvas state. Course-content Common Cartridge exports
remain a separate feature; they do not archive student records.

These tools are available only in educator/all profiles on local
stdio. Capture, attachment download and identity lookup are local
writes, controlled by `ALLOWED_WRITE_TOOLS`; they make no Canvas
mutations. Creator
and student profiles cannot access them. Real record collection
still requires an approved course, client, private destination
and retention policy. Synthetic tests do not approve real use.

## Capture and resume

Call `capture_record_snapshot` with a course identifier and a
private `save_directory` (default `local_snapshots`). Its first
call returns a scope/destination preview and a confirmation
token; no student record collection or file creation occurs.
Show the preview, then confirm identical arguments after the
user approves local retention. Every capture/resume checks the
canonical course, current caller and `manage_grades` permission.

`families` defaults to course, assignments, submissions,
enrollments, sections and groups. You may also select
`peer_reviews` and `outcomes`. Course context is always included;
submissions/peer reviews require assignment context.
`assignment_ids` limits assignment-related families. Other
selected families remain course scoped, as shown in the preview.
Enrollment capture requests active, invited, completed and
inactive student enrollments; deleted records are outside scope.

| Data | Default retained fields |
|---|---|
| Course | Numeric ID, state, dates, term reference |
| Assignments | IDs, dates, points, grading/publication settings and embedded rubric criterion/rating IDs and points |
| Submissions | Assignment/user IDs, attempt, state, times, grade/score, late/missing/excused fields and rubric points/rating IDs |
| Enrollment/section/group references | Minimal IDs, roles/state, dates and group counts; no group membership reconstruction |
| Peer reviews | Visible submission/assessor/user references and completion state |
| Outcomes | Visible result IDs, numeric results and scoped references |

Optional flags are separate choices:

- `include_bodies`: available syllabus, assignment name/body and
  submission body fields.
- `include_comments`: submission and rubric comment text and
  author IDs.
- `include_identities`: available enrollment user names, email
  and login IDs.
- `include_attachment_metadata`: attachment IDs, sizes and media
  types, enabling a separate bounded download; no URLs or bytes
  are retained by capture itself.

Default archives still contain identifying numeric IDs. Optional
text may identify people too. These files are restricted data,
not anonymized public reports. Structured credentials, signed
URLs, Inbox and discussion bodies are excluded. Text fields
containing the current API token or recognized signed URLs are
withheld with an explicit field marker; this is not a general
secret detector or a guarantee that arbitrary text is anonymous.

`page_budget` limits each call to 1–100 record pages, in addition
to course/permission/caller checks. Each verified page is written
and checkpointed before advancing. A call can return:

- `complete`: all selected units completed for the caller's
  visible scope, with no detected inconsistent duplicate records;
- `partial`: a unit was unavailable/invalid, or changes were
  observed during collection;
- `interrupted`: the page budget ended before completion.

Resume by repeating the original scope with `resume_directory`
set to the returned path and obtaining a fresh preview/token.
Already verified pages remain unchanged. A failed permission
check may be retried after access is restored. An expired cursor,
missing selected assignment, detected changed duplicate, or
scope/storage limit can require a new narrower capture;
`recapture_required` makes this explicit. Never change a resume
scope to merge different courses, callers or inclusion policies.

Canvas pagination is not a database snapshot. Later inserts or
removals can shift pages without a detectable duplicate. Capture
times describe an observation window, not an atomic instant.
Independent comparisons cannot establish why a grade changed.

## Format and limits

Each immutable page is `records-<random-id>.jsonl`. Checkpoints
are exclusive `checkpoint-000001.json` files; a successful
checkpoint references only complete, flushed record files and
includes their SHA-256 digests and sizes. No reusable confirmation
token or signed pagination URL is written to disk. Only a bounded
page cursor is retained; requests are rebuilt from validated scope.

Schema version 1 includes origin, canonical course/caller IDs,
package version, scope, observation times, generation, per-unit
state/counts, unavailable-field counts and consistency limits.
It is a selected-field evidence format, not a byte-for-byte dump
of every API field or a full Canvas backup.

Linux directory handles refuse symlinks, traversal, insecure
permissions, nonregular files and linked file aliases. Directories
must be owned by the current user with mode `0700`; files use
`0600`, independent of a permissive umask. A directory lock
prevents concurrent cooperative capture/read operations. Local
hashes detect corruption; they are not signatures proving origin
against someone who can rewrite both data and manifests.

Limits include 1 MiB per record, 32 MiB per member, 4 MiB per
checkpoint, 256 MiB for the entire directory including history,
100,000 records per unit and 1,000 captured assignments for
dependent collection. Reduce scope when a limit is reached.
Temporary/orphan files after process termination remain private;
they are not counted as verified records. Preserve the directory
for inspection, or remove the entire disposable capture and
start again. Do not hand-edit a checkpoint to force completion.

## Verify and compare

`verify_record_snapshot(snapshot_directory)` performs local
schema, scope, file-permission, digest, duplicate, count and
completion checks. It returns only safe summary counts. Invalid
or missing files fail verification; a valid partial archive is
reported as partial, not as a completed capture. No Canvas access
is needed.

`compare_record_snapshots(before_directory, after_directory)`
requires matching schema, origin, course, caller and scope.
It returns per-family counts of changed observations and, only
when both families have complete coverage, additions/removals.
Incomplete coverage yields unknown additions/removals instead
of interpreting missing observations as deleted students/work.
It does not return names, student IDs, comments or raw values.

## Selected attachment downloads

After capture with `include_attachment_metadata=true`, call
`download_snapshot_attachments` with the verified
`snapshot_directory` and 1–20 `selected_files`. Each selection
has `assignment_id`, `user_id` and `file_id` from that capture.
Missing attachment metadata is reported as unavailable, never
inferred to mean no attachments exist.

The first call previews the selected references, destination and
limits. After approval, confirm identical arguments with its
token. The tool rechecks course/caller/permission and fetches the
current submission before downloading. It refuses attachments
that no longer match the selected submission references.

Defaults are 10 MiB per file and 20 MiB total. You may lower or
raise `max_file_bytes` and `max_total_bytes` up to 32 MiB and
256 MiB respectively; the private directory also has a 256 MiB
aggregate limit including metadata. Downloads stream with byte
limits, independent hashes and exclusive private filenames.
They allow HTTPS on the configured public Canvas origin or
recognized public S3 hosts, with bounded redirects. Credentials
are sent only to Canvas. Unsupported storage hosts remain an
explicit limitation; no general URL downloader is exposed.

Completed files survive a later failure. Inspect the returned
per-file states and verify the bundle using
`verify_record_snapshot`. Missing completion markers report
interruption; corrupt or mismatched files fail verification.
Attachment bundles are verified separately and cannot be inputs
to record comparison. Downloads have no automatic retry/resume;
request only remaining files in a new reviewed bundle.

The stored parent checkpoint digest links the bundle to the
record capture. Later downloaded bytes are a separate observation,
not proof of file contents at the original capture time. Neither
signed URLs nor credentials are stored in the bundle.

## Selected local identity lookup

`lookup_student_identities` reads an existing private identity-map
bundle produced by `create_student_anonymization_map`. Supply its
`map_directory`, 1–50 distinct pseudonyms, numeric course ID and a
private output destination. It checks origin/course/version,
algorithm, file integrity and age (seven days by default).
Future-dated, stale, incompatible or incomplete maps are refused.
Capture time cannot prove the current roster is unchanged.

Only the selected records are written to a new private bundle,
as raw JSON and spreadsheet-safe CSV. The tool returns paths and
counts, never names or emails. The human can open those files
locally. Pasting their contents into Claude or Codex would disclose
those identities to that client; it is a separate choice.
Pseudonyms retain the existing stable algorithm and remain
linkable across courses/origins.

## Recovery and retention

| Captured evidence | Recovery boundary |
|---|---|
| Course/assignment/rubric context | Reference for manual comparison; use the separate approved content-export/import workflow for course content |
| Grades and rubric assessments | Evidence for a reviewed correction; no automatic grade replay or rollback |
| Submission attempts/status/body | Evidence only; original attempt history cannot be recreated by replaying a submission |
| Enrollment/section/group references | Evidence only; SIS/enrollment administration and automatic membership restoration are excluded |
| Peer-review and outcome records | Evidence/comparison; no replay of completion or mastery state |
| Attachment bytes | Preserved evidence, not permission to resubmit work |
| Missing/unavailable data | Cannot establish absence or reconstruct an unavailable record |

Choose retention and storage protection before collecting real
records. File modes provide access control, not encryption.
Delete obsolete captures, identity maps, selected-identity files,
attachment bundles, temporary files and secondary copies under
the approved retention policy. Deleting a file does not promise
physical erasure from SSDs or backups. No automated retention
scheduler or restoration writer is installed.

API contracts: [submissions](https://developerdocs.instructure.com/services/canvas/resources/submissions),
[enrollments](https://developerdocs.instructure.com/services/canvas/resources/enrollments),
[peer reviews](https://developerdocs.instructure.com/services/canvas/resources/peer_reviews)
and [outcome results](https://developerdocs.instructure.com/services/canvas/resources/outcome_results).
Capture never requests submission `read_status`, which can itself
change read state despite using a GET endpoint.
