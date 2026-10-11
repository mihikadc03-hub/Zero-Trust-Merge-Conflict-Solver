#!/usr/bin/env python3
"""
Zero-Trust Merge Conflict Resolver - Interactive CLI

Usage:
  python resolve_cli.py              # auto-detect all conflicted files via git
  python resolve_cli.py <file_path>  # resolve a specific file

Env:    HARNESS_URL  (default: http://localhost:8000)
"""

import os
import subprocess
import sys
import requests
from pathlib import Path

HARNESS_URL   = os.environ.get("HARNESS_URL", "http://localhost:8000")
CONTEXT_LINES = 50
WIDTH         = 62


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------
def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_file(path: Path, content: str):
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Conflict parser
# ---------------------------------------------------------------------------
def parse_conflict(content: str) -> dict | None:
    lines = content.splitlines(keepends=True)
    start_idx = sep_idx = end_idx = None
    for i, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        if stripped.startswith("<<<<<<< ") and start_idx is None:
            start_idx = i
        elif stripped == "=======" and start_idx is not None and sep_idx is None:
            sep_idx = i
        elif stripped.startswith(">>>>>>> ") and sep_idx is not None and end_idx is None:
            end_idx = i
            break

    if None in (start_idx, sep_idx, end_idx):
        return None

    before_start = max(0, start_idx - CONTEXT_LINES)
    after_end    = min(len(lines), end_idx + 1 + CONTEXT_LINES)

    return {
        "start_idx": start_idx,
        "end_idx":   end_idx,
        "before":    "".join(lines[before_start:start_idx]),
        "ours":      "".join(lines[start_idx + 1 : sep_idx]),
        "theirs":    "".join(lines[sep_idx + 1  : end_idx]),
        "after":     "".join(lines[end_idx + 1  : after_end]),
        "all_lines": lines,
    }


def apply_resolution(conflict: dict, resolved: str) -> str:
    """Replace the conflict block with resolved code.

    Strips lines the model copied from surrounding context to avoid duplication.
    """
    lines = conflict["all_lines"]

    before_lines = {l.strip() for l in conflict["before"].splitlines() if l.strip()}
    after_lines  = {l.strip() for l in conflict["after"].splitlines()  if l.strip()}

    resolved_lines = resolved.splitlines(keepends=True)
    resolved_lines = [ln if ln.endswith("\n") else ln + "\n" for ln in resolved_lines]

    cleaned = [
        ln for ln in resolved_lines
        if ln.strip() not in before_lines and ln.strip() not in after_lines
    ]
    if not cleaned:
        cleaned = resolved_lines

    new_lines = (
        lines[: conflict["start_idx"]]
        + cleaned
        + lines[conflict["end_idx"] + 1 :]
    )
    return "".join(new_lines)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def hr(char="-"):
    print(char * WIDTH)


def banner(text: str, char="="):
    pad = max(0, WIDTH - len(text) - 4) // 2
    print(char * WIDTH)
    print(f"{char}{' ' * pad}  {text}  {' ' * pad}{char}")
    print(char * WIDTH)


def show_conflict(conflict: dict, filename: str):
    banner(f"Conflict detected in {filename}")
    print()
    print("  OUR version (HEAD):")
    hr("-")
    print(conflict["ours"].rstrip())
    hr("-")
    print("  THEIR version (incoming):")
    hr("-")
    print(conflict["theirs"].rstrip())
    hr()


def show_resolution(resolved: str, attempt: int):
    print()
    banner(f"AI Resolution  (attempt {attempt})")
    print()
    print("\033[32m" + resolved + "\033[0m")
    print()
    hr()


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------
def check_health():
    try:
        r = requests.get(f"{HARNESS_URL}/health", timeout=5)
        r.raise_for_status()
        data = r.json()
        if not data.get("lm_studio_reachable"):
            print(
                "\n\033[31mHarness is up, but LM Studio is NOT reachable.\033[0m\n"
                "  -> Open LM Studio and start the Local Server (port 1234).\n"
            )
            sys.exit(1)
        print(f"  LM Studio OK  |  model: {data.get('model')}")
    except requests.exceptions.ConnectionError:
        print(
            "\n\033[31mCannot connect to harness.\033[0m Is it running?\n"
            "  Start it with:  uvicorn harness:app --port 8000\n"
            "  Working dir:    d:\\code_practice\\python code\\wec_hacktober\\api\n"
        )
        sys.exit(1)


def call_resolve(payload: dict) -> str:
    try:
        r = requests.post(f"{HARNESS_URL}/resolve", json=payload, timeout=180)
        if not r.ok:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"\n\033[31mHarness error {r.status_code}:\033[0m {detail}")
            sys.exit(1)
        return r.json()["resolved"]
    except requests.exceptions.ConnectionError:
        print(
            "\n\033[31mCannot connect to harness.\033[0m Is it running?\n"
            "  Start it with:  uvicorn harness:app --port 8000\n"
        )
        sys.exit(1)


def call_refine(payload: dict, previous: str, feedback: str) -> str:
    try:
        r = requests.post(
            f"{HARNESS_URL}/refine",
            json={**payload, "previous_resolution": previous, "feedback": feedback},
            timeout=180,
        )
        if not r.ok:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"\n\033[31mHarness error {r.status_code}:\033[0m {detail}")
            return previous
        return r.json()["resolved"]
    except requests.exceptions.RequestException as e:
        print(f"\033[31mHarness error:\033[0m {e}")
        return previous


