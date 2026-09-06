from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from stock_automation_tool.cli import main


def invoke(capsys, *args: str) -> tuple[int, dict]:
    code = main(list(args))
    captured = capsys.readouterr()
    return code, json.loads(captured.out)


def write_plan(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "prompts": [
                    {
                        "id": "P-001",
                        "title": "Alpine Morning",
                        "prompt": "A geometric alpine sunrise for a local fixture",
                        "keywords": ["alpine", "morning", "sunrise"],
                        "category": 8,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_confirm_guards_and_smoke_path(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    write_plan(plan)
    generated = tmp_path / "generated"
    upscaled = tmp_path / "upscaled"
    metadata = tmp_path / "metadata.csv"
    package = tmp_path / "package"

    code, result = invoke(capsys, "generate", "stub", "--plan", "plan.json", "--output-dir", "generated")
    assert code != 0 and result["ok"] is False
    assert not generated.exists()
    code, result = invoke(
        capsys,
        "generate",
        "stub",
        "--plan",
        "plan.json",
        "--output-dir",
        "generated",
        "--confirm",
    )
    assert code == 0 and result["provider"] == "stub"
    generated_file = generated / "p-001.png"
    assert generated_file.exists()

    code, result = invoke(capsys, "qc", "technical", "--folder", "generated")
    assert code == 0 and result["passed"] is True
    assert result["images"][0]["decode"] is True
    assert result["images"][0]["dimensions"] == {"width": 512, "height": 512}
    assert result["images"][0]["mode"] == "RGB"
    assert result["images"][0]["alpha"] is False
    assert result["images"][0]["megapixels"] == 0.262144

    code, result = invoke(
        capsys,
        "upscale",
        "pillow",
        "--input",
        "generated/p-001.png",
        "--output-dir",
        "upscaled",
        "--scale",
        "2",
    )
    assert code != 0 and not upscaled.exists()
    code, result = invoke(
        capsys,
        "upscale",
        "pillow",
        "--input",
        "generated/p-001.png",
        "--output-dir",
        "upscaled",
        "--scale",
        "2",
        "--confirm",
    )
    assert code == 0 and result["dimensions"] == {"width": 1024, "height": 1024}
    with Image.open(upscaled / "p-001.png") as image:
        assert image.size == (1024, 1024)

    code, result = invoke(
        capsys,
        "metadata",
        "build",
        "--folder",
        "upscaled",
        "--plan",
        "plan.json",
        "--csv",
        "metadata.csv",
    )
    assert code != 0 and not metadata.exists()
    code, result = invoke(
        capsys,
        "metadata",
        "build",
        "--folder",
        "upscaled",
        "--plan",
        "plan.json",
        "--csv",
        "metadata.csv",
        "--confirm",
    )
    assert code == 0 and result["count"] == 1
    with metadata.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [
        {
            "filename": "p-001.png",
            "title": "Alpine Morning",
            "keywords": "alpine, morning, sunrise",
            "category": "8",
        }
    ]

    code, result = invoke(
        capsys,
        "package",
        "build",
        "--folder",
        "upscaled",
        "--csv",
        "metadata.csv",
        "--output-dir",
        "package",
    )
    assert code != 0 and not package.exists()
    code, result = invoke(
        capsys,
        "package",
        "build",
        "--folder",
        "upscaled",
        "--csv",
        "metadata.csv",
        "--output-dir",
        "package",
        "--confirm",
    )
    assert code == 0 and result["csv_join_valid"] is True
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["csv_join_validation"]["valid"] is True
    assert manifest["images"][0]["sha256"] == hashlib.sha256(
        (package / "p-001.png").read_bytes()
    ).hexdigest()
    assert manifest["csv"]["sha256"] == hashlib.sha256((package / "metadata.csv").read_bytes()).hexdigest()
    digest = manifest.pop("package_digest")
    assert digest == hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    bad_evidence = tmp_path / "bad.visual.json"
    bad_evidence.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "visual_qc",
                "input_sha256": "0" * 64,
                "stage": "post_upscale",
                "adapter_alias": "test",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "score": 99,
                "verdict": "pass",
                "defects": [],
                "confidence": 1,
            }
        ),
        encoding="utf-8",
    )
    code, result = invoke(
        capsys,
        "package",
        "build",
        "--folder",
        "upscaled",
        "--csv",
        "metadata.csv",
        "--output-dir",
        "package-with-bad-evidence",
        "--visual-evidence",
        "bad.visual.json",
        "--confirm",
    )
    assert code != 0 and result["ok"] is False
    assert not (tmp_path / "package-with-bad-evidence").exists()


def test_plan_validation_and_mapping(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    write_plan(plan)
    code, result = invoke(capsys, "plan", "validate", "--file", "plan.json")
    assert code == 0
    assert result["valid"] is True
    assert result["filename_mapping"] == [
        {"id": "P-001", "title": "Alpine Morning", "stem": "p-001"}
    ]

    bad = tmp_path / "bad.json"
    bad.write_text('{"prompts": [{"id": "x"}]}', encoding="utf-8")
    code, result = invoke(capsys, "plan", "validate", "--file", "bad.json")
    assert code != 0 and result["ok"] is False


def test_containment_failure(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    write_plan(plan)
    outside = tmp_path.parent / "stock-tool-outside-test"
    code, result = invoke(
        capsys,
        "generate",
        "stub",
        "--plan",
        "plan.json",
        "--output-dir",
        str(outside),
        "--confirm",
    )
    assert code != 0 and result["ok"] is False
    assert not outside.exists()


def test_symlinked_read_paths_are_rejected(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    write_plan(plan)
    images = tmp_path / "images"
    images.mkdir()
    Image.new("RGB", (8, 8), "red").save(images / "p-001.png")
    try:
        (tmp_path / "plan-link.json").symlink_to(plan)
        (tmp_path / "images-link").symlink_to(images, target_is_directory=True)
        (tmp_path / "input-link.png").symlink_to(images / "p-001.png")
    except OSError as exc:
        pytest.skip(f"symlinks unsupported: {exc}")

    code, result = invoke(capsys, "plan", "validate", "--file", "plan-link.json")
    assert code != 0 and result["ok"] is False
    code, result = invoke(capsys, "qc", "technical", "--folder", "images-link")
    assert code != 0 and result["ok"] is False
    code, result = invoke(
        capsys,
        "upscale",
        "pillow",
        "--input",
        "input-link.png",
        "--output-dir",
        "upscaled",
        "--scale",
        "2",
        "--confirm",
    )
    assert code != 0 and result["ok"] is False
    assert not (tmp_path / "upscaled").exists()


def test_external_image_symlink_is_rejected_by_all_image_operations(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    write_plan(plan)
    images = tmp_path / "images"
    images.mkdir()
    outside_image = tmp_path.parent / f"{tmp_path.name}-external.png"
    Image.new("RGB", (8, 8), "red").save(outside_image)
    try:
        (images / "p-001.png").symlink_to(outside_image)
    except OSError as exc:
        pytest.skip(f"symlinks unsupported: {exc}")

    code, result = invoke(capsys, "qc", "technical", "--folder", "images")
    assert code != 0 and result["ok"] is False

    code, result = invoke(
        capsys,
        "metadata",
        "build",
        "--folder",
        "images",
        "--plan",
        "plan.json",
        "--csv",
        "metadata.csv",
        "--confirm",
    )
    assert code != 0 and result["ok"] is False
    assert not (tmp_path / "metadata.csv").exists()

    code, result = invoke(
        capsys,
        "package",
        "build",
        "--folder",
        "images",
        "--csv",
        "metadata.csv",
        "--output-dir",
        "package",
        "--confirm",
    )
    assert code != 0 and result["ok"] is False
    assert not (tmp_path / "package").exists()


def test_invalid_scales_are_rejected_before_output_creation(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Image.new("RGB", (8, 8), "blue").save(tmp_path / "input.png")

    for index, scale in enumerate(("nan", "inf", "-inf", "1e9999", "1e308")):
        output_dir = tmp_path / f"upscaled-{index}"
        code, result = invoke(
            capsys,
            "upscale",
            "pillow",
            "--input",
            "input.png",
            "--output-dir",
            output_dir.name,
            "--scale",
            scale,
            "--confirm",
        )
        assert code != 0
        assert result["ok"] is False
        assert not output_dir.exists()


def test_subprocess_scale_error_output_is_json_parseable(tmp_path):
    Image.new("RGB", (8, 8), "green").save(tmp_path / "input.png")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "stock_automation_tool",
            "upscale",
            "pillow",
            "--input",
            "input.png",
            "--output-dir",
            "upscaled",
            "--scale",
            "1e9999",
            "--confirm",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    result = json.loads(completed.stdout)
    assert result["ok"] is False
    assert not (tmp_path / "upscaled").exists()


def test_command_adapter_preview_and_image_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Image.new("RGB", (4, 4), "red").save(tmp_path / "input.png")
    (tmp_path / "prompt.json").write_text('{"prompt": "adapter prompt"}', encoding="utf-8")
    (tmp_path / "provider.py").write_text(
        "import json, sys\n"
        "from PIL import Image\n"
        "Image.new('RGB', (7, 5), 'blue').save(sys.argv[2])\n"
        "print(json.dumps({'ok': True, 'raw': 'must not be returned'}))\n",
        encoding="utf-8",
    )
    (tmp_path / "adapter.json").write_text(
        json.dumps(
            {
                "generate": {
                    "alias": "local-test",
                    "command": [
                        sys.executable,
                        "provider.py",
                        "{prompt_file}",
                        "{output_path}",
                        "{input_path}",
                    ],
                    "timeout_seconds": 20,
                }
            }
        ),
        encoding="utf-8",
    )
    code, preview = invoke(
        capsys,
        "generate",
        "command",
        "--config",
        "adapter.json",
        "--prompt-file",
        "prompt.json",
        "--output-path",
        "generated.png",
        "--input-path",
        "input.png",
    )
    assert code == 0 and preview["executed"] is False
    assert not (tmp_path / "generated.png").exists()
    code, result = invoke(
        capsys,
        "generate",
        "command",
        "--config",
        "adapter.json",
        "--prompt-file",
        "prompt.json",
        "--output-path",
        "generated.png",
        "--input-path",
        "input.png",
        "--confirm",
    )
    assert code == 0
    assert result["output"]["dimensions"] == {"width": 7, "height": 5}
    assert "raw" not in json.dumps(result)
    (tmp_path / "provider.py").write_text(
        "import sys\n"
        "print('provider stdout secret and adapter prompt')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    code, failed = invoke(
        capsys,
        "generate",
        "command",
        "--config",
        "adapter.json",
        "--prompt-file",
        "prompt.json",
        "--output-path",
        "failed.png",
        "--input-path",
        "input.png",
        "--confirm",
    )
    assert code != 0
    assert "provider stdout secret" not in json.dumps(failed)
    assert "adapter prompt" not in json.dumps(failed)


def test_run_jobs_are_deterministic_and_resume_idempotently(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write_plan(tmp_path / "plan.json")
    code, preview = invoke(capsys, "run", "export", "--plan", "plan.json", "--output-dir", "run")
    assert code == 0 and preview["status"] == "preview"
    assert not (tmp_path / "run").exists()
    code, exported = invoke(
        capsys,
        "run",
        "export",
        "--plan",
        "plan.json",
        "--output-dir",
        "run",
        "--run-id",
        "demo",
        "--confirm",
    )
    assert code == 0 and exported["job_count"] == 1
    job_data = json.loads((tmp_path / "run" / "jobs.json").read_text(encoding="utf-8"))
    assert job_data["jobs"][0]["output"] == "outputs/p-001.png"
    code, executed = invoke(capsys, "run", "execute", "--run-dir", "run", "--provider", "stub", "--confirm")
    assert code == 0 and executed["succeeded"] == 1
    code, resumed = invoke(capsys, "run", "execute", "--run-dir", "run", "--provider", "stub", "--confirm")
    assert code == 0 and resumed["skipped"] == 1
    code, status = invoke(capsys, "run", "status", "--run-dir", "run")
    assert code == 0 and status["counts"]["succeeded"] == 1


def test_run_receipt_mismatch_blocks_resume(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write_plan(tmp_path / "plan.json")
    invoke(capsys, "run", "export", "--plan", "plan.json", "--output-dir", "run", "--confirm")
    invoke(capsys, "run", "execute", "--run-dir", "run", "--provider", "stub", "--confirm")
    output = tmp_path / "run" / "outputs" / "p-001.png"
    output.write_bytes(b"tampered")
    code, result = invoke(capsys, "run", "execute", "--run-dir", "run", "--provider", "stub", "--confirm")
    assert code != 0
    assert result["status"] == "blocked"
    assert result["blocked_jobs"]


def test_visual_qc_evidence_is_hash_bound_and_requires_confirmation(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Image.new("RGB", (4, 4), "red").save(tmp_path / "input.png")
    (tmp_path / "qc.py").write_text(
        "import json\n"
        "print(json.dumps({'score': 96, 'verdict': 'pass', 'defects': [], 'confidence': 0.9}))\n",
        encoding="utf-8",
    )
    (tmp_path / "adapter.json").write_text(
        json.dumps(
            {
                "visual_qc": {
                    "alias": "local-qc",
                    "command": [sys.executable, "qc.py", "{input_path}"],
                    "timeout_seconds": 20,
                }
            }
        ),
        encoding="utf-8",
    )
    code, preview = invoke(
        capsys,
        "qc",
        "visual",
        "command",
        "--config",
        "adapter.json",
        "--input",
        "input.png",
        "--stage",
        "post_upscale",
        "--evidence",
        "evidence.json",
    )
    assert code == 0 and preview["executed"] is False
    assert not (tmp_path / "evidence.json").exists()
    code, result = invoke(
        capsys,
        "qc",
        "visual",
        "command",
        "--config",
        "adapter.json",
        "--input",
        "input.png",
        "--stage",
        "post_upscale",
        "--evidence",
        "evidence.json",
        "--confirm",
    )
    assert code == 0 and result["score"] == 96
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["input_sha256"] == hashlib.sha256((tmp_path / "input.png").read_bytes()).hexdigest()
    (tmp_path / "input.png").write_bytes(b"changed")
    assert not (tmp_path / "evidence.json").read_text(encoding="utf-8").find("must not be returned") >= 0


def test_pre_upscale_report_cannot_qualify_an_upscaled_package(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    write_plan(plan)
    invoke(capsys, "generate", "stub", "--plan", "plan.json", "--output-dir", "generated", "--confirm")
    invoke(
        capsys,
        "upscale",
        "pillow",
        "--input",
        "generated/p-001.png",
        "--output-dir",
        "upscaled",
        "--scale",
        "2",
        "--confirm",
    )
    invoke(
        capsys,
        "metadata",
        "build",
        "--folder",
        "upscaled",
        "--plan",
        "plan.json",
        "--csv",
        "metadata.csv",
        "--confirm",
    )
    invoke(
        capsys,
        "qc",
        "technical",
        "--folder",
        "generated",
        "--output",
        "pre.json",
        "--stage",
        "source",
        "--confirm",
    )
    code, result = invoke(
        capsys,
        "package",
        "build",
        "--folder",
        "upscaled",
        "--csv",
        "metadata.csv",
        "--technical-report",
        "pre.json",
        "--output-dir",
        "package-pre",
        "--confirm",
    )
    assert code != 0 and result["ok"] is False
    assert not (tmp_path / "package-pre").exists()

    invoke(
        capsys,
        "qc",
        "technical",
        "--folder",
        "upscaled",
        "--output",
        "source-on-upscaled.json",
        "--stage",
        "source",
        "--confirm",
    )
    code, result = invoke(
        capsys,
        "package",
        "build",
        "--folder",
        "upscaled",
        "--csv",
        "metadata.csv",
        "--technical-report",
        "source-on-upscaled.json",
        "--output-dir",
        "package-source-stage",
        "--confirm",
    )
    assert code != 0 and result["ok"] is False
    assert not (tmp_path / "package-source-stage").exists()

    code, result = invoke(
        capsys,
        "qc",
        "technical",
        "--folder",
        "upscaled",
        "--output",
        "post.json",
        "--stage",
        "post_upscale",
        "--confirm",
    )
    assert code == 0 and result["passed"] is True
    code, result = invoke(
        capsys,
        "package",
        "build",
        "--folder",
        "upscaled",
        "--csv",
        "metadata.csv",
        "--technical-report",
        "post.json",
        "--output-dir",
        "package-post",
        "--confirm",
    )
    assert code == 0 and result["eligible"] is True

    bad_post = json.loads((tmp_path / "post.json").read_text(encoding="utf-8"))
    bad_post["image_hashes"]["p-001.png"] = "0" * 64
    (tmp_path / "post-bad.json").write_text(json.dumps(bad_post), encoding="utf-8")
    code, result = invoke(
        capsys,
        "package",
        "build",
        "--folder",
        "upscaled",
        "--csv",
        "metadata.csv",
        "--technical-report",
        "post-bad.json",
        "--output-dir",
        "package-hash-mismatch",
        "--confirm",
    )
    assert code != 0 and result["ok"] is False
    assert not (tmp_path / "package-hash-mismatch").exists()

    failed_evidence = tmp_path / "failed.visual.json"
    failed_evidence.write_text(
        json.dumps(
            {
                "version": 1,
                "kind": "visual_qc",
                "input_sha256": hashlib.sha256((tmp_path / "upscaled/p-001.png").read_bytes()).hexdigest(),
                "stage": "post_upscale",
                "adapter_alias": "test",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "score": 20,
                "verdict": "fail",
                "defects": ["defect"],
                "confidence": 1,
            }
        ),
        encoding="utf-8",
    )
    code, result = invoke(
        capsys,
        "package",
        "build",
        "--folder",
        "upscaled",
        "--csv",
        "metadata.csv",
        "--visual-evidence",
        "failed.visual.json",
        "--output-dir",
        "package-fail",
        "--confirm",
    )
    assert code != 0 and result["ok"] is False
    assert not (tmp_path / "package-fail").exists()


def test_cli_help_and_malformed_arguments_are_single_json_lines(tmp_path):
    for arguments in (("--help",), ("unknown-command",)):
        completed = subprocess.run(
            [sys.executable, "-m", "stock_automation_tool", *arguments],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0
        lines = completed.stdout.splitlines()
        assert len(lines) == 1
        result = json.loads(lines[0])
        assert result["ok"] is False
