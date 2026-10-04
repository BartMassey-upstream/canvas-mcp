---
name: canvas-peer-review-manager
description: Review Canvas peer-review completion and comment evidence, prepare scoped educator follow-up, and send or change assignments only after approval.
---

# Canvas Peer Review Manager

Use educator/all profile with Canvas instructor/TA permissions.
Works in Claude or Codex. Discover available tools and inspect
signatures using `search_canvas_tools`; creator intentionally
excludes student records. Use minimal IDs/pseudonyms by default.
Server anonymization protects supported fields, not a complete
compliance guarantee or permission to publish student records.

## Read and draft

Establish course/assignment IDs, reporting interval, deadline,
timezone and any section/group scope. Read assignment details to
verify peer reviews are enabled and distinguish assignment due
date from peer-review due date. Use
`get_peer_review_completion_analytics`, `list_peer_reviews` and,
when mappings are needed, `get_peer_review_assignments` with
`include_names=false` and minimal submission details.

Report reviews completed/reviews assigned separately from
reviewers fully complete/reviewers assigned. A reviewer with no
completed reviews is “0 completed”, not proven “not started”.
Do not use course enrollment as a denominator unless verified
eligible and assigned. Empty/unavailable mappings do not prove
100% completion. Record observation time and source tool/IDs.

For requested comment review, call `get_peer_review_comments`
with `anonymize_students=true` explicitly (default is false).
Heuristic `analyze_peer_review_quality` and
`identify_problematic_peer_reviews` results are signals for human
review, not objective grades, motivation or diagnosis. Quote
only retrieved evidence; flag truncated/unavailable text.
`get_peer_review_followup_list` priorities are tool heuristics,
not independent proof of deadline violations.

Prepare a deduplicated recipient list, factual draft and exact
course/assignment scope. Weekly educator review may also use
`list_submissions`, `get_assignment_analytics` and selected
`get_course_outcome_results`/`get_course_outcome_rollups`; inspect
all pages via their explicit page/next-page fields. Keep counts
and denominators separate, label unavailable or partial data,
and do not turn monitoring into automatic communication.

## Approved action phase

Messaging tools use two calls. First call
`send_peer_review_inbox_messages` with the chosen `recipient_ids`
and draft or `send_peer_review_followup_campaign` only if its
segmentation is wanted. Show the actual recipient/message
preview. After approval repeat identical arguments with the
returned `confirmation_token`. A campaign confirming call sends
real messages; this skill authorizes no autonomous campaign.

`assign_peer_review` currently has no token or dry run: review
the exact reviewer/reviewee IDs and current mapping before an
approved call. `unassign_peer_review` requires preview/token and
refuses completed reviews. Read mappings after allocation
changes; respect guards rather than deleting work to bypass them.
Inspect any unfamiliar write signature before calling it.

On partial delivery, retain confirmed recipient results and
unconfirmed IDs. Re-read status where available; do not send the
same reminder again because a response timed out. Stop at an
expired or changed preview and generate a new reviewed preview.
Ordinary bounded batches suffice; large classes do not require
privileged code execution.

## Export and report

Only save records in an explicitly chosen authorized destination.
`extract_peer_review_dataset` defaults to local saving; use
`save_locally=false` for an inline review, and explicitly choose
`anonymize_data=true`. Reports can include sensitive evidence;
use `include_student_names=false` where available. Report
observation time, source IDs, actual coverage, completed actions,
unconfirmed outcomes and next steps. Do not automatically export,
message or schedule at the end of a read-only review.

## Synthetic example

“Review progress; draft reminders, don't send.” If 18/30 reviews
are complete and 4/10 reviewers finished all reviews, report
60% of reviews and 40% of reviewers, with the retrieval time.
Two inaccessible reviews remain unavailable, not zero-quality.
Draft one message per deduplicated recipient and make zero writes.
