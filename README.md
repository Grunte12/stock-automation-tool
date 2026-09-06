# Stock Automation Tool

Provider-neutral, local Python pipeline for preparing image batches through an
upload-ready package:

`plan → generation → technical QC → visual QC → upscale → post-upscale QC → metadata → CSV → package`

It stops before Adobe portal upload or submission. A passing package proves
local file integrity only; it does not predict Adobe acceptance, quality, or
sales.

## Safety boundaries

- Every command writes JSON to stdout. All mutations and provider calls require
  `--confirm`; without it, provider adapters return a non-mutating preview.
- Paths must remain under the directory where the CLI is run. Symlinks and
  existing output destinations are rejected.
- The tool does not ship, read, or automate credentials, browser profiles,
  cookies, HAR files, sessions, portals, uploads, or submissions.
- Command adapters use bounded, argument-list subprocesses (`shell=False`).
  Receipts redact prompts, command arguments, environment values, provider
  output, and session identifiers.

## Install

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Plan format

Create `plan.json`:

```json
{
  "version": 1,
  "prompts": [{
    "id": "P-001",
    "title": "Alpine Morning",
    "prompt": "A geometric alpine sunrise",
    "keywords": ["alpine", "morning", "sunrise"],
    "category": 8
  }]
}
```

Filename matching normalizes Unicode to ASCII, lowercases it, replaces runs of
non-alphanumerics with a hyphen, then trims hyphens. Generated files use the
prompt ID stem (`P-001` becomes `p-001.png`). Ambiguous matches are rejected.

## Full local stub workflow

The stub generator creates deterministic Pillow test images; it is not an
image provider. It is useful for testing all file, evidence, and package gates.

```bash
python -m stock_automation_tool plan validate --file plan.json
python -m stock_automation_tool generate stub --plan plan.json --output-dir generated --confirm
python -m stock_automation_tool qc technical --folder generated --output technical.json --confirm
python -m stock_automation_tool upscale pillow --input generated/p-001.png --output-dir upscaled --scale 2 --confirm
python -m stock_automation_tool qc technical --folder upscaled --output post-upscale-technical.json --confirm
python -m stock_automation_tool metadata build --folder upscaled --plan plan.json --csv metadata.csv --confirm
python -m stock_automation_tool package build --folder upscaled --csv metadata.csv --technical-report post-upscale-technical.json --output-dir package --confirm
```

Pillow upscaling is interpolation, not a claim that an image has gained detail.
Its lineage records source and output hashes, requested scale, dimensions, and
technical-gate status. Run post-upscale technical QC before packaging.

## Optional command adapters

Bring your own image-generation or visual-review bridge. This package only
calls an explicitly configured executable; it contains no provider-specific
authenticator. A user who already operates a ChatGPT-Web bridge may connect it
through this boundary, but bridge code, browser authentication, and sessions
are not part of this repository.

Create a local, ignored `adapters.json` such as:

```json
{
  "providers": {
    "generate": {
      "alias": "my-image-bridge",
      "command": ["/absolute/or/workspace/path/to/bridge", "generate", "--prompt", "{prompt_file}", "--input", "{input_path}", "--output", "{output_path}"],
      "timeout_seconds": 120
    },
    "visual_qc": {
      "alias": "my-visual-reviewer",
      "command": ["/absolute/or/workspace/path/to/reviewer", "--image", "{input_path}", "--result", "{output_path}"],
      "timeout_seconds": 60
    }
  }
}
```

`command` is a JSON string list, not shell text. Generation must include all
three placeholders: `{prompt_file}`, `{input_path}`, and `{output_path}`.
Visual QC must include `{input_path}`; it may write its result to
`{output_path}` or print one JSON object to stdout:

```json
{"score": 96, "verdict": "pass", "defects": [], "confidence": 0.91}
```

The visual adapter validates score (0–100), verdict (`pass` or `fail`), a list
of bounded defect strings, and confidence (0–1), then binds the evidence to the
input SHA-256 and review stage. Example calls:

```bash
# Preview only: no provider process and no output write.
python -m stock_automation_tool generate command --config adapters.json --prompt-file prompt.txt --input-path reference.png --output-path generated/p-001.png

# Live generation: requires explicit confirmation.
python -m stock_automation_tool generate command --config adapters.json --prompt-file prompt.txt --input-path reference.png --output-path generated/p-001.png --confirm

python -m stock_automation_tool qc visual command --config adapters.json --input generated/p-001.png --stage pre_upscale --evidence evidence/p-001-pre.json --confirm
python -m stock_automation_tool qc visual command --config adapters.json --input upscaled/p-001.png --stage post_upscale --evidence evidence/p-001-post.json --confirm
```

## Reproducible batch runs

Export jobs from a validated plan, execute with either `stub` or `command`, and
resume safely from atomic receipts. IDs derive from run ID, prompt ID, index,
variant, and plan hash. Completed valid receipts and terminal failures are not
silently retried; malformed receipts, output collisions, hash mismatches, and
containment failures block execution. Concurrency is capped at two.

```bash
python -m stock_automation_tool run export --plan plan.json --output-dir run --run-id alpine-v1 --variants 2 --confirm
python -m stock_automation_tool run execute --run-dir run --provider stub --concurrency 2 --confirm
python -m stock_automation_tool run status --run-dir run
```

For an external provider, add `--provider command --config adapters.json` to
`run execute`. Receipts retain stable job/run identifiers, output hashes and
sizes, safe statuses, and retry classification—but never raw prompts or
provider output. Retryable classes include network, timeout, rate-limit, and
provider failures. Quota, model-limit, authentication, and invalid-output
failures are terminal.

## Packages

`package build` requires an exact image/CSV filename join, a generated manifest,
and matching SHA-256 values. Pass `--technical-report` to require technical
evidence and `--visual-evidence` to bind a validated visual-QC result. The
manifest records a canonical sorted package digest for local integrity checks.

## Generic coding-agent use

Any agent that can run an ordinary subprocess can call the JSON CLI; no agent
integration is required:

```python
import json
import subprocess

completed = subprocess.run(
    ["python", "-m", "stock_automation_tool", "run", "status", "--run-dir", "run"],
    check=True, capture_output=True, text=True,
)
result = json.loads(completed.stdout)
```

## Development

```bash
pytest -q
```

Tests create all fixtures in temporary directories. The repository contains no
bundled images, generated outputs, credentials, provider adapters, browser
sessions, or portal actions.