# ---------------------------------------------------------------------------
# Git integration
# ---------------------------------------------------------------------------
def find_conflicted_files() -> list[Path]:
    """Use git to find all files currently in a conflicted (unmerged) state."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            capture_output=True, text=True, check=True
        )
        paths = [Path(p.strip()) for p in result.stdout.splitlines() if p.strip()]
        return [p for p in paths if p.exists()]
    except subprocess.CalledProcessError:
        return []
    except FileNotFoundError:
        print("\033[31mError: git not found in PATH.\033[0m")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Interactive resolution loop (single file — handles multiple conflicts)
# ---------------------------------------------------------------------------
def _resolve_one_conflict(file_path: Path, conflict_num: int, total: int | None) -> bool:
    """
    Resolve the FIRST conflict found in the file.
    Returns True if the conflict was accepted/written, False if skipped (R).
    Re-reads the file fresh each time so it always sees the current state.
    """
    content  = read_file(file_path)
    conflict = parse_conflict(content)
    if not conflict:
        return False

    label = f"Conflict {conflict_num}" + (f" of {total}" if total else "")
    show_conflict(conflict, f"{file_path.name}  [{label}]")

    base_payload = {
        "filename": file_path.name,
        "before":   conflict["before"],
        "ours":     conflict["ours"],
        "theirs":   conflict["theirs"],
        "after":    conflict["after"],
    }

    print("Sending to local model for resolution...")
    resolved = call_resolve(base_payload)

    attempt = 1
    while True:
        show_resolution(resolved, attempt)

        print("  [A]  Accept     - write this to the file")
        print("  [R]  Reject     - skip this conflict (resolve manually)")
        print("  [F]  Feedback   - tell the model what to change")
        hr("-")
        choice = input("  Your choice (A / R / F): ").strip().upper()

        if choice == "A":
            new_content = apply_resolution(conflict, resolved)
            write_file(file_path, new_content)
            print(f"\n\033[32mConflict {conflict_num} resolved in {file_path.name}\033[0m\n")
            return True

        elif choice == "R":
            print(f"\n\033[33mSkipped conflict {conflict_num} - leaving markers in place.\033[0m\n")
            return False

        elif choice == "F":
            print()
            feedback = input(
                "  Describe what to change\n"
                "  (e.g. 'keep both', 'prefer theirs', 'add error handling'):\n"
                "  > "
            ).strip()
            if not feedback:
                print("  No feedback entered. Try again.")
                continue
            print("\n  Refining with your feedback...")
            previous  = resolved
            resolved  = call_refine(base_payload, previous, feedback)
            attempt  += 1

        else:
            print("  Invalid choice. Enter A, R, or F.")


def count_conflicts(content: str) -> int:
    """Count how many conflict blocks exist in the file."""
    return content.count("<<<<<<< ")


def resolve_file(file_path: Path) -> None:
    """Resolve ALL conflicts in a file, one at a time, in order."""
    content = read_file(file_path)
    total   = count_conflicts(content)

    if total == 0:
        print(f"No merge conflicts found in {file_path.name}.")
        return

    print(f"\n\033[33mFound {total} conflict(s) in {file_path.name}\033[0m")

    print("\nChecking connection...")
    check_health()

    resolved_count = 0
    skipped_count  = 0

    for i in range(1, total + 1):
        # Re-read each iteration — file changes as we resolve
        current_content = read_file(file_path)
        if count_conflicts(current_content) == 0:
            break  # all done

        accepted = _resolve_one_conflict(file_path, i, total)
        if accepted:
            resolved_count += 1
        else:
            skipped_count += 1
            # Skip past this conflict so we don't loop on it
            # The skipped conflict marker stays in the file
            # We need to move to next conflict — but parse_conflict always
            # finds the first one. So if user skipped, we cannot advance
            # automatically. Warn and stop for this file.
            remaining = count_conflicts(read_file(file_path))
            if remaining > 0:
                cont = input(
                    f"  {remaining} conflict(s) remain. Continue to next? (Y/N): "
                ).strip().upper()
                if cont != "Y":
                    break

    # Final git add if anything was resolved
    if resolved_count > 0:
        remaining_after = count_conflicts(read_file(file_path))
        if remaining_after == 0:
            exit_code = os.system(f'git add "{file_path}"')
            if exit_code == 0:
                print(f"\033[32mAll conflicts resolved. File staged: git add {file_path.name}\033[0m")
        else:
            print(
                f"\033[33m{remaining_after} conflict(s) still unresolved in {file_path.name}. "
                f"Fix them manually, then run: git add {file_path.name}\033[0m"
            )

    print(f"\nSummary: {resolved_count} resolved, {skipped_count} skipped.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    # Mode 1: specific file passed as argument
    if len(sys.argv) == 2:
        file_path = Path(sys.argv[1])
        if not file_path.exists():
            print(f"Error: file not found: {file_path}")
            sys.exit(1)
        resolve_file(file_path)
        return

    if len(sys.argv) > 2:
        print("Usage: python resolve_cli.py [file_path]")
        sys.exit(1)

    # Mode 2: auto-detect all conflicted files via git
    print("\nScanning for conflicted files...")
    conflicted = find_conflicted_files()

    if not conflicted:
        print("\033[32mNo conflicted files found. Working tree is clean.\033[0m")
        sys.exit(0)

    print(f"\n\033[33mFound {len(conflicted)} conflicted file(s):\033[0m")
    for i, f in enumerate(conflicted, 1):
        print(f"  [{i}] {f}")

    print()
    hr()
    for file_path in conflicted:
        resolve_file(file_path)
        remaining = find_conflicted_files()
        if remaining:
            cont = input(f"\nContinue to next conflicted file? (Y/N): ").strip().upper()
            if cont != "Y":
                break


if __name__ == "__main__":
    main()