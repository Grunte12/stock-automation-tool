# Stock Automation Tool

Stock Automation Tool is a compact, local-only Python 3.11+ pipeline that
stops at an upload-ready Adobe-style package:

`prompt plan → stub generation → technical QC → Pillow upscale → metadata → package`

It does not upload, submit, browse, authenticate, or call an image provider.
ChatGPT Web is intentionally **not included in v0.1**; an optional future
provider adapter can be configured independently without changing this local
pipeline.

## Install

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Prompt plan

Create a JSON plan such as `plan.json`:

```json
{
  "version": 1,
  "prompts": [
    {
      "id": "P-001",
      "title": "Alpine Morning",
      "prompt": "A geometric alpine sunrise",
      "keywords": ["alpine", "morning", "sunrise"],
      "category": 8
    }
  ]
}
```

Filename matching uses a safe mapping documented in the package: normalize
Unicode to ASCII, lowercase, replace each run of non-alphanumeric characters
with one hyphen, and trim hyphens. Generated files use the prompt ID stem
(`P-001` becomes `p-001.png`). Metadata also accepts a unique title stem.
Ambiguous mappings and duplicate prompt matches are rejected.

## JSON CLI

Every command prints one JSON object. Paths must resolve inside the current
working directory. Commands that create or modify files require `--confirm`.

```bash
python -m stock_automation_tool plan validate --file plan.json
python -m stock_automation_tool generate stub --plan plan.json --output-dir generated --confirm
python -m stock_automation_tool qc technical --folder generated
python -m stock_automation_tool upscale pillow --input generated/p-001.png --output-dir upscaled --scale 2 --confirm
python -m stock_automation_tool metadata build --folder upscaled --plan plan.json --csv metadata.csv --confirm
python -m stock_automation_tool package build --folder upscaled --csv metadata.csv --output-dir package --confirm
```

The generation command explicitly reports `"provider": "stub"` and creates
deterministic synthetic Pillow images locally. It is a test fixture, not a
provider call. Technical QC reports decode status, dimensions, RGB/RGBA mode,
alpha-channel presence, and megapixels. The package contains the images, CSV,
and a manifest with SHA-256 hashes and CSV filename-join validation.

Coding agents can invoke the same ordinary subprocess interface without any
agent-specific integration:

```python
import json
import subprocess

completed = subprocess.run(
    ["python", "-m", "stock_automation_tool", "qc", "technical", "--folder", "upscaled"],
    check=True,
    capture_output=True,
    text=True,
)
result = json.loads(completed.stdout)
```

## Development

```bash
pytest -q
```

All test fixtures are created in temporary directories. There are no bundled
images, credentials, provider adapters, browser sessions, or portal actions.
