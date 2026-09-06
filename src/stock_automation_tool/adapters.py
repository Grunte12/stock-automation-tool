"""Safe, provider-neutral command adapters.

The adapter boundary is deliberately small: a configured executable receives
contained paths, writes an image or returns a compact visual-QC object, and
never gets browser/session handling from this package.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from .core import (
    ToolError,
    _commit_new_file,
    _temporary_path,
    _write_json_new,
    contained_nonsymlink_path,
    display_path,
    sha256_file,
    validate_visual_evidence,
    workspace_root,
)


PLACEHOLDERS = ("{prompt_file}", "{output_path}", "{input_path}")
MAX_ADAPTER_TIMEOUT = 300
MAX_PROVIDER_JSON = 1_000_000
ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class AdapterError(ToolError):
    """A sanitized adapter failure with an explicit retry classification."""

    def __init__(self, message: str, *, status: str = "terminal", code: str = "adapter_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class CommandAdapter:
    alias: str
    command: tuple[str, ...]
    timeout_seconds: float
    kind: str


def _section(raw: dict[str, Any], kind: str) -> dict[str, Any] | None:
    providers = raw.get("providers")
    if isinstance(providers, dict):
        provider_names = {
            "generate": ("generate", "generation"),
            "visual_qc": ("visual_qc", "qc_visual", "visual"),
        }
        for name in provider_names[kind]:
            candidate = providers.get(name)
            if isinstance(candidate, dict):
                return candidate
    aliases = {
        "generate": ("generate", "generation"),
        "visual_qc": ("visual_qc", "qc_visual", "visual"),
    }
    for name in aliases[kind]:
        candidate = raw.get(name)
        if isinstance(candidate, dict):
            return candidate
    return None


def load_command_adapter(config_value: str | Path, kind: str) -> CommandAdapter:
    if kind not in {"generate", "visual_qc"}:
        raise AdapterError("unsupported adapter kind", code="config_invalid")
    config_path = contained_nonsymlink_path(config_value, "adapter config", must_exist=True)
    if not config_path.is_file():
        raise AdapterError("adapter config is not a file", code="config_invalid")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("adapter config is not valid JSON", code="config_invalid") from exc
    if not isinstance(raw, dict):
        raise AdapterError("adapter config must be a JSON object", code="config_invalid")
    section = _section(raw, kind)
    if section is None:
        raise AdapterError("no command adapter is configured", code="config_missing")
    alias = section.get("alias")
    if not isinstance(alias, str) or not ALIAS_RE.fullmatch(alias):
        raise AdapterError("adapter alias is invalid", code="config_invalid")
    command = section.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
        raise AdapterError("adapter command must be a non-empty JSON string list", code="config_invalid")
    if any("\x00" in item or len(item) > 4096 for item in command):
        raise AdapterError("adapter command contains an invalid argument", code="config_invalid")
    if "{input_path}" not in command and not any("{input_path}" in item for item in command):
        raise AdapterError("adapter command must include {input_path}", code="config_invalid")
    if kind == "generate" and any(
        placeholder not in command and not any(placeholder in item for item in command)
        for placeholder in PLACEHOLDERS
    ):
        raise AdapterError(
            "generation command must include {prompt_file}, {output_path}, and {input_path}",
            code="config_invalid",
        )
    timeout = section.get("timeout_seconds", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise AdapterError("adapter timeout is invalid", code="config_invalid")
    if not 1 <= timeout <= MAX_ADAPTER_TIMEOUT:
        raise AdapterError("adapter timeout is outside the supported bound", code="config_invalid")
    return CommandAdapter(alias, tuple(command), float(timeout), kind)


def _preview(adapter: CommandAdapter, operation: str) -> dict[str, Any]:
    return {
        "ok": True,
        "command": operation,
        "status": "preview",
        "provider": "command",
        "adapter_alias": adapter.alias,
        "timeout_seconds": adapter.timeout_seconds,
        "placeholders": [item[1:-1] for item in PLACEHOLDERS if any(item in arg for arg in adapter.command)],
        "executed": False,
    }


def _render(adapter: CommandAdapter, prompt_path: Path, output_path: Path, input_path: Path) -> list[str]:
    values = {
        "{prompt_file}": os.fspath(prompt_path),
        "{output_path}": os.fspath(output_path),
        "{input_path}": os.fspath(input_path),
    }
    rendered = [
        argument.replace("{prompt_file}", values["{prompt_file}"])
        .replace("{output_path}", values["{output_path}"])
        .replace("{input_path}", values["{input_path}"])
        for argument in adapter.command
    ]
    if any("{" in argument or "}" in argument for argument in rendered):
        raise AdapterError("adapter command contains an unsupported placeholder", code="config_invalid")
    return rendered


def _run(adapter: CommandAdapter, argv: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            cwd=workspace_root(),
            capture_output=True,
            text=True,
            timeout=adapter.timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError("provider command timed out", status="retryable", code="timeout") from exc
    except OSError as exc:
        raise AdapterError("provider command could not be started", code="launch_failed") from exc
    if len(completed.stdout.encode("utf-8", "replace")) > MAX_PROVIDER_JSON:
        raise AdapterError("provider response exceeded the size limit", code="response_too_large")
    if completed.returncode != 0:
        raise AdapterError("provider command returned a failure status", status="retryable", code="provider_failed")
    if not completed.stdout.strip():
        return {}
    try:
        result = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AdapterError("provider response was not valid JSON", code="invalid_provider_result") from exc
    if not isinstance(result, dict):
        raise AdapterError("provider response must be a JSON object", code="invalid_provider_result")
    if result.get("ok") is False or result.get("status") in {"failed", "error"}:
        raise AdapterError("provider reported a failure", status="retryable", code="provider_reported_failure")
    return result


def _image_receipt(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
    except Exception as exc:  # Pillow exposes several decoder-specific errors.
        raise AdapterError("provider output is not a valid image", code="invalid_output") from exc
    return {
        "path": display_path(path),
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "dimensions": {"width": width, "height": height},
    }


def generate_command(
    config_value: str | Path,
    prompt_value: str | Path,
    output_value: str | Path,
    input_value: str | Path,
    confirm: bool,
) -> dict[str, Any]:
    adapter = load_command_adapter(config_value, "generate")
    prompt = contained_nonsymlink_path(prompt_value, "prompt file", must_exist=True)
    input_path = contained_nonsymlink_path(input_value, "input path", must_exist=True)
    output = contained_nonsymlink_path(output_value, "output path")
    if not prompt.is_file() or not input_path.is_file():
        raise AdapterError("prompt and input paths must be files", code="path_invalid")
    if output.exists():
        raise AdapterError("output path already exists", code="collision")
    if not confirm:
        return _preview(adapter, "generate command")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(output.parent, output.suffix or ".png")
    try:
        _run(adapter, _render(adapter, prompt, temporary, input_path))
        if not temporary.is_file():
            raise AdapterError("provider did not create an output image", status="retryable", code="output_missing")
        receipt = _image_receipt(temporary)
        _commit_new_file(temporary, output)
        receipt["path"] = display_path(output)
        return {
            "ok": True,
            "command": "generate command",
            "status": "succeeded",
            "provider": "command",
            "adapter_alias": adapter.alias,
            "output": receipt,
        }
    except AdapterError:
        raise
    except (OSError, ValueError) as exc:
        raise AdapterError("provider output could not be committed", code="commit_failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _visual_result(raw: dict[str, Any]) -> dict[str, Any]:
    score = raw.get("score")
    confidence = raw.get("confidence")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        raise AdapterError("visual-QC score is invalid", code="invalid_qc_result")
    if not 0 <= score <= 100:
        raise AdapterError("visual-QC score is out of range", code="invalid_qc_result")
    if raw.get("verdict") not in {"pass", "fail"}:
        raise AdapterError("visual-QC verdict is invalid", code="invalid_qc_result")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
        raise AdapterError("visual-QC confidence is invalid", code="invalid_qc_result")
    if not 0 <= confidence <= 1:
        raise AdapterError("visual-QC confidence is out of range", code="invalid_qc_result")
    defects = raw.get("defects")
    if not isinstance(defects, list) or any(not isinstance(item, str) for item in defects):
        raise AdapterError("visual-QC defects are invalid", code="invalid_qc_result")
    if len(defects) > 100 or any(len(item) > 500 for item in defects):
        raise AdapterError("visual-QC defects are too large", code="invalid_qc_result")
    return {
        "score": score,
        "verdict": raw["verdict"],
        "defects": [item.replace("\n", " ").replace("\r", " ") for item in defects],
        "confidence": confidence,
    }


def visual_command(
    config_value: str | Path,
    input_value: str | Path,
    stage: str,
    evidence_value: str | Path,
    confirm: bool,
    prompt_value: str | Path | None = None,
    output_value: str | Path | None = None,
) -> dict[str, Any]:
    if stage not in {"pre_upscale", "post_upscale"}:
        raise AdapterError("visual-QC stage is invalid", code="invalid_stage")
    adapter = load_command_adapter(config_value, "visual_qc")
    input_path = contained_nonsymlink_path(input_value, "visual-QC input", must_exist=True)
    evidence_path = contained_nonsymlink_path(evidence_value, "visual evidence")
    if not input_path.is_file():
        raise AdapterError("visual-QC input is not a file", code="path_invalid")
    try:
        with Image.open(input_path) as image:
            image.load()
    except Exception as exc:
        raise AdapterError("visual-QC input is not a valid image", code="invalid_input") from exc
    input_hash = sha256_file(input_path)
    prompt_path = (
        contained_nonsymlink_path(prompt_value, "visual-QC prompt file", must_exist=True)
        if prompt_value is not None
        else input_path
    )
    if evidence_path.exists():
        raise AdapterError("visual evidence path already exists", code="collision")
    if output_value is not None:
        configured_output = contained_nonsymlink_path(output_value, "visual-QC output path")
        if configured_output.exists():
            raise AdapterError("visual-QC output path already exists", code="collision")
    if not confirm:
        return _preview(adapter, "qc visual command")

    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(evidence_path.parent, ".json")
    try:
        raw = _run(adapter, _render(adapter, prompt_path, temporary, input_path))
        if not raw and temporary.is_file() and temporary.stat().st_size:
            raw = json.loads(temporary.read_text(encoding="utf-8"))
        result = _visual_result(raw)
        evidence = {
            "version": 1,
            "kind": "visual_qc",
            "input": display_path(input_path),
            "input_sha256": input_hash,
            "stage": stage,
            "adapter_alias": adapter.alias,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **result,
        }
        _write_json_new(evidence_path, evidence)
        validate_visual_evidence(evidence_path, input_path)
        return {
            "ok": True,
            "command": "qc visual command",
            "status": "succeeded",
            "provider": "command",
            "adapter_alias": adapter.alias,
            "evidence": display_path(evidence_path),
            "input_sha256": input_hash,
            **result,
        }
    except json.JSONDecodeError as exc:
        raise AdapterError("visual-QC output was not valid JSON", code="invalid_qc_result") from exc
    except AdapterError:
        raise
    except (OSError, ValueError) as exc:
        raise AdapterError("visual evidence could not be written", code="evidence_write_failed") from exc
    finally:
        temporary.unlink(missing_ok=True)
