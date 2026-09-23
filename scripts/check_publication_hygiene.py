#!/usr/bin/env python3
"""Check staged/tracked text for accidental personal identifiers.

Exact private project names belong in a private workspace check, not public
source. This guard catches structural home paths and unexpected email addresses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME_RE = re.compile(r"/(?:Users|home)/([A-Za-z0-9._-]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_HOME = {
    "agent",
    "alice",
    "bob",
    "ci",
    "dev",
    "example",
    "home",
    "me",
    "parity",
    "runner",
    "someone",
    "test",
    "testuser",
    "user",
    "you",
}
ALLOWED_EMAILS = {
    "git@github.com",
    "noreply@anthropic.com",
    "digest@yourdomain.com",
    "you@email.com",
}
CONFIG_NAME = ".publication-hygiene.json"


def clean_git_env() -> dict[str, str]:
    # Git hooks export repository-local variables. A control repo must not
    # inherit the caller's index or object store.
    return {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }


def load_config(root: Path) -> tuple[set[str], set[str], dict[str, str]]:
    try:
        data = subprocess.check_output(
            ["git", "show", f":{CONFIG_NAME}"],
            cwd=root,
            env=clean_git_env(),
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        config = {}
    else:
        config = json.loads(data)
    unknown = set(config) - {"allowed_home_segments", "allowed_emails", "pinned_files"}
    if unknown:
        raise ValueError(f"unknown publication hygiene config keys: {sorted(unknown)}")
    homes = ALLOWED_HOME | {
        item.lower() for item in config.get("allowed_home_segments", [])
    }
    emails = ALLOWED_EMAILS | {
        item.lower() for item in config.get("allowed_emails", [])
    }
    return homes, emails, config.get("pinned_files", {})


def findings(text: str, homes: set[str], emails: set[str]) -> list[tuple[int, str]]:
    found = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for match in HOME_RE.finditer(line):
            segment = match.group(1).lower()
            if segment not in homes and not segment.startswith("."):
                found.append((line_number, "literal home path"))
        for match in EMAIL_RE.finditer(line):
            address = match.group(0).lower()
            domain = address.rsplit("@", 1)[1]
            if address in emails or domain in {
                "example.com",
                "example.org",
                "example.net",
            }:
                continue
            if domain.endswith((".example", ".invalid", ".test")):
                continue
            found.append((line_number, "unexpected email address"))
    return found


def tracked_files(root: Path) -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached"], cwd=root, env=clean_git_env()
    )
    return [os.fsdecode(item) for item in output.split(b"\0") if item]


def scan(root: Path) -> tuple[list[str], int]:
    homes, emails, pinned = load_config(root)
    paths = tracked_files(root)
    problems = []
    if not paths:
        return ["git reports no tracked files; refusing an empty scan"], 0
    stale = set(pinned) - set(paths)
    for path in sorted(stale):
        problems.append(f"{path}: pinned path is no longer tracked")
    checked = 0
    for relative in paths:
        try:
            data = subprocess.check_output(
                ["git", "show", f":{relative}"], cwd=root, env=clean_git_env()
            )
        except subprocess.CalledProcessError:
            problems.append(f"{relative}: staged file is unreadable")
            continue
        if relative in pinned:
            if hashlib.sha256(data).hexdigest() != pinned[relative]:
                problems.append(
                    f"{relative}: pinned historical file changed; review and repin"
                )
            continue
        if b"\0" in data[:4096]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        checked += 1
        matches = findings(text, homes, emails)
        for line_number, kind in matches[:5]:
            problems.append(f"{relative}:{line_number}: {kind}")
        if len(matches) > 5:
            problems.append(f"{relative}: {len(matches) - 5} further findings")
    return problems, checked


def verify_detector() -> None:
    homes, emails, _ = load_config(ROOT)
    bad = "/Users/" + "realperson/project\n" + "person@" + "company.tld\n"
    good = "/Users/example/project\nyou@example.com\n"
    assert [kind for _, kind in findings(bad, homes, emails)] == [
        "literal home path",
        "unexpected email address",
    ]
    assert not findings(good, homes, emails)
    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        subprocess.run(
            ["git", "init", "-q"], cwd=scratch, env=clean_git_env(), check=True
        )
        sample = scratch / "sample.txt"
        sample.write_text(good)
        subprocess.run(
            ["git", "add", "sample.txt"], cwd=scratch, env=clean_git_env(), check=True
        )
        assert scan(scratch)[0] == []
        sample.write_text(bad)
        # The staged blob is the commit candidate, even if the working copy
        # has already been edited again.
        assert scan(scratch)[0] == []
        subprocess.run(
            ["git", "add", "sample.txt"], cwd=scratch, env=clean_git_env(), check=True
        )
        assert any("literal home path" in item for item in scan(scratch)[0])
        sample.write_text(good)
        subprocess.run(
            ["git", "add", "sample.txt"], cwd=scratch, env=clean_git_env(), check=True
        )
        (scratch / CONFIG_NAME).write_text(
            json.dumps(
                {
                    "pinned_files": {
                        "sample.txt": hashlib.sha256(good.encode()).hexdigest()
                    }
                }
            )
        )
        subprocess.run(
            ["git", "add", CONFIG_NAME], cwd=scratch, env=clean_git_env(), check=True
        )
        assert scan(scratch)[0] == []
        sample.write_text(bad)
        subprocess.run(
            ["git", "add", "sample.txt"], cwd=scratch, env=clean_git_env(), check=True
        )
        assert any(
            "pinned historical file changed" in item for item in scan(scratch)[0]
        )


if __name__ == "__main__":
    verify_detector()
    problems, checked = scan(ROOT)
    if problems:
        print("Publication hygiene failed:\n" + "\n".join(problems), file=sys.stderr)
        raise SystemExit(1)
    print(f"Publication hygiene passed: {checked} tracked text files checked")
