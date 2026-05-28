#!/usr/bin/env python3
"""Sync shared modules from evm-wallet-scanner to downstream autopilot projects."""

import argparse
import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SHARED_FILES = ["audit.py", "state_machine.py", "policy.py"]

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent


def find_source_dir() -> Path:
    """Locate the evm-wallet-scanner source directory."""
    candidates = [
        PROJECT_ROOT / "src" / "evm_wallet_scanner",
        PROJECT_ROOT.parent / "evm-wallet-scanner" / "src" / "evm_wallet_scanner",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    # Clone to a temporary directory as a last resort.
    print("Fetching evm-wallet-scanner source...")
    tmp = tempfile.mkdtemp(prefix="evm-wallet-scanner-")
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "https://github.com/counterfactual5/evm-wallet-scanner.git",
            tmp,
        ],
        check=True,
        capture_output=True,
    )
    return Path(tmp) / "src" / "evm_wallet_scanner"


def get_target_projects() -> dict[str, Path]:
    """Determine which project(s) to sync/check."""
    if (PROJECT_ROOT / "src" / "evm_wallet_scanner").exists():
        return {
            "hyperliquid-autopilot": PROJECT_ROOT.parent / "hyperliquid-autopilot" / "src" / "hyperliquid_autopilot",
            "polymarket-autopilot": PROJECT_ROOT.parent / "polymarket-autopilot" / "src" / "polymarket_autopilot",
            "uniswap-autopilot": PROJECT_ROOT.parent / "uniswap-autopilot" / "src" / "uniswap_autopilot",
        }

    targets = {}
    for project_name, pkg_name in [
        ("hyperliquid-autopilot", "hyperliquid_autopilot"),
        ("polymarket-autopilot", "polymarket_autopilot"),
        ("uniswap-autopilot", "uniswap_autopilot"),
    ]:
        path = PROJECT_ROOT / "src" / pkg_name
        if path.exists():
            targets[project_name] = path
            break

    if not targets:
        raise RuntimeError(f"Cannot determine target project from {PROJECT_ROOT}")

    return targets


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def transformed_content(source_path: Path, project_name: str) -> str:
    content = read_text(source_path)
    return content.replace(
        '_DEFAULT_PROJECT = "evm-wallet-scanner"',
        f'_DEFAULT_PROJECT = "{project_name}"',
    )


def check_project(source_dir: Path, project_name: str, target_dir: Path) -> bool:
    ok = True
    for filename in SHARED_FILES:
        source_path = source_dir / filename
        dest_path = target_dir / filename
        expected = transformed_content(source_path, project_name)
        actual = read_text(dest_path)
        if expected == actual:
            continue
        ok = False
        print(f"DRIFT: {project_name}/{filename}")
        diff = difflib.unified_diff(
            actual.splitlines(keepends=True),
            expected.splitlines(keepends=True),
            fromfile=str(dest_path),
            tofile=f"{dest_path} (expected)",
        )
        sys.stdout.writelines(diff)
    return ok


def sync_project(source_dir: Path, project_name: str, target_dir: Path) -> None:
    for filename in SHARED_FILES:
        source_path = source_dir / filename
        dest_path = target_dir / filename
        content = transformed_content(source_path, project_name)
        write_text(dest_path, content)
        print(f"  -> {dest_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync shared modules to target projects")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Diff files and exit with code 1 if any differ",
    )
    args = parser.parse_args()

    source_dir = find_source_dir()
    target_projects = get_target_projects()

    all_ok = True
    for project_name, target_dir in target_projects.items():
        if not target_dir.exists():
            print(f"Warning: target directory {target_dir} does not exist, skipping")
            continue
        if args.check:
            if not check_project(source_dir, project_name, target_dir):
                all_ok = False
        else:
            print(f"Syncing to {project_name} ...")
            sync_project(source_dir, project_name, target_dir)

    if args.check:
        if all_ok:
            print("All shared files are in sync.")
            return 0
        else:
            print("\nShared file drift detected.")
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
