# v1 Completion Roadmap

## Deliverable

Finish the agent-agnostic local workflow through an upload-ready Adobe-style package:

`plan → provider generation → technical QC → visual QC → upscale → post-upscale QC → metadata → CSV → package`

## Required additions

1. Provider adapter contract plus a separately configured ChatGPT-Web bridge adapter. It must invoke a user-installed bridge through an explicit command/configuration boundary; it must never parse browser credentials, cookies, HAR files, profiles, or sessions.
2. Visual-QC adapter contract with explicit confirmation and a disabled-by-default deterministic placeholder/failure mode when no provider is configured.
3. Batch orchestration CLI that records JSON receipts, hashes, technical results, visual-QC results, upscale lineage, and package eligibility.
4. Quality policy: non-destructive output, workspace containment, no overwrite, explicit confirm for every mutation/provider call, and post-upscale QC required before package eligibility.
5. Generic-agent documentation, example configuration, tests, and release checks.

## Non-goals

- Adobe portal/browser automation, uploads, submissions, or account state.
- Bundled image data, generated outputs, credentials, cookies, profiles, or source-workspace history.
- Agent-specific protocol or tool wrappers. The JSON CLI remains the shared interface.
