"""Deterministic local batch export, execution, and status projections."""

from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .adapters import AdapterError, generate_command
from .core import (
    ToolError,
    _commit_new_file,
    _temporary_path,
    _write_json_new,
    contained_nonsymlink_path,
    display_path,
    load_plan,
    require_confirm,
    safe_stem,
    sha256_file,
    workspace_root,
)


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_VARIANTS = 100


def _canonical_plan_hash(plan_path: Path) -> str:
    try:
        raw = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("could not read plan for run export") from exc
    return hashlib.sha256(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _run_id(value: str | None, plan_hash: str) -> str:
    selected = value or f"run-{plan_hash[:12]}"
    if not RUN_ID_RE.fullmatch(selected):
        raise ToolError("run ID must use letters, numbers, dot, underscore, or hyphen")
    return selected


def _job_id(run_id: str, plan_hash: str, prompt_id: str, index: int, variant: int) -> str:
    material = f"{run_id}\n{plan_hash}\n{prompt_id}\n{index}\n{variant}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:24]


def run_export(
    plan_value: str | Path,
    output_value: str | Path,
    run_value: str | None,
    variants: int,
    confirm: bool,
) -> dict[str, Any]:
    if isinstance(variants, bool) or not isinstance(variants, int) or not 1 <= variants <= MAX_VARIANTS:
        raise ToolError(f"variants must be between 1 and {MAX_VARIANTS}")
    plan_path, prompts, _ = load_plan(plan_value)
    plan_hash = _canonical_plan_hash(plan_path)
    selected_run = _run_id(run_value, plan_hash)
    run_dir = contained_nonsymlink_path(output_value, "run output directory")
    if run_dir.exists():
        raise ToolError(f"refusing to overwrite existing run directory: {display_path(run_dir)}")
    jobs: list[dict[str, Any]] = []
    for index, prompt in enumerate(prompts, start=1):
        for variant in range(1, variants + 1):
            job_id = _job_id(selected_run, plan_hash, prompt.prompt_id, index, variant)
            jobs.append(
                {
                    "job_id": job_id,
                    "prompt_id": prompt.prompt_id,
                    "index": index,
                    "variant": variant,
                    "prompt_file": f"jobs/{job_id}/prompt.json",
                    "output": (
                        f"outputs/{safe_stem(prompt.prompt_id)}.png"
                        if variants == 1
                        else f"outputs/{safe_stem(prompt.prompt_id)}-v{variant}.png"
                    ),
                    "receipt": f"receipts/{job_id}.json",
                    "status": "pending",
                }
            )
    preview = {
        "ok": True,
        "command": "run export",
        "status": "preview",
        "executed": False,
        "run_id": selected_run,
        "plan_sha256": plan_hash,
        "job_count": len(jobs),
        "variants": variants,
        "output_dir": display_path(run_dir),
    }
    if not confirm:
        return preview

    run_dir.parent.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=False, exist_ok=False)
    (run_dir / "jobs").mkdir()
    (run_dir / "outputs").mkdir()
    (run_dir / "receipts").mkdir()
    for job, prompt in zip(jobs, [item for item in prompts for _ in range(variants)]):
        prompt_path = run_dir / job["prompt_file"]
        prompt_path.parent.mkdir(parents=True, exist_ok=False)
        _write_json_new(
            prompt_path,
            {
                "version": 1,
                "id": prompt.prompt_id,
                "title": prompt.title,
                "prompt": prompt.prompt,
                "keywords": list(prompt.keywords),
                "category": prompt.category,
                "index": job["index"],
                "variant": job["variant"],
            },
        )
    _write_json_new(
        run_dir / "run.json",
        {
            "version": 1,
            "run_id": selected_run,
            "plan_sha256": plan_hash,
            "job_count": len(jobs),
            "variants": variants,
            "status": "exported",
        },
    )
    _write_json_new(run_dir / "jobs.json", {"version": 1, "jobs": jobs})
    return {**preview, "status": "exported", "executed": True}


