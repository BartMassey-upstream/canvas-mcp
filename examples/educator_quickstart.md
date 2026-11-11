# Educator Quick Start Guide

This guide shows the most common tasks educators use Canvas MCP for.

## Setup

Follow [Getting started](../docs/getting-started.md), including
client configuration and the first read-only request. Set
`CANVAS_ROLE=educator` in your per-user `env` file.
Use `creator` instead for course building without student
records. For student data, review your institution's AI policy
and consider `ENABLE_DATA_ANONYMIZATION=true`.

The guide starts with writes disabled. Read requests below work
in that setup; for actions that change Canvas, first
[enable the relevant tools](../docs/getting-started.md#enable-changes-when-ready).

## Common Tasks

### Check Submission Status

```
Which students haven't submitted Assignment 5 in CS 101?
```

Your AI assistant will show you who's missing submissions and help you follow up.

### Grade Assignments with Rubrics

```
I need to grade Assignment 3 in BADM 350 using the rubric
```

Your AI assistant can:
- Show you the rubric criteria
- Help you grade submissions
- Apply consistent grading across students

### Send Reminders

```
Send a reminder to students who haven't submitted the midterm project
```

Your AI assistant will:
- Identify students with missing submissions
- Draft a message
- Show the preview and wait for your approval
- Send through Canvas only after you confirm

### Analyze Participation

```
Who hasn't participated in the Discussion 4 in my Intro to Python course?
```

Get quick insights into discussion participation.

### Bulk Operations

For many submissions, ask for a proposed grading batch first:

```
Prepare grades for these submissions using the assignment rubric.
Show me the proposed grades and comments before saving anything.
```

After you review and approve the grades, the ordinary
`bulk_grade_submissions` tool can apply them. Custom TypeScript
execution is optional and disabled by default; it bypasses the
usual confirmation and content-fencing guarantees.

### Check Assignment Statistics

```
Show me statistics for Quiz 3 in ECON 102
```

Get:
- Average score
- High/low scores
- Submission rate
- Common issues

## Advanced Workflows

### Weekly Check-in

```
For CS 225, show me:
1. Who's falling behind (missing multiple assignments)
2. Discussion participation rates
3. Average grades on the latest assignment
```

### Grade Discussion with Rubric

```
Grade Discussion Topic 5 in BADM 350. Give 10 points for initial post, 5 points per peer review (must do 2), max 10 points for peer reviews.
```

Uses bulk discussion grading - super fast!

### Peer Review Analysis

```
Show me peer review completion for Assignment 4. Who hasn't done their reviews?
```

### Course Health Check

```
Give me an overview of MATH 221:
- How many students are current on assignments?
- What's the average grade?
- Who might need extra support?
```

## FERPA & Privacy Tips

With `ENABLE_DATA_ANONYMIZATION=true` (a privacy control, not a complete compliance determination):
- Student names appear as "Student_xxxxxxxx"
- You can still identify patterns and get insights
- Supported identity fields are masked before tool results reach the AI client
- You keep a local mapping to correlate IDs with real students

Example anonymized output:
```
Missing submissions:
- Student_a8f7e23d (submitted 2/5 assignments)
- Student_c9b21f84 (submitted 1/5 assignments)
```

## Performance Tips

**For small operations (< 10 students):** Just ask naturally

**For bulk operations:** Review a batch of proposed grades, then
ask the assistant to apply the approved batch with the ordinary
bulk grading tool.

## Common Pitfalls

❌ **Don't**: "Grade all assignments" (too vague)
✅ **Do**: "Grade Assignment 5 in CS 101 using the provided rubric"

❌ **Don't**: Send messages without reviewing
✅ **Do**: "Draft a message to students missing Assignment 3, let me review before sending"

❌ **Don't**: Assume old data is current
✅ **Do**: Canvas MCP fetches fresh data, but cache may be a few minutes old

## Troubleshooting

**"Can't access student data"**
- Check that your Canvas API token has the right permissions
- Some institutions restrict educator access - contact your Canvas admin

**"Bulk operations are slow"**
- Narrow the request to one assignment and a reviewed batch
- Ask for ordinary bulk grading after approving the grades

**"Anonymization isn't working"**
- Verify `ENABLE_DATA_ANONYMIZATION=true` in the per-user `env` file
- Restart your MCP client after changing settings

Need help? Check the [troubleshooting guide](common_issues.md) or [open an issue](https://github.com/BartMassey-upstream/canvas-mcp/issues).
