# Canvas MCP fork maintenance and student handoff

## Current decision October 12 2026

The fork is adequate for Bart's needs, and a colleague is
experimenting with it. Bart does not currently want to take on
a larger maintenance project. Complete the bounded stabilization
pass, retain the upstream review, and inspect new upstream work
next week when requested. Bart will try to find a student to
take responsibility for the broader effort.

The five-stage proposal below is retained for that future owner.
Only stage 1 is currently authorized. Stages 2 through 5 are
deferred proposals, not tasks to resume automatically. There is
no commitment to a weekly service, feature parity, a broad merge,
or publishing contributions. No scheduled automation is set up.

On November 10, Bart authorized a second bounded safety pass:
security dependencies, file-transfer credential scoping and
content fencing, and rubric/announcement guards. The
[current handoff](../PLAN.md) records that pass and its checks.
The safety pass was committed as `ef5a2fa` and installed. Bart
then authorized the accessibility fixes, their commit, and their
installation. The next upstream comparison starts at `61f43d9`;
the broader stages below remain deferred.

## Possible followups, deliberately deferred

These are options for future demand, not an implementation queue.
Prefer a small repair to a workflow Bart or a colleague uses.

- Peer-review reminders: bind composition, confirmation, and
  delivery to one resolved numeric course ID. This is a small
  correctness fix if followup campaigns are used.
- Other read summaries: preserve Canvas's explicit missing flag
  even before a due date, disclose inbox pagination and sampled
  course overviews, and reuse the validated HTML tokenizer in
  older syllabus/text readers when needed.
- Guarded content edits: expected timestamps or hashes and
  readback for pages, assignments, discussions, and syllabus.
  Accessibility edits now have their own conservative checks.
- Practical acceptance: colleague/client feedback and an
  authorized sandbox backup/restore drill for relied-on flows.
  Synthetic validation does not establish live recovery.
- Windows use: private downloaded-file DACLs and timezone data
  if the colleague uses Windows. A warning alone is not an ACL.
- Hosted use: fail closed on an empty Entra allowlist and isolate
  anonymization statistics across callers if HTTP deployment is
  actually needed. These do not affect today's local workflow.
- TypeScript execution: keep it disabled unless needed. Before
  enabling it, adopt the Node permission-model changes; they
  still do not enforce the normal write-confirmation boundary.
- Optional course workflows: bounded bulk submission text or
  CSV group provisioning if a concrete course task needs them.
  Existing individual submission and group tools remain usable.
- Optional student features: activity feeds, grade scenarios,
  own-group discussions, quiz-attempt awareness, student Inbox
  writes, and aggregated calendars. Select by actual need.
- Usability options: exact full course-name matching with
  suggestions, anonymous-discussion reads, or lazy package
  imports if those limitations become relevant.
- Maintenance ownership: a student could keep a bounded review
  ledger and prepare small upstream contributions. A broad merge,
  rewrite, recurring service, and feature parity are not goals.

## Stage 1 Stabilize existing workflows

Completed locally in the October 12 stabilization pass. The
[current handoff](../PLAN.md) records verification and release
status. The scope below is retained for the future maintainer.

Address the defects identified in the
[October upstream review](upstream-review-2026-10-12.md):

1. Remove placeholder submissions from peer-review assignment.
2. Reject ambiguous course codes before target dispatch.
3. Isolate HTTP course metadata and course-policy checks.
4. Preserve full rubric and requested discussion content.
5. Correct peer-review attribution, pagination, and failures.
6. Follow Canvas missing flags and classify graded/offline work.
7. Bound and fence diagnostic output while preserving internal
   write-outcome evidence.

Verify each defect with a regression, then run the full Python
suite and lint/type checks. Keep current path validation,
confirmation, privacy, and uncertain-write safeguards. Applicable
packaging and synthetic client checks should confirm the change
remains usable. Record the result in PLAN.md.

No upstream feature expansion is needed to finish this stage.
Commits, releases, installation changes, and external actions
remain separately authorized operations.

## Stage 2 Establish a stable version for users

When an owner is available, select a pinned fork release for
Bart and colleagues, with clear upgrade notes. Development can
move faster than user installations.

Complete actual Claude/Codex client acceptance and colleague
feedback for the workflows users depend on. The current handoff
records extensive synthetic validation; actual client and live
sandbox acceptance are separate. Obtain explicit authorization
before any Canvas write testing or installation changes.

Document specific supported workflows, clients, and platforms.
Do not substitute a tool count for evidence that workflows work.

## Stage 3 Review upstream within a bounded maintenance budget

A future owner may propose a weekly review, with prompt review
of relevant security advisories. Keep a short ledger containing
upstream commits, applicability, fork resolution, and checks.

- Adapt applicable security and correctness fixes promptly.
- Record fixes already implemented independently in the fork.
- Adopt features only for a demonstrated user need.
- Leave unrelated features upstream.
- Evaluate infrastructure and dependency redesign separately.

Start the next comparison at `61f43d9`, rather than repeating
the entire divergence review. Keep the prior review's open-PR
states as historical evidence and verify their current state.
Guarded content edits would be a useful first feature proposal;
feeds, grade scenarios, and student messaging can wait for
demand.

## Stage 4 Extract selected upstream contributions

Keep the integrated fork. Build small contribution branches
against current upstream and reassess overlap before extracting
old commits. The older
[Stage 2 extraction plan](../PLAN.md#stage-2-prepare-upstream-contributions)
is an inventory, not proof that its branches still apply cleanly.

Suggested contribution order:

1. Narrow client correctness safeguards with reproductions.
2. Path validation and construction, separate from broad tools.
3. Native credential configuration and migration behavior.
4. Explicit nullable-field clearing.
5. Creator-profile boundaries, after maintainer design agreement.
6. New Quizzes, exports, and snapshots as separate proposals.

Keep one or two PRs in flight. Large Stage 1/3 commits need
splitting by behavior. Fork release machinery and Bart-specific
documentation stay local. Validate extracted branches on their
own; the integrated fork's tests do not certify those branches.

Upstream has invited small contributions from another large
fork while declining a complete infrastructure import. This
supports selective contribution, not guaranteed acceptance of
our work. See the
[maintainer response][maintainer]. Publication needs explicit
permission; draft proposals and patches can be prepared locally.

## Stage 5 Reassess the maintenance commitment

After four to six weeks of owner-led maintenance, assess whether
fixes are straightforward to adapt, accepted contributions reduce
recurring differences, and colleagues use the pinned release
reliably. Compare the maintenance cost with the time available.

If shared-core conflicts repeatedly dominate, narrow the fork's
scope or explicitly maintain an independent project. Consider
plugins or a major rewrite only after demonstrating their need.
Upstream acceptance should reduce the burden over time without
blocking dependable local software.

## Starting checklist for a student maintainer

- Read the current PLAN.md handoff and this decision first.
- Inspect the working tree; preserve existing uncommitted work.
- Reproduce the recorded checks before changing behavior.
- Ask Bart which workflows and platforms need maintenance.
- Fetch upstream read-only and compare from the recorded
  baseline.
- Bring a small prioritized proposal based on actual needs.
- Keep synthetic tests distinct from live acceptance evidence.
- Obtain separate permission for commits, pushes, releases,
  installations, Canvas writes, and public GitHub actions.

[maintainer]: https://github.com/vishalsachdev/canvas-mcp/issues/484#issuecomment-6073947661