def _load_run(run_value: str | Path) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    run_dir = contained_nonsymlink_path(run_value, "run directory", must_exist=True)
    if not run_dir.is_dir():
        raise ToolError("run directory is not a directory")
    run_meta_path = contained_nonsymlink_path(run_dir / "run.json", "run metadata", must_exist=True)
    jobs_meta_path = contained_nonsymlink_path(run_dir / "jobs.json", "run jobs metadata", must_exist=True)
    try:
        run_meta = json.loads(run_meta_path.read_text(encoding="utf-8"))
        jobs_meta = json.loads(jobs_meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("run metadata is invalid") from exc
    jobs = jobs_meta.get("jobs") if isinstance(jobs_meta, dict) else None
    if not isinstance(run_meta, dict) or not isinstance(jobs, list) or not all(isinstance(item, dict) for item in jobs):
        raise ToolError("run metadata is invalid")
    return run_dir, run_meta, jobs


def _job_paths(run_dir: Path, job: dict[str, Any]) -> tuple[Path, Path, Path]:
    try:
        values = {
            "job output": job["output"],
            "job prompt file": job["prompt_file"],
            "job receipt": job["receipt"],
        }
        resolved: dict[str, Path] = {}
        for label, value in values.items():
            path = contained_nonsymlink_path(run_dir / str(value), label, must_exist=label == "job prompt file")
            try:
                path.relative_to(run_dir)
            except ValueError as exc:
                raise ToolError(f"{label} must stay inside the run directory") from exc
            resolved[label] = path
        output = resolved["job output"]
        prompt = resolved["job prompt file"]
        receipt = resolved["job receipt"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError("run job metadata is invalid") from exc
    if not prompt.is_file():
        raise ToolError("run job prompt file is not a file")
    return prompt, output, receipt


def _image_receipt(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
    except Exception as exc:
        raise ToolError("run output is not a valid image") from exc
    return {
        "path": display_path(path),
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "dimensions": {"width": width, "height": height},
    }


def _read_receipt(path: Path, job: dict[str, Any], output: Path) -> tuple[str, dict[str, Any] | None]:
    if not path.exists():
        if output.exists():
            return "blocked", None
        return "pending", None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "blocked", None
    if not isinstance(raw, dict) or raw.get("job_id") != job.get("job_id"):
        return "blocked", None
    status = raw.get("status")
    if status not in {"succeeded", "retryable", "terminal"}:
        return "blocked", None
    if status == "succeeded":
        output_data = raw.get("output")
        if (
            not isinstance(output_data, dict)
            or not output.exists()
            or output_data.get("sha256") != sha256_file(output)
            or output_data.get("size") != output.stat().st_size
        ):
            return "blocked", None
        try:
            _image_receipt(output)
        except ToolError:
            return "blocked", None
    elif output.exists():
        return "blocked", None
    return status, raw


def _stub_output(prompt_path: Path, output: Path) -> dict[str, Any]:
    try:
        prompt = json.loads(prompt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError("run prompt file is invalid") from exc
    if not isinstance(prompt, dict) or not isinstance(prompt.get("prompt"), str):
        raise ToolError("run prompt file is invalid")
    digest = hashlib.sha256(f"{prompt.get('id')}\n{prompt['prompt']}\n{prompt.get('variant')}".encode("utf-8")).digest()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(output.parent, output.suffix)
    try:
        image = Image.new("RGB", (512, 512), (digest[0], digest[1], digest[2]))
        draw = ImageDraw.Draw(image)
        for position in range(0, 257, 64):
            color = (
                digest[(position // 64 + 3) % len(digest)],
                digest[(position // 64 + 9) % len(digest)],
                digest[(position // 64 + 15) % len(digest)],
            )
            draw.rectangle(
                (position, position, 511 - position // 2, 511 - position // 2),
                outline=color,
                width=4,
            )
        image.save(temporary, format="PNG", optimize=True)
        _commit_new_file(temporary, output)
        return _image_receipt(output)
    finally:
        temporary.unlink(missing_ok=True)


def _success_receipt(job: dict[str, Any], provider: str, output: dict[str, Any], alias: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "version": 1,
        "job_id": job["job_id"],
        "status": "succeeded",
        "retry_class": "none",
        "provider": provider,
        "output": output,
    }
    if alias:
        result["adapter_alias"] = alias
    return result


def _failure_receipt(job: dict[str, Any], provider: str, error: AdapterError | ToolError) -> dict[str, Any]:
    if isinstance(error, AdapterError):
        status = error.status
        code = error.code
    else:
        status = "terminal"
        code = "execution_failed"
    return {
        "version": 1,
        "job_id": job["job_id"],
        "status": status,
        "retry_class": status,
        "provider": provider,
        "error_code": code,
    }


def _write_receipt(path: Path, payload: dict[str, Any], *, replace: bool) -> None:
    if not replace:
        _write_json_new(path, payload)
        return
    temporary = _temporary_path(path.parent, ".json")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _execute_one(
    run_dir: Path,
    job: dict[str, Any],
    provider: str,
    config_value: str | Path | None,
) -> dict[str, Any]:
    prompt, output, receipt_path = _job_paths(run_dir, job)
    if output.exists():
        raise ToolError("job output collision")
    try:
        if provider == "stub":
            output_data = _stub_output(prompt, output)
            receipt = _success_receipt(job, "stub", output_data, None)
        else:
            if config_value is None:
                raise AdapterError("no generation command config is provided", code="config_missing")
            result = generate_command(config_value, prompt, output, prompt, True)
            output_data = result["output"]
            receipt = _success_receipt(job, "command", output_data, result.get("adapter_alias"))
    except AdapterError as exc:
        receipt = _failure_receipt(job, provider, exc)
    except ToolError as exc:
        receipt = _failure_receipt(job, provider, exc)
    _write_receipt(receipt_path, receipt, replace=receipt_path.exists())
    return receipt


def run_execute(
    run_value: str | Path,
    provider: str,
    config_value: str | Path | None,
    concurrency: int,
    max_jobs: int | None,
    confirm: bool,
) -> dict[str, Any]:
    if provider not in {"stub", "command"}:
        raise ToolError("run provider must be stub or command")
    if isinstance(concurrency, bool) or not 1 <= concurrency <= 2:
        raise ToolError("run concurrency must be between 1 and 2")
    if max_jobs is not None and (isinstance(max_jobs, bool) or max_jobs < 1):
        raise ToolError("max-jobs must be positive")
    run_dir, run_meta, jobs = _load_run(run_value)
    selected = jobs if max_jobs is None else jobs[:max_jobs]
    if not confirm:
        return {
            "ok": True,
            "command": "run execute",
            "status": "preview",
            "executed": False,
            "run_id": run_meta.get("run_id"),
            "provider": provider,
            "job_count": len(selected),
            "concurrency": concurrency,
        }
    require_confirm(confirm, "run execution")

    preflight: list[str] = []
    states: dict[str, tuple[str, dict[str, Any] | None]] = {}
    for job in selected:
        _, output, receipt = _job_paths(run_dir, job)
        state, existing = _read_receipt(receipt, job, output)
        states[job["job_id"]] = (state, existing)
        if state == "blocked":
            preflight.append(str(job.get("job_id", "unknown")))
    if preflight:
        return {
            "ok": False,
            "command": "run execute",
            "status": "blocked",
            "error": "receipt or output collision/mismatch",
            "blocked_jobs": sorted(preflight),
        }

    pending = [job for job in selected if states[job["job_id"]][0] in {"pending", "retryable"}]
    results: list[dict[str, Any]] = []
    if pending:
        with ThreadPoolExecutor(max_workers=min(2, concurrency)) as executor:
            futures = [executor.submit(_execute_one, run_dir, job, provider, config_value) for job in pending]
            for future in futures:
                try:
                    results.append(future.result())
                except (ToolError, OSError):
                    results.append({"status": "terminal", "retry_class": "terminal", "error_code": "execution_failed"})
    succeeded = sum(item.get("status") == "succeeded" for item in results)
    retryable = sum(item.get("status") == "retryable" for item in results)
    terminal = sum(item.get("status") == "terminal" for item in results) + sum(
        states[job["job_id"]][0] == "terminal" for job in selected
    )
    skipped = sum(states[job["job_id"]][0] == "succeeded" for job in selected)
    return {
        "ok": terminal == 0 and retryable == 0,
        "command": "run execute",
        "status": "completed" if terminal == 0 and retryable == 0 else "partial",
        "run_id": run_meta.get("run_id"),
        "provider": provider,
        "job_count": len(selected),
        "succeeded": succeeded,
        "retryable": retryable,
        "terminal": terminal,
        "skipped": skipped,
    }


def run_status(run_value: str | Path) -> dict[str, Any]:
    run_dir, run_meta, jobs = _load_run(run_value)
    projection: list[dict[str, Any]] = []
    counts = {"pending": 0, "succeeded": 0, "retryable": 0, "terminal": 0, "blocked": 0}
    for job in jobs:
        _, output, receipt = _job_paths(run_dir, job)
        state, raw = _read_receipt(receipt, job, output)
        if state == "blocked":
            counts["blocked"] += 1
            status = "blocked"
        else:
            counts[state] += 1
            status = state
        projection.append(
            {
                "job_id": job.get("job_id"),
                "prompt_id": job.get("prompt_id"),
                "index": job.get("index"),
                "variant": job.get("variant"),
                "status": status,
                "retry_class": raw.get("retry_class") if isinstance(raw, dict) else None,
            }
        )
    return {
        "ok": True,
        "command": "run status",
        "run_id": run_meta.get("run_id"),
        "plan_sha256": run_meta.get("plan_sha256"),
        "counts": counts,
        "jobs": projection,
    }
