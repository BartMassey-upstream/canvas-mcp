---
name: canvas-mcp-release-drill
description: Integrate completed Canvas MCP feature-branch work into main and refresh the local uv tool installation when the user asks for the usual commit, merge, install, or release drill.
---

# Canvas MCP release drill

Use this only in the Canvas MCP repository. Treat commit, merge, install, and
push as separate actions whose authorization comes from the user's request.

Before changing git state, read the repository's active `AGENTS.md`, inspect
the current branch and worktree, and confirm that the pending diff belongs to
the requested feature. Preserve unrelated user changes.

Locate `uv` on `PATH`, falling back to `$HOME/.local/bin/uv`, and use that
resolved executable for every `uv` command below.

## Local CI validation

Before a requested commit, merge, or release, run applicable CI
checks locally whenever possible. Read the current workflows in
`.github/workflows/` for commands, dependency installation,
interpreter versions, environment flags and required jobs. Use
those definitions as the source of truth; a generic local test
run alone does not establish CI compatibility.

- Run `git diff --check` and the CI quality gates:
  `uv run --frozen ruff check src/ tests/` and
  `uv run --frozen mypy src/`. Use the resolved `uv` executable.
  Match CI's locked tools; where CI deliberately resolves fresh
  dependencies, reproduce that in an isolated environment.
- For completed feature/release work, run the full Python suite
  on locally available CI matrix versions, including the oldest
  supported version when possible. Match CI environment flags,
  including `FASTMCP_MCP_CAMELCASE_COMPAT=false`. Unit tests must
  pass without the developer's Canvas URL, token or config files;
  use explicit synthetic configuration only where the CI step
  calls for it, such as installed-wheel smoke tests.
- Run the applicable TypeScript build/tests, confirmation proofs,
  security checks, distribution audit and clean installed-wheel
  MCP workflows using the workflow's commands and pinned tools.
  Package checks must import the installed wheel, not the source
  checkout. Keep Canvas calls synthetic; CI validation does not
  authorize live writes, deployment or publication.
- Keep temporary environments, downloads and artifacts inside
  ignored project directories. If downloading test interpreters,
  set `UV_PYTHON_INSTALL_DIR` inside the project and use
  `uv python install --no-bin` to avoid user-level launchers.
- Fix reproduced failures before integration. If a check cannot
  run locally because of platform, credentials or unavailable
  tooling, record the exact check and reason as unverified.
  Do not silently skip it or report a local pass as a GitHub pass.
- Reuse passing evidence only when the tested code and relevant
  environment are unchanged. After conflict resolution or other
  integration changes, rerun affected checks. Scale validation
  for documentation-only edits to their actual CI impact.

When the user requests the full local drill:

1. Complete the applicable local CI validation above and retain
   its results before committing.
2. Commit on the feature branch only when explicitly requested. Follow the
   repository's current commit-message and attribution rules.
3. Merge the feature branch into local `main` only when requested. Preserve a
   reviewable feature-branch commit and use an explicit merge commit unless the
   user asks for a different history. Resolve conflicts by retaining both the
   feature and newer integration-branch behavior.
4. Verify the integrated worktree, graph, and branch tracking. Every local PR
   branch whose same-named `origin/<branch>` ref exists should track that ref;
   repair missing associations with `git branch --set-upstream-to`. That
   command fails when the remote-tracking ref does not exist yet. For a new PR
   branch, predeclare its future same-named upstream without pushing:

   ```bash
   git config --local branch.<branch>.remote origin
   git config --local branch.<branch>.merge refs/heads/<branch>
   ```

   Substitute the exact local branch name in both commands. Confirm the two
   values with `git config --get`; before the first push, status may describe
   `origin/<branch>` as `gone` and upstream revision lookup may fail because
   the remote-tracking ref does not exist. This is expected. Once the user
   pushes that branch, ordinary upstream tracking becomes active without a
   separate `git push -u`. Do not push merely to create the ref, and do not
   push unless the user gives explicit push permission; saying they will push
   is not permission for Codex to do it.
5. For a full drill that will publish the integrated fork state, prepare an
   immutable fork release after validation. Use
   `v<upstream-version>+bart.<serial>` and increment the serial rather than
   moving or reusing a tag. Keep `pyproject.toml`,
   `src/canvas_mcp/__init__.py`, `server.json`, and `uv.lock` equal to the tag
   without its leading `v`, commit that version change, and create an annotated
   tag on the resulting `main` commit. Do not create a release tag for a
   feature-only drill or unless the user requested the full release drill.

   Keep repository-local `push.followTags=false`. After creating the tag,
   refresh a repository-local alias pinned to that exact tag:

   ```bash
   tag="<tag>"
   git config --local alias.publish-bart \
     "push --atomic origin refs/heads/*:refs/heads/* refs/tags/$tag:refs/tags/$tag"
   git config --show-origin --get alias.publish-bart
   ```

   Replace `<tag>` with the new tag. This makes `git publish-bart` push all
   local branches and only the intended tag in one atomic SSH operation. The
   alias must be refreshed for every release. Configuring it does not
   authorize or execute a push; running it still requires explicit user
   authorization. Never rely on `push.followTags`: it can publish unrelated
   upstream tags reachable from the same history.
6. When installation is requested, refresh the user-level editable tool from
   the integrated checkout with `uv tool install --force --editable .`. Do not
   install a system-level package. Confirm the launcher imports `canvas_mcp`
   from this checkout and perform a local registry or help smoke test that
   does not call the live Canvas API.

Report the feature and merge commit IDs, branch/worktree state,
local CI results (including versions and unverified checks),
installation result, and whether anything was pushed. If a requested action
was not authorized, leave it undone and say so plainly.
