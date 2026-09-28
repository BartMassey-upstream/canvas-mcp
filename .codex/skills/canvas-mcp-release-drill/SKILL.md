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

When the user requests the full local drill:

1. Run checks proportionate to the change before committing. At minimum run
   `git diff --check` and the focused tests; use the full suite for a completed
   feature or after integration when practical.
2. Commit on the feature branch only when explicitly requested. Follow the
   repository's current commit-message and attribution rules.
3. Merge the feature branch into local `main` only when requested. Preserve a
   reviewable feature-branch commit and use an explicit merge commit unless the
   user asks for a different history. Resolve conflicts by retaining both the
   feature and newer integration-branch behavior.
4. Verify the integrated worktree, graph, and branch tracking. Every local PR
   branch whose same-named `origin/<branch>` ref exists should track that ref;
   repair missing associations with `git branch --set-upstream-to`. If the
   remote ref does not exist, do not push merely to create it—report the
   missing upstream instead. Do not push unless the user gives explicit push
   permission; saying they will push is not permission for Codex to do it.
5. When installation is requested, refresh the user-level editable tool from
   the integrated checkout with `uv tool install --force --editable .`. Locate
   `uv` on `PATH`, falling back to `$HOME/.local/bin/uv`; do not install a
   system-level package. Confirm the launcher imports `canvas_mcp` from this
   checkout and perform a local registry or help smoke test that does not call
   the live Canvas API.

Report the feature and merge commit IDs, branch/worktree state, checks run,
installation result, and whether anything was pushed. If a requested action
was not authorized, leave it undone and say so plainly.
