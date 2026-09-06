"""Core pipeline operations.

The package deliberately has no provider, browser, or portal integration.  A
filename is safely associated with a prompt by slugifying its ``id`` or
``title``: Unicode is reduced to ASCII, runs of non-alphanumeric characters
become one hyphen, and the result is lower case.  Generated files use the ID
slug.  Metadata accepts either the ID slug or the title slug, provided the
mapping is unique and one image cannot represent a prompt twice.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
CSV_COLUMNS = ["filename", "title", "keywords", "category"]
# Keep local Pillow resizes bounded before allocation.  These limits are
# intentionally conservative for a practical, provider-free CLI operation.
MAX_UPSCALE_DIMENSION = 16_384
MAX_UPSCALE_PIXELS = 64_000_000


class ToolError(Exception):
    """A user-facing, JSON-serializable command error."""


@dataclass(frozen=True)
class Prompt:
    prompt_id: str
    title: str
    prompt: str
    keywords: tuple[str, ...]
    category: str


def workspace_root() -> Path:
    """Return the resolved current working directory used as the workspace."""

    return Path.cwd().resolve()


def contained_path(value: str | Path, label: str, *, must_exist: bool = False) -> Path:
    """Resolve a path and reject it unless it stays inside the current cwd."""

    root = workspace_root()
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ToolError(f"{label} must be inside the current workspace") from exc
    if must_exist and not resolved.exists():
        raise ToolError(f"{label} does not exist: {value}")
    return resolved


def display_path(path: Path) -> str:
    """Use stable workspace-relative paths in command output."""

    return path.relative_to(workspace_root()).as_posix()


def require_confirm(confirm: bool, operation: str) -> None:
    if not confirm:
        raise ToolError(f"{operation} mutates local files; pass --confirm")


def safe_stem(value: str) -> str:
    """Return the documented, collision-prone-safe filename mapping."""

    ascii_value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")


def _nonempty_string(value: Any, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"prompt {index} field {field!r} must be a non-empty string")
    return value.strip()


def _keywords(value: Any, index: int) -> tuple[str, ...]:
    if isinstance(value, str):
        values: Iterable[Any] = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        raise ToolError(f"prompt {index} field 'keywords' must be a list or string")
    result = tuple(str(item).strip() for item in values if str(item).strip())
    if not result:
        raise ToolError(f"prompt {index} must contain at least one keyword")
    if any("," in item for item in result):
        raise ToolError(f"prompt {index} keywords must not contain commas")
    return result


def load_plan(path_value: str | Path) -> tuple[Path, list[Prompt], dict[str, Prompt]]:
    """Load and validate the small JSON prompt-plan format."""

    path = contained_path(path_value, "plan file", must_exist=True)
    if not path.is_file():
        raise ToolError(f"plan file is not a file: {path_value}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolError(f"could not read JSON plan: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("prompts"), list):
        raise ToolError("plan must be an object with a non-empty 'prompts' list")
    if not raw["prompts"]:
        raise ToolError("plan must contain at least one prompt")

    prompts: list[Prompt] = []
    aliases: dict[str, Prompt] = {}
    ids: set[str] = set()
    for index, item in enumerate(raw["prompts"], start=1):
        if not isinstance(item, dict):
            raise ToolError(f"prompt {index} must be an object")
        prompt_id = _nonempty_string(item.get("id"), "id", index)
        title = _nonempty_string(item.get("title"), "title", index)
        prompt_text = _nonempty_string(item.get("prompt"), "prompt", index)
        keywords = _keywords(item.get("keywords"), index)
        category_value = item.get("category")
        if not isinstance(category_value, (str, int)) or isinstance(category_value, bool):
            raise ToolError(f"prompt {index} field 'category' must be a string or integer")
        category = str(category_value).strip()
        if not category:
            raise ToolError(f"prompt {index} field 'category' must not be empty")

        id_stem = safe_stem(prompt_id)
        title_stem = safe_stem(title)
        if not id_stem or not title_stem:
            raise ToolError(f"prompt {index} id and title must produce safe filename stems")
        if id_stem in ids:
            raise ToolError(f"duplicate prompt id or ID filename stem: {prompt_id}")
        ids.add(id_stem)
        prompt = Prompt(prompt_id, title, prompt_text, keywords, category)
        prompts.append(prompt)
        for alias in (id_stem, title_stem):
            previous = aliases.get(alias)
            if previous is not None and previous != prompt:
                raise ToolError(f"ambiguous filename mapping for stem: {alias}")
            aliases[alias] = prompt
    return path, prompts, aliases


def plan_validation(path_value: str | Path) -> dict[str, Any]:
    path, prompts, _ = load_plan(path_value)
    return {
        "ok": True,
        "valid": True,
        "command": "plan validate",
        "plan": display_path(path),
        "prompt_count": len(prompts),
        "filename_mapping": [
            {"id": item.prompt_id, "title": item.title, "stem": safe_stem(item.prompt_id)}
            for item in prompts
        ],
    }


def image_files(folder_value: str | Path) -> tuple[Path, list[Path]]:
    folder = contained_path(folder_value, "folder", must_exist=True)
    if not folder.is_dir():
        raise ToolError(f"folder is not a directory: {folder_value}")
    try:
        entries = sorted(folder.iterdir(), key=lambda item: item.name.lower())
    except OSError as exc:
        raise ToolError(f"could not inspect image folder: {exc}") from exc

    root = workspace_root()
    for entry in entries:
        if entry.is_symlink():
            raise ToolError(f"folder contains a symlink entry: {entry.name}")
        try:
            entry.resolve(strict=False).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ToolError(f"image path must be inside the current workspace: {entry.name}") from exc

    files = [
        entry for entry in entries if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS
    ]
    return folder, files


def _check_new_path(path: Path, label: str) -> None:
    if path.exists():
        raise ToolError(f"refusing to overwrite existing {label}: {display_path(path)}")


def _upscale_dimensions(width: int, height: int, scale: float) -> tuple[int, int]:
    """Compute safe Pillow dimensions without allowing overflowing arithmetic."""

    limit_message = (
        f"practical Pillow safety limit is {MAX_UPSCALE_DIMENSION} pixels per side "
        f"and {MAX_UPSCALE_PIXELS} total pixels"
    )
    try:
        scaled_width = width * scale
        scaled_height = height * scale
    except (OverflowError, TypeError, ValueError) as exc:
        raise ToolError(f"target dimensions are not representable; {limit_message}") from exc
    if not math.isfinite(scaled_width) or not math.isfinite(scaled_height):
        raise ToolError(f"target dimensions are not representable; {limit_message}")

    try:
        new_width = round(scaled_width)
        new_height = round(scaled_height)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ToolError(f"target dimensions are not representable; {limit_message}") from exc
    if new_width <= 0 or new_height <= 0:
        raise ToolError(f"target dimensions must be positive; {limit_message}")
    if (
        new_width > MAX_UPSCALE_DIMENSION
        or new_height > MAX_UPSCALE_DIMENSION
        or new_width * new_height > MAX_UPSCALE_PIXELS
    ):
        raise ToolError(
            f"target dimensions {new_width}x{new_height} exceed the {limit_message}"
        )
    return new_width, new_height


def generate_stub(plan_value: str | Path, output_value: str | Path, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, "stub generation")
    _, prompts, _ = load_plan(plan_value)
    output_dir = contained_path(output_value, "output directory")
    if output_dir.exists() and not output_dir.is_dir():
        raise ToolError(f"output directory is not a directory: {output_value}")
    output_paths = [output_dir / f"{safe_stem(item.prompt_id)}.png" for item in prompts]
    for path in output_paths:
        _check_new_path(path, "generated image")
    output_dir.mkdir(parents=True, exist_ok=True)

    generated: list[str] = []
    for item, path in zip(prompts, output_paths):
        digest = hashlib.sha256(f"{item.prompt_id}\n{item.prompt}".encode("utf-8")).digest()
        image = Image.new("RGB", (512, 512), (digest[0], digest[1], digest[2]))
        draw = ImageDraw.Draw(image)
        # Simple geometric content makes the local fixture visibly non-empty
        # while remaining deterministic and provider-free.
        for position in range(0, 257, 64):
            color = (digest[(position // 64 + 3) % len(digest)], digest[(position // 64 + 9) % len(digest)], digest[(position // 64 + 15) % len(digest)])
            draw.rectangle((position, position, 511 - position // 2, 511 - position // 2), outline=color, width=4)
        draw.ellipse((128, 128, 384, 384), outline=(digest[10], digest[11], digest[12]), width=8)
        image.save(path, format="PNG", optimize=True)
        generated.append(display_path(path))
    return {
        "ok": True,
        "command": "generate stub",
        "provider": "stub",
        "output_dir": display_path(output_dir),
        "generated": generated,
        "count": len(generated),
    }


def technical_qc(folder_value: str | Path) -> dict[str, Any]:
    folder, files = image_files(folder_value)
    reports: list[dict[str, Any]] = []
    for path in files:
        report: dict[str, Any] = {"filename": path.name, "decode": False}
        try:
            with Image.open(path) as image:
                image.load()
                width, height = image.size
                mode = image.mode
                has_alpha = "A" in image.getbands()
                report.update(
                    {
                        "decode": True,
                        "dimensions": {"width": width, "height": height},
                        "width": width,
                        "height": height,
                        "mode": mode,
                        "rgb_or_rgba": mode in {"RGB", "RGBA"},
                        "alpha": has_alpha,
                        "megapixels": round((width * height) / 1_000_000, 6),
                    }
                )
        except Exception as exc:  # Pillow uses several format-specific errors.
            report["error"] = str(exc)
        reports.append(report)
    passed = bool(files) and all(
        report.get("decode") is True and report.get("rgb_or_rgba") is True for report in reports
    )
    return {
        "ok": True,
        "command": "qc technical",
        "folder": display_path(folder),
        "count": len(reports),
        "passed": passed,
        "images": reports,
    }


def upscale_pillow(
    input_value: str | Path, output_value: str | Path, scale_value: str | float, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, "Pillow upscale")
    source = contained_path(input_value, "input image", must_exist=True)
    if not source.is_file():
        raise ToolError(f"input image is not a file: {input_value}")
    if source.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ToolError("input image must be JPG or PNG")
    try:
        scale = float(scale_value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ToolError("scale must be a positive number") from exc
    if not math.isfinite(scale) or scale <= 0:
        raise ToolError("scale must be a positive number")

    output_dir = contained_path(output_value, "output directory")
    if output_dir.exists() and not output_dir.is_dir():
        raise ToolError(f"output directory is not a directory: {output_value}")
    destination = output_dir / source.name
    _check_new_path(destination, "upscaled image")
    try:
        with Image.open(source) as image:
            image.load()
            width, height = image.size
            new_size = _upscale_dimensions(width, height, scale)
            output_dir.mkdir(parents=True, exist_ok=True)
            resized = image.resize(new_size, Image.Resampling.LANCZOS)
            if destination.suffix.lower() in {".jpg", ".jpeg"} and resized.mode in {"RGBA", "LA"}:
                resized = resized.convert("RGB")
            save_kwargs: dict[str, Any] = {"optimize": True}
            if destination.suffix.lower() in {".jpg", ".jpeg"}:
                save_kwargs["quality"] = 95
            resized.save(destination, **save_kwargs)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"could not upscale image: {exc}") from exc
    return {
        "ok": True,
        "command": "upscale pillow",
        "input": display_path(source),
        "output": display_path(destination),
        "scale": scale,
        "dimensions": {"width": new_size[0], "height": new_size[1]},
    }


def _metadata_rows(folder: Path, files: list[Path], aliases: dict[str, Prompt]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen_prompts: set[int] = set()
    for path in files:
        prompt = aliases.get(path.stem)
        if prompt is None:
            raise ToolError(
                f"image stem {path.stem!r} does not match a prompt ID or title safe stem"
            )
        marker = id(prompt)
        if marker in seen_prompts:
            raise ToolError(f"multiple images map to prompt {prompt.prompt_id!r}")
        seen_prompts.add(marker)
        rows.append(
            {
                "filename": path.name,
                "title": prompt.title,
                "keywords": ", ".join(prompt.keywords),
                "category": prompt.category,
            }
        )
    if not rows:
        raise ToolError("metadata requires at least one JPG or PNG image")
    return rows


def build_metadata(
    folder_value: str | Path,
    plan_value: str | Path,
    csv_value: str | Path,
    confirm: bool,
) -> dict[str, Any]:
    require_confirm(confirm, "metadata build")
    folder, files = image_files(folder_value)
    plan_path, _, aliases = load_plan(plan_value)
    csv_path = contained_path(csv_value, "CSV output")
    if csv_path.suffix.lower() != ".csv":
        raise ToolError("CSV output must use a .csv extension")
    _check_new_path(csv_path, "CSV output")
    rows = _metadata_rows(folder, files, aliases)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        raise ToolError(f"could not write CSV: {exc}") from exc
    return {
        "ok": True,
        "command": "metadata build",
        "folder": display_path(folder),
        "plan": display_path(plan_path),
        "csv": display_path(csv_path),
        "count": len(rows),
        "columns": CSV_COLUMNS,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv(csv_path: Path) -> list[dict[str, str]]:
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != CSV_COLUMNS:
                raise ToolError(f"CSV columns must be exactly: {','.join(CSV_COLUMNS)}")
            rows = list(reader)
    except UnicodeDecodeError as exc:
        raise ToolError(f"CSV is not UTF-8: {exc}") from exc
    except OSError as exc:
        raise ToolError(f"could not read CSV: {exc}") from exc
    for row in rows:
        if not row.get("filename"):
            raise ToolError("CSV contains a row without a filename")
        filename = row["filename"]
        if Path(filename).name != filename or Path(filename).is_absolute():
            raise ToolError(f"CSV filename must be a package basename: {filename}")
    return rows


def build_package(
    folder_value: str | Path,
    csv_value: str | Path,
    output_value: str | Path,
    confirm: bool,
) -> dict[str, Any]:
    require_confirm(confirm, "package build")
    folder, files = image_files(folder_value)
    csv_path = contained_path(csv_value, "CSV input", must_exist=True)
    if not csv_path.is_file():
        raise ToolError(f"CSV input is not a file: {csv_value}")
    output_dir = contained_path(output_value, "package output directory")
    if output_dir.exists() and not output_dir.is_dir():
        raise ToolError(f"package output directory is not a directory: {output_value}")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ToolError(f"refusing to build into non-empty package directory: {display_path(output_dir)}")
    if not files:
        raise ToolError("package requires at least one JPG or PNG image")

    rows = _read_csv(csv_path)
    csv_names = [row["filename"] for row in rows]
    if len(csv_names) != len(set(csv_names)):
        raise ToolError("CSV contains duplicate filenames")
    image_names = [path.name for path in files]
    missing = sorted(set(image_names) - set(csv_names))
    extra = sorted(set(csv_names) - set(image_names))
    if missing or extra:
        raise ToolError(f"CSV/image filename join failed; missing={missing}, extra={extra}")

    package_csv = output_dir / csv_path.name
    destinations = [output_dir / path.name for path in files] + [package_csv, output_dir / "manifest.json"]
    if len({path.name for path in destinations}) != len(destinations):
        raise ToolError("package filenames collide")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        for source in files:
            shutil.copyfile(source, output_dir / source.name)
        shutil.copyfile(csv_path, package_csv)
        image_entries = [
            {"filename": path.name, "sha256": _sha256(output_dir / path.name)} for path in files
        ]
        manifest: dict[str, Any] = {
            "version": 1,
            "images": image_entries,
            "csv": {"filename": package_csv.name, "sha256": _sha256(package_csv)},
            "csv_join_validation": {
                "valid": True,
                "image_count": len(image_names),
                "csv_count": len(csv_names),
                "missing": [],
                "extra": [],
            },
        }
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise ToolError(f"could not build package: {exc}") from exc
    return {
        "ok": True,
        "command": "package build",
        "folder": display_path(folder),
        "csv": display_path(csv_path),
        "output_dir": display_path(output_dir),
        "manifest": display_path(output_dir / "manifest.json"),
        "image_count": len(image_entries),
        "csv_join_valid": True,
    }
