#!/usr/bin/env python
"""Run manga_translator local mode for each first-level book directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".avif"}
DEFAULT_SAKURA_API_BASE = "http://127.0.0.1:8080/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONCURRENCY = 5
DEFAULT_BATCH_SIZE = 6


def configure_console_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


@dataclass(frozen=True)
class BatchOptions:
    config: Path | None = None
    result_dir_name: str = "result"
    force: bool = False
    verbose: bool = False
    use_gpu: bool = False
    disable_onnx_gpu: bool = False
    output_format: str | None = None
    batch_size: int | None = None
    attempts: int | None = None
    intra_book_concurrent: bool = False
    use_subprocess: bool = False
    memory_limit: int | None = None
    memory_percent: int | None = None
    batch_per_restart: int | None = None
    extra_local_args: tuple[str, ...] = ()


@dataclass
class JobState:
    index: int
    book_dir: Path
    total: int
    log_path: Path
    result_dir_name: str = "result"
    status: str = "queued"
    started_at: float | None = None
    ended_at: float | None = None
    returncode: int | None = None
    error: str | None = None

    @property
    def name(self) -> str:
        return self.book_dir.name


def natural_key(value: str) -> list[object]:
    parts = re.split(r"(\d+)", value.casefold())
    return [int(part) if part.isdigit() else part for part in parts]


def count_images(path: Path) -> int:
    return sum(
        1
        for item in path.rglob("*")
        if item.is_file() and item.suffix.casefold() in IMAGE_EXTENSIONS
    )


def count_source_images(book_dir: Path, result_dir_name: str = "result") -> int:
    total = 0
    for item in book_dir.iterdir():
        if not item.is_file() or item.suffix.casefold() not in IMAGE_EXTENSIONS:
            continue
        total += 1
    return total


def discover_book_dirs(root: Path) -> list[Path]:
    if not root.exists():
        raise FileNotFoundError(f"Root directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Root path is not a directory: {root}")

    books = [item for item in root.iterdir() if item.is_dir() and count_source_images(item) > 0]
    books.sort(key=lambda path: natural_key(path.name))
    return books


def sanitize_log_name(name: str, max_length: int = 80) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._")
    sanitized = re.sub(r"_+", "_", sanitized)
    return (sanitized or "book")[:max_length]


def result_dir_for(book_dir: Path, result_dir_name: str) -> Path:
    result_dir = Path(result_dir_name)
    if result_dir.is_absolute():
        return result_dir
    return book_dir / result_dir


def is_book_complete(book_dir: Path, result_dir_name: str) -> bool:
    total = count_source_images(book_dir, result_dir_name)
    if total == 0:
        return False

    result_dir = result_dir_for(book_dir, result_dir_name)
    if not result_dir.exists():
        return False

    return count_images(result_dir) >= total


def build_local_command(
    python_executable: Path,
    book_dir: Path,
    options: BatchOptions,
) -> list[str]:
    command = [
        str(python_executable),
        "-m",
        "manga_translator",
        "local",
        "-i",
        str(book_dir),
        "--save-to-source-dir",
        "--source-result-dir",
        options.result_dir_name,
        "--no-recursive",
    ]

    command.extend(["--config", str(options.config or resolve_default_config_path())])
    if options.force:
        command.append("--overwrite")
    else:
        command.append("--no-overwrite")
    if options.verbose:
        command.append("--verbose")
    if options.use_gpu:
        command.append("--use-gpu")
    if options.disable_onnx_gpu:
        command.append("--disable-onnx-gpu")
    if options.output_format:
        command.extend(["--format", options.output_format])
    if options.batch_size is not None:
        command.extend(["--batch-size", str(options.batch_size)])
    if options.attempts is not None:
        command.extend(["--attempts", str(options.attempts)])
    if options.intra_book_concurrent:
        command.append("--concurrent")
    if options.use_subprocess:
        command.append("--subprocess")
    if options.memory_limit is not None:
        command.extend(["--memory-limit", str(options.memory_limit)])
    if options.memory_percent is not None:
        command.extend(["--memory-percent", str(options.memory_percent)])
    if options.batch_per_restart is not None:
        command.extend(["--batch-per-restart", str(options.batch_per_restart)])

    command.extend(options.extra_local_args)
    return command


def resolve_default_config_path() -> Path:
    """Match ConfigService.get_user_config_path() in development mode."""
    return PROJECT_ROOT / "examples" / "config.json"


def resolve_default_env_path() -> Path:
    """Match ConfigService.env_path in development mode."""
    return PROJECT_ROOT / ".env"


def resolve_effective_config_path(config_path: Path | None) -> Path:
    return (config_path or resolve_default_config_path()).resolve()


def load_config_dict(config_path: Path | None) -> dict:
    path = resolve_effective_config_path(config_path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return data if isinstance(data, dict) else {}


def load_dotenv_values(path: Path | None = None) -> dict[str, str]:
    path = path or resolve_default_env_path()
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def resolve_sakura_api_base(env_path: Path | None = None) -> str:
    env_values = load_dotenv_values(env_path)
    base_url = os.getenv("SAKURA_API_BASE") or env_values.get("SAKURA_API_BASE") or DEFAULT_SAKURA_API_BASE
    base_url = base_url.strip().strip('"').strip("'").rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    return base_url


def preflight_checks(
    config_path: Path | None,
    env_path: Path | None = None,
    timeout_seconds: float = 3.0,
) -> tuple[bool, str]:
    try:
        config = load_config_dict(config_path)
    except Exception as exc:
        return False, f"Failed to read config for preflight: {exc}"

    translator = str((config.get("translator") or {}).get("translator") or "").strip().lower()
    if translator != "sakura":
        return True, "No Sakura endpoint preflight needed."

    base_url = resolve_sakura_api_base(env_path=env_path)
    models_url = f"{base_url}/models"
    request = Request(models_url, headers={"Authorization": "Bearer sk-114514"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", 200)
            if 200 <= int(status) < 500:
                return True, f"Sakura API endpoint reachable: {models_url}"
            return False, f"Sakura API endpoint returned HTTP {status}: {models_url}"
    except HTTPError as exc:
        if 400 <= exc.code < 500:
            return True, f"Sakura API endpoint reachable: {models_url} (HTTP {exc.code})"
        return False, f"Sakura API endpoint returned HTTP {exc.code}: {models_url}"
    except (OSError, URLError, TimeoutError) as exc:
        return False, f"Sakura API endpoint is not reachable: {models_url} ({exc})"


def elapsed_text(started_at: float | None, ended_at: float | None = None) -> str:
    if started_at is None:
        return "--:--"
    seconds = int((ended_at or time.monotonic()) - started_at)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


def render_table(states: Iterable[JobState], concurrency: int, started_at: float) -> str:
    rows = list(states)
    done = sum(1 for row in rows if row.status in {"pass", "skipped"})
    failed = sum(1 for row in rows if row.status in {"fail", "timeout"})
    running = sum(1 for row in rows if row.status == "running")
    lines = [
        (
            f"Books: {done}/{len(rows)} done | {running} running | "
            f"{failed} failed | concurrency={concurrency} | elapsed {elapsed_text(started_at)}"
        ),
        "#   Status     Pages       Time  Book",
    ]
    for row in rows:
        finished = count_images(result_dir_for(row.book_dir, row.result_dir_name)) if row.status == "running" else (
            row.total if row.status in {"pass", "skipped"} else count_images(result_dir_for(row.book_dir, row.result_dir_name))
        )
        pages = f"{min(finished, row.total)}/{row.total}" if row.total else "0/?"
        lines.append(
            f"{row.index:<3} {row.status:<10} {pages:>7} {elapsed_text(row.started_at, row.ended_at):>8}  "
            f"{truncate(row.name, 70)}"
        )
    return "\n".join(lines)


def run_job(
    state: JobState,
    python_executable: Path,
    options: BatchOptions,
    timeout: int | None,
    cwd: Path,
) -> JobState:
    state.status = "running"
    state.started_at = time.monotonic()
    command = build_local_command(python_executable, state.book_dir, options)

    with state.log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        log_file.write("Command: " + subprocess.list2cmdline(command) + "\n\n")
        log_file.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            state.returncode = process.wait(timeout=timeout)
            if state.returncode == 0:
                state.status = "pass"
            elif is_book_complete(state.book_dir, options.result_dir_name):
                state.status = "pass"
                state.error = f"Process exited with code {state.returncode}, but outputs are complete."
                log_file.write(
                    f"\nWarning: process exited with code {state.returncode}, "
                    "but outputs are complete; marking job as pass.\n"
                )
            else:
                state.status = "fail"
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            state.returncode = process.returncode
            state.status = "timeout"
            state.error = f"Timed out after {timeout} seconds"
            log_file.write(f"\nTimed out after {timeout} seconds\n")
        except Exception as exc:
            state.status = "fail"
            state.error = str(exc)
            log_file.write(f"\n{exc.__class__.__name__}: {exc}\n")

    state.ended_at = time.monotonic()
    return state


def make_log_path(log_dir: Path, index: int, book_name: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return log_dir / f"{index:04d}_{timestamp}_{sanitize_log_name(book_name)}.log"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process each first-level manga book directory with manga_translator local mode.",
    )
    parser.add_argument("--root", required=True, type=Path, help="Directory whose first-level folders are books.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Number of books to process at once. Default: {DEFAULT_CONCURRENCY}.",
    )
    parser.add_argument("--timeout", type=int, default=0, help="Per-book timeout in seconds. 0 disables timeout.")
    parser.add_argument("--config", type=Path, default=None, help="Config file passed to local mode.")
    parser.add_argument("--result-dir-name", default="result", help="Result folder name inside each book.")
    parser.add_argument("--logs-dir", type=Path, default=Path("result") / "batch_logs", help="Directory for per-book logs.")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip dependency checks before starting workers.")
    parser.add_argument("--preflight-timeout", type=float, default=3.0, help="Endpoint preflight timeout in seconds.")
    parser.add_argument("--force", action="store_true", help="Rerun completed books and overwrite outputs.")
    parser.add_argument("--dry-run", action="store_true", help="Print discovered jobs without running translation.")
    parser.add_argument("--verbose", action="store_true", help="Pass verbose logging to local mode.")
    parser.add_argument("--use-gpu", action="store_true", help="Pass --use-gpu to local mode.")
    parser.add_argument("--disable-onnx-gpu", action="store_true", help="Pass --disable-onnx-gpu to local mode.")
    parser.add_argument("--format", dest="output_format", default=None, help="Output format passed to local mode.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Batch size passed to local mode. Default: {DEFAULT_BATCH_SIZE}.",
    )
    parser.add_argument("--attempts", type=int, default=None, help="Retry attempts passed to local mode.")
    parser.add_argument(
        "--intra-book-concurrent",
        dest="intra_book_concurrent",
        action="store_true",
        default=True,
        help="Pass --concurrent to local mode so each book uses the internal pipeline. Enabled by default.",
    )
    parser.add_argument(
        "--no-intra-book-concurrent",
        dest="intra_book_concurrent",
        action="store_false",
        help="Disable the default per-book internal pipeline concurrency.",
    )
    parser.add_argument("--subprocess", dest="use_subprocess", action="store_true", help="Use local subprocess mode.")
    parser.add_argument("--memory-limit", type=int, default=None, help="Local subprocess memory limit in MB.")
    parser.add_argument("--memory-percent", type=int, default=None, help="Local subprocess memory percent.")
    parser.add_argument("--batch-per-restart", type=int, default=None, help="Local subprocess restart interval.")
    parser.add_argument("local_args", nargs=argparse.REMAINDER, help="Extra args after -- are passed to local mode.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    args = parse_args(argv)
    root = args.root.resolve()
    concurrency = max(1, args.concurrency)
    timeout = args.timeout if args.timeout and args.timeout > 0 else None
    logs_dir = args.logs_dir if args.logs_dir.is_absolute() else PROJECT_ROOT / args.logs_dir
    logs_dir = logs_dir.resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)
    effective_config_path = resolve_effective_config_path(args.config)
    env_path = resolve_default_env_path()

    extra_local_args = tuple(arg for arg in args.local_args if arg != "--")
    options = BatchOptions(
        config=effective_config_path,
        result_dir_name=args.result_dir_name,
        force=args.force,
        verbose=args.verbose,
        use_gpu=args.use_gpu,
        disable_onnx_gpu=args.disable_onnx_gpu,
        output_format=args.output_format,
        batch_size=args.batch_size,
        attempts=args.attempts,
        intra_book_concurrent=args.intra_book_concurrent,
        use_subprocess=args.use_subprocess,
        memory_limit=args.memory_limit,
        memory_percent=args.memory_percent,
        batch_per_restart=args.batch_per_restart,
        extra_local_args=extra_local_args,
    )

    books = discover_book_dirs(root)
    if not books:
        print(f"No first-level book directories with images found under: {root}")
        return 1

    print(f"Using config: {effective_config_path}")
    print(f"Using env: {env_path}")
    if not args.skip_preflight:
        ok, message = preflight_checks(effective_config_path, env_path=env_path, timeout_seconds=args.preflight_timeout)
        if not ok:
            print(f"Preflight failed: {message}")
            print("Start the Sakura LLM API endpoint, or rerun with --skip-preflight if this check is intentionally bypassed.")
            return 2
        print(f"Preflight ok: {message}")

    states = [
        JobState(
            index=index,
            book_dir=book,
            total=count_source_images(book, args.result_dir_name),
            log_path=make_log_path(logs_dir, index, book.name),
            result_dir_name=args.result_dir_name,
        )
        for index, book in enumerate(books, start=1)
    ]

    for state in states:
        if not args.force and is_book_complete(state.book_dir, args.result_dir_name):
            state.status = "skipped"
            state.started_at = time.monotonic()
            state.ended_at = state.started_at

    if args.dry_run:
        for state in states:
            print(f"{state.index:04d} {state.status:<8} {state.total:>4} {state.book_dir}")
        return 0

    pending = [state for state in states if state.status == "queued"]
    started_at = time.monotonic()
    print(render_table(states, concurrency, started_at))
    interactive = sys.stdout.isatty()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(run_job, state, Path(sys.executable), options, timeout, PROJECT_ROOT): state
            for state in pending
        }
        while futures:
            done, _ = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
            if done:
                for future in done:
                    futures.pop(future)
                    future.result()
            prefix = "\033[2J\033[H" if interactive else "\n"
            print(prefix + render_table(states, concurrency, started_at), end="\n")

    prefix = "\033[2J\033[H" if interactive else "\n"
    print(prefix + render_table(states, concurrency, started_at))

    failed = [state for state in states if state.status in {"fail", "timeout"}]
    if failed:
        print("\nFailed:")
        for state in failed:
            print(f"- {state.name}")
            print(f"  status: {state.status}")
            print(f"  log: {state.log_path}")
        return 1

    print(f"\nLogs: {logs_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
