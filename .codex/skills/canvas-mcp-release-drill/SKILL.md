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

When the user requests the full local drill:

1. Before committing, run `git diff --check`, the focused tests, and the same
   Python quality gates as CI:

   ```bash
   uv run --frozen ruff check src/ tests/
   uv run --frozen mypy src/
   ```

   Substitute the resolved `uv` executable when the fallback is needed. Use
   the full test suite for a completed feature or after integration when
   practical. Rerun Ruff and mypy after integration when conflict resolution
   or merging changed Python files.
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

   Keep repository-local `push.followTags=false`. A branch-only push must use
   `git push --no-follow-tags --all origin`; push the one intended tag by its
   exact ref in a separate command. Each push still requires explicit user
   authorization. Never rely on `push.followTags`: it can publish unrelated
   upstream tags reachable from the same history.
6. When installation is requested, refresh the user-level editable tool from
   the integrated checkout with `uv tool install --force --editable .`. Do not
   install a system-level package. Confirm the launcher imports `canvas_mcp`
   from this checkout and perform a local registry or help smoke test that
   does not call the live Canvas API.

Report the feature and merge commit IDs, branch/worktree state, checks run,
installation result, and whether anything was pushed. If a requested action
was not authorized, leave it undone and say so plainly.
