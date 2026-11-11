# Getting started with Canvas MCP

Connect your AI assistant to your own Canvas account, then make
a first read-only request. These instructions install Bart's
fork from this repository on Linux, macOS, or Windows.

Follow these sections in order; choose just your OS and client
where alternatives are shown:

1. [Install the fork](#install-this-fork).
2. [Configure Canvas access](#configure-canvas-access).
3. [Test the connection](#test-the-connection).
4. [Connect your assistant](#connect-your-assistant).
5. [Make your first request](#make-your-first-request).

After that, you can [enable changes](#enable-changes-when-ready)
and add optional skills.

## Understand the pieces

You need three things:

- A **Canvas account** and an API token issued for that account.
- An **MCP client**: an AI application that can launch local MCP
  servers, such as Codex, Claude Code, or Claude Desktop.
- This **Canvas MCP server**, installed on the computer where
  your client runs its local tools.

MCP stands for Model Context Protocol. It lets your assistant
ask this server to call Canvas tools. You use normal language
in the assistant; you do not need to write API calls or Python.
The assistant starts and stops the server process for you.

These instructions use a local connection called **stdio**.
A chat website that only accepts remote server URLs cannot
launch this executable. Use a client with local MCP support,
or ask your administrator about an authenticated hosted setup.

Canvas MCP uses your token's existing Canvas permissions.
Results are sent to your AI client and its provider. Check your
institution's policy before using student records with AI.

## Install this fork

Install [Git][git-install] and [uv][uv-install] if needed.
Open Terminal on Linux/macOS, or PowerShell on Windows, and
check that both commands are available:

```sh
git --version
uv --version
```

Restart your terminal after installing them if either command
is not found. You need Python 3.11 or newer; the command below
asks uv to use Python 3.12 and download it if needed.

```sh
git clone https://github.com/BartMassey-upstream/canvas-mcp.git
cd canvas-mcp
uv tool install --python 3.12 .
git rev-parse HEAD
uv tool list
uv tool dir --bin
```

This installs a copy in an isolated user environment. You do
not need to activate a virtual environment or keep a server
running in the terminal. Record the commit ID printed by
`git rev-parse HEAD`: fork commits can share a package version.
`main` evolves; if a maintainer gave you a reviewed commit or
tag, run `git checkout --detach <revision>` before installing,
replacing `<revision>` with that value.

If uv reports that `canvas-mcp` is already installed, decide
whether to replace that installation. To replace it with this
checkout, run `uv tool install --force --python 3.12 .`.
Installing `canvas-mcp` by package name, or an upstream `.mcpb`
extension, instead selects the upstream distribution.

The last command prints the directory containing the launcher:
`canvas-mcp-server` on Linux/macOS, or `canvas-mcp-server.exe`
on Windows. Keep its **absolute path** for the client setup.
For example, a Linux path might be
`/home/alex/.local/bin/canvas-mcp-server`.

To use the command by name, run `uv tool update-shell` and open
a new terminal. The client should still use the absolute path.
You can check the launcher without contacting Canvas:

```sh
canvas-mcp-server --help
```

If your terminal cannot find it yet, use the absolute path.
In PowerShell, invoke a quoted executable path with `&`:

```powershell
& 'C:\Users\Alex\.local\bin\canvas-mcp-server.exe' --help
```

Replace example paths with the path on your own computer.

## Configure Canvas access

### Get a Canvas token

Sign in to your institution's Canvas site. Under **Account →
Settings → Approved Integrations**, look for **New Access Token**.
Give it a recognizable purpose, choose an appropriate expiry,
and copy the generated value when shown. If token creation is
unavailable, ask your institution's Canvas administrator or IT
help desk how to request API access.

Treat the token like a password. Store it in the private file
below, rather than a chat message, shell command, Git checkout,
or client configuration. A browser login password is not an
API token.

Use the Canvas site address where you sign in, followed by
`/api/v1`. For example:
`https://your-institution.instructure.com/api/v1`.
Use your institution's actual hostname, including custom domains.
Do not use a course URL, an SSO login page, or a public MCP URL.

### Choose a profile

Pick the role that matches your work:

| `CANVAS_ROLE` | Available work |
|---|---|
| `student` | Your grades and assignments; shared course content |
| `creator` | Course construction; no student records or grading |
| `educator` | Course management, student work, and grading |
| `all` | Combined tools; default if no role is set |

The profile controls which tools appear, not your Canvas account
role. For course design, start with `creator`. For your own
schoolwork, start with `student`.

### Create the configuration files

Canvas MCP reads two files from a per-user directory:

| System | Directory |
|---|---|
| Linux | `${XDG_CONFIG_HOME:-$HOME/.config}/canvas-mcp` |
| macOS | `~/Library/Application Support/canvas-mcp` |
| Windows | `%APPDATA%\canvas-mcp` |

The **`env`** file holds settings. The **`token`** file holds
only the raw token, without quotes or `CANVAS_API_TOKEN=`.
Neither filename has an extension. A trailing newline is fine.

**Linux:** set the directory variable in your terminal:

```sh
canvas_config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/canvas-mcp"
```

**macOS:** use this instead:

```sh
canvas_config_dir="$HOME/Library/Application Support/canvas-mcp"
```

Then, on either Linux or macOS:

```sh
umask 077
mkdir -p "$canvas_config_dir"
touch "$canvas_config_dir/env" "$canvas_config_dir/token"
chmod 600 "$canvas_config_dir/env" "$canvas_config_dir/token"
```

Open both files in a text editor. On macOS use plain text mode,
not rich text. Paste the token into `token`; put the settings
shown below in `env`. If these files already contain settings,
review them before editing. The token file must remain a regular
file with permissions exactly `0600`; symlinks are refused.

**Windows:** in PowerShell, create the directory:

```powershell
$canvasConfigDir = Join-Path $env:APPDATA 'canvas-mcp'
New-Item -ItemType Directory -Force $canvasConfigDir
```

For a new installation, create the two empty files and restrict
the token to your account before opening them:

```powershell
New-Item -ItemType File (Join-Path $canvasConfigDir 'env') -ErrorAction Stop
New-Item -ItemType File (Join-Path $canvasConfigDir 'token') -ErrorAction Stop
$canvasAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls (Join-Path $canvasConfigDir 'token') /inheritance:r /grant:r "${canvasAccount}:(F)"
notepad (Join-Path $canvasConfigDir 'env')
notepad (Join-Path $canvasConfigDir 'token')
```

Wait for Notepad to open, edit, and save each file. Keep the
filenames `env` and `token`, without `.txt`. If a file already
exists, inspect it instead of recreating it. Canvas MCP checks
the token's Windows access list: access may be granted only to
your account, SYSTEM, and Administrators. If `icacls` fails,
resolve the permissions before saving the token.

In **`env`**, begin with:

```dotenv
CANVAS_API_URL=https://your-institution.instructure.com/api/v1
CANVAS_ROLE=student
TIMEZONE=UTC
ALLOWED_WRITE_TOOLS=none
```

Replace the URL and role. Set `TIMEZONE` to your IANA timezone,
such as `America/Los_Angeles`, to display local times. The last
line disables tools that change Canvas or make other side
effects while you check the connection. You can enable selected
actions later.

Process environment variables override settings in `env`, but
the native `token` file overrides `CANVAS_API_TOKEN`. Legacy
`~/.canvas-mcp` and project `.env` files are migration fallbacks;
new installations should use the directory above. On Linux,
use the same `XDG_CONFIG_HOME` when launching the AI client.

## Test the connection

Run these from your terminal, using the absolute executable
path if needed:

```sh
canvas-mcp-server --config
canvas-mcp-server --test
```

`--config` displays effective settings without printing the
token. `--test` makes live Canvas reads and reports whether the
connection succeeded; it does not change Canvas. Normal server
startup also checks your token against Canvas.

If this fails, use [troubleshooting](../examples/common_issues.md)
before setting up the client. A successful test checks Canvas
access; it does not register the server with an AI application.

## Connect your assistant

Choose **one** client recipe. The server name `canvas` below is
just a label. If it already exists in your client, inspect that
entry before adding another one. Client configuration contains
the executable path; the Canvas token stays in its private file.

### Codex

With the Codex CLI installed, replace the example executable
path and run:

```sh
codex mcp add canvas -- /absolute/path/to/canvas-mcp-server
codex mcp list
```

Alternatively, add this entry to `~/.codex/config.toml`, merging
it with existing settings:

```toml
[mcp_servers.canvas]
command = "/absolute/path/to/canvas-mcp-server"
```

For Windows TOML, a single-quoted path avoids backslash escapes:

```toml
[mcp_servers.canvas]
command = 'C:\Users\Alex\.local\bin\canvas-mcp-server.exe'
```

Restart or reconnect Codex after adding the entry. In the CLI,
`/mcp` shows active servers. See the
[official Codex MCP documentation][codex-mcp] for your surface.

### Claude Code

Replace the executable path and run:

```sh
claude mcp add --transport stdio --scope user canvas \
  -- /absolute/path/to/canvas-mcp-server
claude mcp list
```

Open a new Claude Code session and inspect `/mcp`. The user
scope makes the server available across projects. See the
[official Claude Code MCP documentation][claude-mcp].

### Claude Desktop

Use the local MCP configuration file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add a server entry, preserving any existing entries:

```json
{
  "mcpServers": {
    "canvas": {
      "command": "/absolute/path/to/canvas-mcp-server"
    }
  }
}
```

For a Windows JSON path, escape backslashes, for example:
`"C:\\Users\\Alex\\.local\\bin\\canvas-mcp-server.exe"`.
Use valid JSON, with no comments or trailing commas. Fully quit
and reopen Claude Desktop, then check its connected tools.
See the [Claude Desktop local-server guide][desktop-mcp].

### Other clients

Find your application's instructions for a **local stdio MCP
server**. Supply the absolute executable path as its command;
no server URL or port is needed. Configuration schemas differ,
so use your client's format rather than copying another
client's JSON. If it asks for a URL only, that setup expects a
remote server rather than this local process.

Do not run a separate `canvas-mcp-server` in a terminal and
expect the client to attach to it. Each stdio connection launches
its own process. A manually launched server may simply wait
for protocol input; use Ctrl+C to stop that manual process.

## Make your first request

In your AI assistant, ask:

> Use the Canvas tools to list my courses and their IDs.
> Do not change anything. If Canvas is unavailable, tell me
> instead of guessing.

Confirm that your client shows a Canvas tool call and that the
returned courses match your Canvas account. Then select a
course by its ID or exact course code and ask:

> Show the modules and pages in this course. Do not edit them.

Students can also ask “What assignments are due this week?”
Educators using the `educator` profile can ask for submission
status for a specific assignment. The
[student](../examples/student_quickstart.md) and
[educator](../examples/educator_quickstart.md) workflows give
more examples. You usually do not need to name individual tools.

## Enable changes when ready

The example configuration intentionally starts with
`ALLOWED_WRITE_TOOLS=none`. To enable a particular action,
replace that value with tool names from the
[tool reference](../tools/README.md), for example:

```dotenv
ALLOWED_WRITE_TOOLS=edit_page_content,create_page
```

Restart or reconnect your client after changing settings.
Only listed side-effect tools are enabled, and your profile and
Canvas permissions still apply. Removing the setting entirely
restores the local stdio default, which permits ordinary write
tools. It does not turn on separately gated student writes or
TypeScript execution.

Some actions save immediately. Deletions and messages commonly
return a preview and a single-use confirmation token first.
Have your assistant show that preview and wait for your answer.
For an initial write, choose a small edit in a test course and
inspect the result in Canvas. If a save is **unconfirmed**, check
Canvas before trying again; it may already have succeeded.

For grading, consider `ENABLE_DATA_ANONYMIZATION=true` in `env`
and review your institution's rules for handling student data.
Anonymization reduces supported identity fields in tool results;
it does not make every piece of course content anonymous.

Keep `execute_typescript` disabled unless you need custom code
execution. Enabling it bypasses the usual confirmation and
untrusted-content fencing guarantees.

## Upgrade or return to an earlier version

For the copy installation above, changing source files does
not update the installed server. From your clean checkout:

```sh
git status --short
git pull --ff-only
git rev-parse HEAD
uv tool install --force --python 3.12 .
```

Review local changes before pulling. If you installed a detached
revision, explicitly choose the next reviewed revision instead
of pulling. To return to a recorded commit, use
`git checkout --detach <saved-commit-id>` and reinstall with the
same uv command. Do not type the angle-bracket placeholders.

Restart or reconnect the client after reinstalling so it loads
the current code and tool definitions. Repeat the connection
test and first read-only request. Avoid `uv tool upgrade
canvas-mcp` for this checkout installation: use the selected
fork source again. Changing software versions does not undo
Canvas changes or restore a course backup.

## Optional workflow skills

Once the server works, you can add recipes from this checkout's
[skills directory](../skills/). A skill is guidance for the
assistant, not another server. It still needs the connected
tools and your permission for actions.

For example, on Linux/macOS, from this checkout, copy the backup
skill to the directory used by your client. For Codex:

```sh
mkdir -p "$HOME/.agents/skills/canvas-course-backup"
cp -n skills/canvas-course-backup/SKILL.md \
  "$HOME/.agents/skills/canvas-course-backup/SKILL.md"
```

For Claude Code, use `~/.claude/skills/canvas-course-backup`
instead. These commands preserve an existing skill file; review
it before choosing to replace it. Restart the client if needed,
then ask it to use the `canvas-course-backup` skill with a course
and local backup directory. See the client's own skill
installation instructions on Windows.

If setup fails, continue with
[Common issues and solutions](../examples/common_issues.md).

[git-install]: https://git-scm.com/downloads
[uv-install]: https://docs.astral.sh/uv/getting-started/installation/
[codex-mcp]: https://developers.openai.com/codex/mcp
[claude-mcp]: https://code.claude.com/docs/en/mcp
[desktop-mcp]: https://modelcontextprotocol.io/docs/develop/connect-local-servers
