<p align="center">
  <img src="docs/canvas-mcp-header.png" alt="Canvas MCP — AI tools for Canvas LMS" width="800">
</p>

# Canvas MCP

Canvas MCP connects an AI assistant to Canvas LMS. Ask about
upcoming assignments, inspect a course, build course content,
or review submissions through your assistant's chat interface.

This is **Bart's fork** of
[vishalsachdev/canvas-mcp][upstream]. It includes additional
course-building tools and safeguards. The published PyPI package
and upstream desktop extension follow upstream releases; they
may lack changes described here. Install this repository to use
this fork.

## Quick start

**New to MCP? Start with [Getting started](docs/getting-started.md).**
It explains the pieces and takes you from installation through
credentials, client configuration, and your first request on
Linux, macOS, or Windows. You do not need to write Python.

The short version, once Git and [uv][uv-install] are installed:

```sh
git clone https://github.com/BartMassey-upstream/canvas-mcp.git
cd canvas-mcp
uv tool install --python 3.12 .
git rev-parse HEAD
uv tool dir --bin
```

Then:

1. Save your Canvas URL and private token using the
   [configuration steps](docs/getting-started.md#configure-canvas-access).
2. [Connect your MCP client](docs/getting-started.md#connect-your-assistant)
   to the installed `canvas-mcp-server` executable.
3. Restart or reconnect the client, then ask:
   “List my Canvas courses. Do not change anything.”

Your AI application starts the server for you. Installing the
package alone does not connect it to your assistant. Starting
it normally, or running `canvas-mcp-server --test`, contacts
Canvas to check your credentials. Neither check changes Canvas.

## What MCP does

[MCP, the Model Context Protocol][mcp-intro], lets AI
applications use external tools. Your assistant is the **client**;
this package is the **server** that translates tool requests
into Canvas API calls using your account's permissions.

```text
You → AI assistant → local Canvas MCP server → Canvas
```

The normal setup uses **stdio**: the assistant launches a local
process and communicates with it through its input and output.
You do not need a web server, an open port, Docker, Node.js, or
an additional OpenAI API key to run this Python server. Your
chosen AI client has its own account and software requirements.
Tool results reach that client and its model provider, even
though the Canvas MCP process and token file are local.

## Choose a tool profile

Set `CANVAS_ROLE` in your configuration's `env` file:

| Profile | Use it for |
|---|---|
| `student` | Your assignments, grades, and shared course content |
| `creator` | Building courses without exposing student records |
| `educator` | Teaching, rosters, submissions, grading, and messaging |
| `all` | The combined tool set; the server default |

Profiles select available tools; they do not grant Canvas
permissions. The getting-started guide begins with writes
disabled so you can check the connection before enabling edits.
Student write tools and TypeScript execution require separate
opt-ins. See [enabling changes](docs/getting-started.md#enable-changes-when-ready).

## Things to try

After completing setup, ask your assistant:

- **Student:** “What assignments are due this week?”
- **Course creator:** “Show the modules and pages in my course.”
- **Educator:** “Show submission status for Assignment 3.”
- **Accessibility:** “Scan this course's pages and assignments
  for accessibility issues. Report findings without fixing them.”

Include the course code or Canvas course ID when selecting a
course. Read the results before asking for changes. For an
initial edit, choose a small change in a test course and inspect
the result in Canvas.

The fork supports course pages and revisions, modules, Classic
and New Quizzes, assignments, rubrics, files and folders,
course settings, outcomes, announcements, and local course
content backups. Availability depends on your profile,
configuration, and Canvas permissions. See the
[tool reference](tools/README.md) for exact operations and
[API coverage](docs/api-coverage.md) for limits.

## Using changes and confirmations

Many deletions and messaging actions require two calls: the
first returns a preview and confirmation token; the assistant
shows you the preview and waits for your decision before making
the second call. Other edits may take effect in one call.
Your client's approval settings also control tool use.

Content edits can check an earlier timestamp or body hash and
verify what Canvas stored. They are not atomic locks. If an
edit is reported as **unconfirmed**, inspect Canvas before
retrying: the save may have happened, including with Canvas
rewriting the HTML. See the
[content-edit reference](tools/README.md#safer-content-edits).

Anonymization is available for educator workflows through
`ENABLE_DATA_ANONYMIZATION=true`. It reduces supported identity
fields in tool results; it does not establish institutional
privacy compliance. Use your institution's rules when deciding
what data to share with an AI provider.

## Documentation

- [Getting started](docs/getting-started.md): installation,
  token files, clients, first requests, and upgrades.
- [Troubleshooting](examples/common_issues.md): missing tools,
  executable paths, authentication, and uncertain saves.
- [Student workflows](examples/student_quickstart.md).
- [Educator workflows](examples/educator_quickstart.md).
- [Tool reference](tools/README.md) and
  [machine-readable catalog](tools/TOOL_MANIFEST.json).
- [API coverage and boundaries](docs/api-coverage.md).
- [AI agent guide](AGENTS.md): how to use the tools correctly.
- [Development guide](CLAUDE.md): working on the server.
- [Fork maintenance plan](docs/fork-maintenance-plan.md) and
  [current maintainer handoff](PLAN.md).

## Optional workflow skills

Skills teach an assistant multi-step workflows using the server's
existing tools. They are optional and do not install or connect
the MCP server. This checkout's [skills directory](skills/)
contains the available recipes, including course backup.
See [installing a skill](docs/getting-started.md#optional-workflow-skills)
after your first connection works.

## Advanced setups

For shared institutional hosting, see
[authenticated Azure deployment](deploy/azure/). The former
public hosted endpoint is retired. The normal newcomer setup
is the local stdio connection above.

The optional [TypeScript code API](src/canvas_mcp/code_api/README.md)
is for custom bulk processing. `execute_typescript` is disabled
by default. Enabling it gives code privileged Canvas access
outside the normal confirmation and content-fencing guarantees;
keep it disabled unless you need that capability.

## Contributing

Start with the [development setup](CLAUDE.md#environment-setup),
then the [fork handoff](PLAN.md) for current scope and checks.
Keep fixes small and include relevant validation. Review the
[repository instructions](AGENTS.md) before creating commits,
pushing, publishing, or making live Canvas changes.

## Credits and license

Originally created by [Vishal Sachdev][upstream], with
contributions from the upstream community and Bart's fork.
Released under the [MIT license](LICENSE).

[upstream]: https://github.com/vishalsachdev/canvas-mcp
[uv-install]: https://docs.astral.sh/uv/getting-started/installation/
[mcp-intro]: https://modelcontextprotocol.io/docs/getting-started/intro
