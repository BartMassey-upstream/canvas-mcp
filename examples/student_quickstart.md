# Student Quick Start Guide

This guide shows common tasks students can accomplish with Canvas MCP.

## Setup

Follow [Getting started](../docs/getting-started.md), including
client configuration and the first read-only request. Set
`CANVAS_ROLE=student` in your per-user `env` file.

The guide starts with writes disabled. Read requests below work
in that setup; for actions that change Canvas, first
[enable the relevant tools](../docs/getting-started.md#enable-changes-when-ready).

## Common Tasks

### Check What's Due This Week

Just ask your AI assistant:
```
What assignments do I have due this week?
```

Your AI assistant will:
- List all upcoming assignments across all your courses
- Show due dates and times
- Indicate which ones you've already submitted

### Track Your Grades

```
Show me my current grades in all my courses
```

Your AI assistant will provide:
- Current grade in each course
- Assignment-by-assignment breakdown
- What's missing or needs attention

### Find Unsubmitted Work

```
What assignments haven't I submitted yet?
```

Perfect before deadlines! Shows everything you still need to complete.

### Peer Review Management

```
What peer reviews do I need to complete?
```

Your AI assistant will:
- List all pending peer review assignments
- Show deadlines
- Tell you how many reviews you need to do for each

### Course-Specific Questions

```
Show me everything due in BADM 350 this week
```

```
What's my current grade in CS 101?
```

```
Have I submitted all assignments for Biology 205?
```

## Tips

- **Be specific**: Mention course names or codes when relevant
- **Use natural language**: Just ask like you would ask a friend
- **Check regularly**: Make it part of your weekly routine
- **Privacy**: The server runs locally, but tool results are sent to your configured AI client; review that provider's data policy

## Common Workflows

### Monday Morning Check-in
```
What's due this week across all my courses? Show me what I've completed and what's still pending.
```

### Before Class
```
What assignments are due for MATH 221 this week?
```

### End of Semester
```
Show me my final grades in all courses and any missing assignments
```

## Troubleshooting

**"I don't see my courses"**
- Check the `env` and `token` files in the platform config directory
- Verify your Canvas API token is valid
- Run your installed launcher with `--test` to check the connection
- See [course data troubleshooting](common_issues.md#the-assistant-shows-the-wrong-or-incomplete-course-data) for permissions and course IDs

**"Some assignments are missing"**
- Canvas MCP shows what's in Canvas - check with your professor if something seems off

**Need help?** Check the [main documentation](../README.md) or [open an issue](https://github.com/BartMassey-upstream/canvas-mcp/issues).
