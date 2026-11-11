# Common issues and solutions

Use the [getting-started guide](../docs/getting-started.md) for
the installation and client recipes. Diagnose setup in this
order: executable, configuration, Canvas connection, then client.

## The server command is not found

Find the launcher directory:

```sh
uv tool list
uv tool dir --bin
```

Use its absolute path, ending in `canvas-mcp-server` on
Linux/macOS or `canvas-mcp-server.exe` on Windows. Check it
with `--help`. In PowerShell, use `&` before a quoted path.
If the absolute path works, this is a PATH problem rather than
a missing Python module. Run `uv tool update-shell` and open
a new terminal to make the command available by name.

If no installation is listed, return to the fork checkout and
follow [installation](../docs/getting-started.md#install-this-fork).
Do not switch to the published package when diagnosing this
fork. You do not need an editable installation for normal use.

## A token or configuration error stops startup

Check the two files in your platform's configuration directory:

- Linux: `${XDG_CONFIG_HOME:-$HOME/.config}/canvas-mcp`.
- macOS: `~/Library/Application Support/canvas-mcp`.
- Windows: `%APPDATA%\canvas-mcp`.

`env` contains `CANVAS_API_URL` and other settings. `token`
contains only the raw token, without a variable name or quotes.
An empty token file, a symlink, or unsafe token permissions
stops startup rather than falling back to another credential.

On Linux/macOS, check the file mode without printing its content:

```sh
ls -l "<config-directory>/token"
chmod 600 "<config-directory>/token"
```

Replace `<config-directory>` first. On Windows, inspect the
file's Security properties or `icacls` output. Access may be
granted only to your account, SYSTEM, and Administrators.
See the [configuration steps](../docs/getting-started.md#configure-canvas-access).

The native token file takes precedence over an environment
token, even if the environment has a newer value. Process
settings take precedence over `env`; a stale `CANVAS_API_URL`
or `CANVAS_ROLE` in the client environment can explain why
editing the file has no effect. Linux clients must use the same
`XDG_CONFIG_HOME` as your terminal.

Restart or reconnect after changing settings or the token.

## Canvas authentication or connection fails

Run the installed executable with `--config`, then `--test`.
The test makes live Canvas reads and changes nothing.

| Result | What to check |
|---|---|
| 401 | Expired, revoked, or incorrectly copied token |
| 403 | Your account's permission for the requested operation |
| 404 | Canvas hostname, course ID, or resource ID |
| Timeout or TLS error | Network, VPN, proxy, and institutional certificate setup |

Use the Canvas hostname where you sign in, with HTTPS and the
`/api/v1` suffix. Do not use a course or SSO login URL. A missing
suffix or trailing slash is normalized by the server; a wrong
hostname still needs correcting.

If the token expired, obtain a replacement and update the
private `token` file. If **New Access Token** is unavailable,
ask your Canvas administrator or IT help desk about API access.
Token lifetimes and issuance rules depend on your institution.
Do not paste the token into a chat or issue report.

## The connection test works but my assistant has no tools

The terminal test does not configure your client. Check its
[local MCP entry](../docs/getting-started.md#connect-your-assistant):

1. Use the absolute executable path, not `~` or a path to the
   repository directory. Windows launchers end in `.exe`.
2. Choose local stdio, not an HTTP server URL.
3. Use the configuration schema for your client. Codex uses
   TOML; Claude Desktop uses an `mcpServers` JSON object.
4. Restart or reconnect the client and inspect its MCP status.
   Codex and Claude Code provide `/mcp` in their terminal UIs.
5. Inspect the client's MCP logs if startup still fails. A GUI
   client may have different PATH and environment settings from
   your terminal.

A server started manually in a terminal is a separate process;
your client does not attach to it. A stdio server waiting without
a prompt is normal. Stop that manual process with Ctrl+C and
let the client launch its own server.

## Only some tools appear

Check `CANVAS_ROLE`, `ALLOWED_WRITE_TOOLS`, and any `--role`
argument in the client's server entry. The CLI role overrides
the configured role. `creator` deliberately excludes rosters,
submissions, grading, and other student-record operations.

The getting-started configuration uses
`ALLOWED_WRITE_TOOLS=none`; write tools are absent until you
[enable selected actions](../docs/getting-started.md#enable-changes-when-ready).
Student writes and TypeScript execution have separate opt-ins.
A client or hosted operator can also restrict tools. A tool's
absence is not a reason to bypass those restrictions.

The built-in accessibility scanner is available in `creator`,
`educator`, and `all`, including when
`ACCESSIBILITY_CHECKERS=none`. That setting removes the three
UDOIT/UFIXIT tools, which require the institutional add-on.

## An edit is unconfirmed or a confirmation token fails

An **unconfirmed** result means success could not be verified;
the save may already have happened. Read the item in Canvas
before trying again. Canvas can sanitize or rewrite HTML,
causing exact readback verification to differ from your request.

A stale timestamp or body-hash guard means the content differs
from the earlier snapshot. Read it again, reconcile the edits,
and submit a new request. These checks are not atomic locks.

Confirmation tokens are single-use, expire, and bind the
previewed arguments and target. If a token no longer matches,
request a new preview and review it before confirming.
See [safer content edits](../tools/README.md#safer-content-edits).

## The assistant shows the wrong or incomplete course data

Use a Canvas course ID or the exact course code from
`list_courses`. Archived courses and objects outside your
account's permissions may not be available. Narrow the request
to one course, assignment, or date range. Ask the assistant to
report pagination, sampling, or unavailable fields rather than
inferring that missing data means there is none.

Some lookups are cached. Restart the connection or allow the
cache to expire if recently changed course metadata is stale.
For large grading jobs, use the ordinary bulk grading tools
with reviewed grades. Custom TypeScript execution is an
optional privileged feature, not a setup repair.

## Student names look like Student_xxxxxxxx

`ENABLE_DATA_ANONYMIZATION=true` replaces supported identity
fields with anonymous IDs before returning them to your client.
This is expected. Review your institution's privacy requirements
before changing it; anonymization alone does not establish
compliance or remove all identifying free text.

## An upgrade seems to have no effect

Reinstall from the selected fork checkout, then restart or
reconnect the client. A running process keeps the code it loaded.
Check that the client's absolute command path points at the
installation you updated. A normal `uv tool install .` copies
the package; later source edits do not update that copy.
See [upgrade and rollback](../docs/getting-started.md#upgrade-or-return-to-an-earlier-version).

## Getting more help

Record the fork commit (`git rev-parse HEAD`), Python version,
client name, OS, command path, and exact error. Review and redact
logs before sharing them: remove tokens, signed URLs, student
records, and other private data. `--config` omits the token but
can still show your institution's URL.

For this fork, use the
[fork issue tracker](https://github.com/BartMassey-upstream/canvas-mcp/issues).
Confirm whether the problem also occurs upstream before
reporting a fork-specific behavior to the upstream project.
