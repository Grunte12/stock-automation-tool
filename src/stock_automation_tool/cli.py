"""JSON command-line interface for the local stock automation pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .adapters import AdapterError, generate_command, visual_command
from .core import (
    ToolError,
    build_metadata,
    build_package,
    generate_stub,
    plan_validation,
    technical_qc,
    upscale_pillow,
)
from .runs import run_execute, run_export, run_status


class JsonArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.pop("add_help", None)
        super().__init__(*args, add_help=False, **kwargs)
        self.add_argument("-h", "--help", action=JsonHelpAction, nargs=0)

    def error(self, message: str) -> None:
        raise ToolError(message)


class JsonHelpAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        raise ToolError("help requested; commands return JSON and require explicit arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="stock-automation-tool")
    groups = parser.add_subparsers(dest="group", required=True, parser_class=JsonArgumentParser)

    plan = groups.add_parser("plan")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True, parser_class=JsonArgumentParser)
    validate = plan_commands.add_parser("validate")
    validate.add_argument("--file", required=True)

    generate = groups.add_parser("generate")
    generate_commands = generate.add_subparsers(dest="generate_command", required=True, parser_class=JsonArgumentParser)
    stub = generate_commands.add_parser("stub")
    stub.add_argument("--plan", required=True)
    stub.add_argument("--output-dir", required=True)
    stub.add_argument("--confirm", action="store_true")
    command = generate_commands.add_parser("command")
    command.add_argument("--config", required=True)
    command.add_argument("--prompt-file", required=True)
    command.add_argument("--output-path", required=True)
    command.add_argument("--input-path", required=True)
    command.add_argument("--confirm", action="store_true")

    qc = groups.add_parser("qc")
    qc_commands = qc.add_subparsers(dest="qc_command", required=True, parser_class=JsonArgumentParser)
    technical = qc_commands.add_parser("technical")
    technical.add_argument("--folder", required=True)
    technical.add_argument("--output")
    technical.add_argument("--stage", choices=["source", "post_upscale"], default="source")
    technical.add_argument("--confirm", action="store_true")
    visual_commands = qc_commands.add_parser("visual")
    visual_subcommands = visual_commands.add_subparsers(
        dest="visual_command", required=True, parser_class=JsonArgumentParser
    )
    visual_command_parser = visual_subcommands.add_parser("command")
    visual_command_parser.add_argument("--config", required=True)
    visual_command_parser.add_argument("--input", required=True)
    visual_command_parser.add_argument("--stage", required=True, choices=["pre_upscale", "post_upscale"])
    visual_command_parser.add_argument("--evidence", required=True)
    visual_command_parser.add_argument("--prompt-file")
    visual_command_parser.add_argument("--output-path")
    visual_command_parser.add_argument("--confirm", action="store_true")

    upscale = groups.add_parser("upscale")
    upscale_commands = upscale.add_subparsers(dest="upscale_command", required=True, parser_class=JsonArgumentParser)
    pillow = upscale_commands.add_parser("pillow")
    pillow.add_argument("--input", required=True)
    pillow.add_argument("--output-dir", required=True)
    pillow.add_argument("--scale", required=True)
    pillow.add_argument("--confirm", action="store_true")

    metadata = groups.add_parser("metadata")
    metadata_commands = metadata.add_subparsers(dest="metadata_command", required=True, parser_class=JsonArgumentParser)
    build_metadata_parser = metadata_commands.add_parser("build")
    build_metadata_parser.add_argument("--folder", required=True)
    build_metadata_parser.add_argument("--plan", required=True)
    build_metadata_parser.add_argument("--csv", required=True)
    build_metadata_parser.add_argument("--confirm", action="store_true")

    package = groups.add_parser("package")
    package_commands = package.add_subparsers(dest="package_command", required=True, parser_class=JsonArgumentParser)
    build_package_parser = package_commands.add_parser("build")
    build_package_parser.add_argument("--folder", required=True)
    build_package_parser.add_argument("--csv", required=True)
    build_package_parser.add_argument("--output-dir", required=True)
    build_package_parser.add_argument("--technical-report")
    build_package_parser.add_argument("--visual-evidence")
    build_package_parser.add_argument("--confirm", action="store_true")

    run = groups.add_parser("run")
    run_commands = run.add_subparsers(dest="run_command", required=True, parser_class=JsonArgumentParser)
    export = run_commands.add_parser("export")
    export.add_argument("--plan", required=True)
    export.add_argument("--output-dir", required=True)
    export.add_argument("--run-id")
    export.add_argument("--variants", type=int, default=1)
    export.add_argument("--confirm", action="store_true")
    execute = run_commands.add_parser("execute")
    execute.add_argument("--run-dir", required=True)
    execute.add_argument("--provider", choices=["stub", "command"], default="stub")
    execute.add_argument("--config")
    execute.add_argument("--concurrency", type=int, default=2)
    execute.add_argument("--max-jobs", type=int)
    execute.add_argument("--confirm", action="store_true")
    status = run_commands.add_parser("status")
    status.add_argument("--run-dir", required=True)
    return parser


def dispatch(args: argparse.Namespace) -> dict:
    if args.group == "plan" and args.plan_command == "validate":
        return plan_validation(args.file)
    if args.group == "generate" and args.generate_command == "stub":
        return generate_stub(args.plan, args.output_dir, args.confirm)
    if args.group == "generate" and args.generate_command == "command":
        return generate_command(args.config, args.prompt_file, args.output_path, args.input_path, args.confirm)
    if args.group == "qc" and args.qc_command == "technical":
        return technical_qc(args.folder, args.output, args.confirm, args.stage)
    if args.group == "qc" and args.qc_command == "visual" and args.visual_command == "command":
        return visual_command(
            args.config,
            args.input,
            args.stage,
            args.evidence,
            args.confirm,
            args.prompt_file,
            args.output_path,
        )
    if args.group == "upscale" and args.upscale_command == "pillow":
        return upscale_pillow(args.input, args.output_dir, args.scale, args.confirm)
    if args.group == "metadata" and args.metadata_command == "build":
        return build_metadata(args.folder, args.plan, args.csv, args.confirm)
    if args.group == "package" and args.package_command == "build":
        return build_package(
            args.folder,
            args.csv,
            args.output_dir,
            args.confirm,
            args.technical_report,
            args.visual_evidence,
        )
    if args.group == "run" and args.run_command == "export":
        return run_export(args.plan, args.output_dir, args.run_id, args.variants, args.confirm)
    if args.group == "run" and args.run_command == "execute":
        return run_execute(
            args.run_dir,
            args.provider,
            args.config,
            args.concurrency,
            args.max_jobs,
            args.confirm,
        )
    if args.group == "run" and args.run_command == "status":
        return run_status(args.run_dir)
    raise ToolError("unsupported command")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = dispatch(args)
    except AdapterError as exc:
        result = {
            "ok": False,
            "status": exc.status,
            "retry_class": exc.status,
            "error_code": exc.code,
            "error": str(exc),
        }
    except (ToolError, OSError, ValueError, TypeError, KeyError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result.get("ok", False):
        return 1
    if result.get("command") == "qc technical" and not result.get("passed", False):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
