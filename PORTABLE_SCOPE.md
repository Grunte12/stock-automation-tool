# Stock Automation Tool — Portable Scope

## Goal

Provide a tool-only, coding-agent-agnostic pipeline that stops at an Adobe Stock upload-ready package:

`prompt plan → generate → technical QC → optional visual QC → upscale → post-upscale QC → metadata → Adobe CSV → upload-ready package`

## Interface

- Python CLI with stable JSON output; usable through ordinary subprocess calls by Codex, Claude Code, Cursor, or OpenCode.
- Provider-neutral generation interface; optional ChatGPT Web/native adapter is separately configured by each user.
- Every provider call and filesystem mutation requires explicit confirmation.
- Local-only output directories and workspace containment checks.

## Excluded

- Adobe portal/browser automation, upload, submission, account data, cookies, profiles, and sessions.
- Dashboard and Chrome extension.
- Images, prompts from the source workspace, generated outputs, packages, CSVs, logs, receipts, captures, thumbnails, and historical user data.
- OpenCode-only tools/skills and any agent-specific configuration.

## Acceptance

- Fresh clone has no user assets or credentials.
- README provides generic-agent, Codex, and optional-provider setup.
- Deterministic local test covers plan validation, technical QC, safe upscale stub/adapter contract, metadata validation, CSV generation, and package manifest.
- `.gitignore` blocks outputs, credentials, browser/session artifacts, and generated images.
- No command performs Adobe portal action.
