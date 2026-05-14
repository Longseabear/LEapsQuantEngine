---
name: leaps-github-issues
description: Use when Codex needs to inspect, triage, implement, update, comment on, or close GitHub issues for LEapsQuantEngine, especially when the user says "issue #N", "GitHub issue", "check issue", "address issue", "close issue", or asks to create a new issue.
---

# LEaps GitHub Issues

## Core Rule

Use GitHub issues as an operator-facing work ledger. Do not treat an issue as a
blind instruction. Read the issue, compare it with the current engine design,
iterate on the implementation, test, then update the issue with what changed.

If the target issue is unclear, ask for the issue number, URL, or exact title.
If creating a new issue and the title/name is unclear, ask the user for the
issue name before creating it.

## Inspect

Prefer `gh` from the repo root:

```powershell
gh issue view <number-or-url> --json number,title,state,body,comments --repo Longseabear/LEapsQuantEngine
```

When the user gives only a vague issue name, search first:

```powershell
gh issue list --search "<name or keywords>" --state all --repo Longseabear/LEapsQuantEngine
```

Summarize:

- problem statement
- observed behavior
- expected behavior
- acceptance criteria
- affected files or runtime surfaces
- whether the issue is a real bug, design gap, operator misunderstanding, or
  stale report

## Implement

Work from the engine architecture, not from the issue wording alone.

- Read relevant files before editing.
- Keep diffs narrow and reviewable.
- Preserve LEAN-style boundaries: universe, alpha, portfolio, risk, execution,
  order runtime, broker adapter.
- Do not put strategy decisions into core guards unless they are engine safety
  invariants.
- Add tests for the bug or contract change before calling it done.
- Do not close an issue just because a local patch exists; close only when the
  user asks or the acceptance criteria are fully met and verified.

## Update GitHub

After meaningful progress, comment on the issue:

```powershell
gh issue comment <number> --repo Longseabear/LEapsQuantEngine --body "<summary, tests, remaining risk>"
```

The comment should include:

- what was confirmed
- what changed
- tests run and result
- remaining risks or live-reload requirements

If the user asks to close the issue:

```powershell
gh issue close <number> --repo Longseabear/LEapsQuantEngine --comment "<why this is resolved>"
```

If the issue is not actually fixed, leave it open and say why.

## Create A New Issue

Ask for the issue title/name if the user did not provide one. Do not invent a
title for an operationally important issue unless the user clearly delegates
naming.

Use this shape:

```markdown
## Summary

## Observed Behavior

## Expected Behavior

## Why This Matters

## Acceptance Criteria

## Notes
```

Create with:

```powershell
gh issue create --repo Longseabear/LEapsQuantEngine --title "<issue title>" --body-file <body-file>
```

Prefer a temporary UTF-8 body file over command-line literals when non-ASCII
text is involved.

## Final Report To User

Keep the user update concise:

- issue number and title
- confirmed problem
- fix or current blocker
- tests run
- GitHub update URL when available
