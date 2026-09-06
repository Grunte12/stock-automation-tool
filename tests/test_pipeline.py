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
