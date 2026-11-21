#!/usr/bin/env python3
"""Ratchet check that bans code comments unless they record a decision.

A comment is allowed when it starts with ``WHY:`` (a decision record: a
non-obvious reason the code cannot express), is a tool directive such as
``noqa`` or ``eslint-disable``, or is documentation syntax (Python docstrings,
``/** */``, ``///``, Sphinx ``#:``, Go doc comments, a shell script's header
block). An excuse marker (see ``EXCUSE_MARKER`` and ``EXCUSE_PHRASE``) makes
any comment or documentation block disallowed, even one marked ``WHY:``.
Every disallowed comment line is counted per file and frozen in
``.comment-baseline.json``; counts may only go down.

Usage:
  check_comments.py              fail when a file has more, or fewer, disallowed
                                 comment lines than its baseline entry
  check_comments.py --update     lower baseline counts to the current state
  check_comments.py --init       write the first baseline from the current state
  check_comments.py --list [P]   print comment blocks as JSON lines, optionally
                                 only under the given path prefixes; documentation
                                 appears only when it carries an excuse marker

The baseline's ``exclude`` list holds extra glob patterns (``**`` spans
directories) for files whose comments are data, such as test fixtures.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

BASELINE = ".comment-baseline.json"
MARKER = "WHY:"
MAX_SHOWN = 20

DEFAULT_EXCLUDE = (
    "**/node_modules/**",
    "**/vendor/**",
    "**/third_party/**",
    "**/dist/**",
    "**/build/**",
    "**/migrations/**",
    "**/*.min.js",
    "**/*.min.css",
    "**/*.generated.*",
    "**/*_pb2.py",
    "**/*.pb.go",
    "**/*.lock",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/yarn.lock",
    "**/Package.resolved",
)

PYTHON = {".py", ".pyi"}
SHELL = {".sh", ".bash", ".zsh"}
HASH = {".rb", ".r", ".tf", ".yml", ".yaml", ".toml", ".pl"}
HASH_NAMES = {"Dockerfile", "Makefile", "Justfile", "justfile", "Containerfile"}
C_FAMILY = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
    ".swift",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".c",
    ".h",
    ".cc",
    ".cpp",
    ".hpp",
    ".cs",
    ".dart",
    ".scss",
    ".less",
    ".m",
    ".mm",
}
MARKUP = {".vue", ".svelte", ".astro"}
HTML = {".html", ".htm"}
CSS = {".css"}
SQL = {".sql"}

DIRECTIVE = re.compile(
    r"(noqa|type:\s*ignore|pragma\b|pyright:|mypy:|pylint:|ruff:|isort:|fmt:\s*(off|on|skip)"
    r"|nosec|coverage:|eslint|prettier-ignore|biome-ignore|@ts-|tslint:|deno-lint"
    r"|istanbul\s+ignore|[cv]8\s+ignore|webpack[A-Z]|@vite-ignore|@jsx|@refresh|@flow"
    r"|@vitest-environment|@jest-environment|[#@]\s*sourceMappingURL|@license|@preserve"
    r"|swiftlint:|swift-format-ignore|MARK:|#?region\b|#?endregion\b|nolint|go:\w|\+build"
    r"|shellcheck\s|yamllint|hadolint|syntax=|escape=|check=|yaml-language-server"
    r"|SPDX-|Copyright|\(c\)|-\*-|vim:|\+goose|migrate:|<reference\s)",
    re.IGNORECASE,
)
EXCUSE_MARKER = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
EXCUSE_PHRASE = re.compile(
    r"\b(for now|quick (fix|hack)|temporary (fix|hack|workaround)|hacky)\b",
    re.IGNORECASE,
)
GO_DECL = re.compile(r"(func|type|var|const|package)\b")
HEREDOC = re.compile(r'<<-?\s*([\'"]?)([A-Za-z_][A-Za-z0-9_]*)\1')


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(out)


def glob_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def language(path: str, text: str) -> str | None:
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext in PYTHON:
        return "python"
    if ext in SHELL:
        return "shell"
    if ext in HASH or name in HASH_NAMES or name.startswith("Dockerfile."):
        return "hash"
    if ext in C_FAMILY:
        return "go" if ext == ".go" else "c"
    if ext in MARKUP:
        return "markup"
    if ext in HTML:
        return "html"
    if ext in CSS:
        return "css"
    if ext in SQL:
        return "sql"
    if not ext and text.startswith("#!"):
        first = text.split("\n", 1)[0]
        if "python" in first:
            return "python"
        if re.search(r"\b(ba|z)?sh\b", first):
            return "shell"
    return None


def comment_body(raw: str) -> str:
    body = raw.strip()
    for opener in ("//", "/*", "#", "--"):
        if body.startswith(opener):
            body = body[len(opener) :]
            break
    return body.lstrip(" *\t")


def is_directive(raw: str) -> bool:
    return bool(DIRECTIVE.match(comment_body(raw)))


def is_excuse(raw: str) -> bool:
    return bool(EXCUSE_MARKER.search(raw) or EXCUSE_PHRASE.search(raw))


def python_docs(text: str):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            yield first.lineno, first.end_lineno, first.value.value.strip(), True, True


def python_comments(text: str):
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        yield from hash_comments(text, shell=False)
        return
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        row, col = tok.start
        if row <= 2 and (tok.string.startswith("#!") or "coding" in tok.string):
            continue
        full = tok.line[:col].strip() == ""
        yield row, row, tok.string, full, tok.string.startswith("#:")


def hash_comments(text: str, shell: bool):
    heredoc = None
    header = shell
    for row, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if heredoc:
            if stripped == heredoc:
                heredoc = None
            continue
        if row == 1 and stripped.startswith("#!"):
            continue
        if stripped.startswith("#"):
            yield row, row, stripped, True, header
            continue
        header = False
        if shell:
            match = HEREDOC.search(line)
            if match:
                heredoc = match.group(2)


def sql_comments(text: str):
    for row, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("--"):
            yield row, row, stripped, True, False


def c_comments(
    text: str, line_comments: bool = True, markup: bool = False, html_only: bool = False
):
    i, row, n = 0, 1, len(text)
    line_start = 0
    while i < n:
        ch = text[i]
        if ch == "\n":
            row += 1
            line_start = i + 1
            i += 1
            continue
        if markup and text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            end = n if end == -1 else end + 3
            raw = text[i:end]
            full = text[line_start:i].strip() == ""
            start_row = row
            row += raw.count("\n")
            if "\n" in raw:
                line_start = i + raw.rfind("\n") + 1
            yield start_row, row, raw, full, False
            i = end
            continue
        if html_only:
            i += 1
            continue
        two = text[i : i + 2]
        if line_comments and two == "//":
            end = text.find("\n", i)
            end = n if end == -1 else end
            raw = text[i:end]
            doc = raw.startswith(("///", "//!"))
            yield row, row, raw.rstrip(), text[line_start:i].strip() == "", doc
            i = end
            continue
        if two == "/*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            raw = text[i:end]
            start_row = row
            full = text[line_start:i].strip() == ""
            row += raw.count("\n")
            if "\n" in raw:
                line_start = i + raw.rfind("\n") + 1
            is_doc = raw.startswith("/**") and not raw.startswith("/**/")
            yield start_row, row, raw, full, is_doc
            i = end
            continue
        if text.startswith('"""', i):
            end = text.find('"""', i + 3)
            end = n if end == -1 else end + 3
            row += text.count("\n", i, end)
            i = end
            continue
        if ch in "\"'`":
            j = i + 1
            while j < n:
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == ch:
                    j += 1
                    break
                if c == "\n":
                    if ch != "`":
                        break
                    row += 1
                    line_start = j + 1
                j += 1
            i = j
            continue
        i += 1


def go_doc_rows(text: str, comments):
    lines = text.split("\n")
    doc = set()
    for start, end, _raw, full, _doc in comments:
        if not full:
            continue
        nxt = end
        while nxt < len(lines) and lines[nxt].lstrip().startswith("//"):
            nxt += 1
        if nxt < len(lines) and GO_DECL.match(lines[nxt]):
            doc.add(start)
    return doc


def raw_comments(path: str, text: str):
    lang = language(path, text)
    if lang == "python":
        return sorted([*python_comments(text), *python_docs(text)])
    if lang == "shell":
        return list(hash_comments(text, shell=True))
    if lang == "hash":
        return list(hash_comments(text, shell=False))
    if lang == "sql":
        return list(sql_comments(text))
    if lang == "css":
        return list(c_comments(text, line_comments=False))
    if lang == "markup":
        return list(c_comments(text, markup=True))
    if lang == "html":
        return list(c_comments(text, markup=True, html_only=True))
    if lang in ("c", "go"):
        found = list(c_comments(text))
        if lang == "go":
            doc = go_doc_rows(text, found)
            found = [(*c[:4], c[4] or c[0] in doc) for c in found]
        return found
    return []


def blocks(path: str, text: str):
    out = []
    for start, end, raw, full, doc in raw_comments(path, text):
        directive = not doc and is_directive(raw)
        prev = out[-1] if out else None
        if doc:
            if prev and prev["kind"] == "doc" and start == prev["end"] + 1:
                prev["end"] = end
                prev["text"] += "\n" + raw.strip()
            else:
                out.append(
                    {
                        "path": path,
                        "start": start,
                        "end": end,
                        "kind": "doc",
                        "text": raw.strip(),
                        "directive": False,
                    }
                )
            continue
        joinable = (
            prev
            and full
            and prev["kind"] == "line"
            and not directive
            and not prev["directive"]
            and start == prev["end"] + 1
            and start == end
        )
        if joinable:
            prev["end"] = end
            prev["text"] += "\n" + raw.strip()
            continue
        kind = (
            "trailing"
            if not full
            else (
                "line"
                if start == end and not raw.lstrip().startswith("/*")
                else "block"
            )
        )
        out.append(
            {
                "path": path,
                "start": start,
                "end": end,
                "kind": kind,
                "text": raw.strip(),
                "directive": directive,
            }
        )
    for block in out:
        exempt = (
            block["kind"] == "doc"
            or block["directive"]
            or comment_body(block["text"]).startswith(MARKER)
        )
        del block["directive"]
        block["allowed"] = exempt and not is_excuse(block["text"])
    return [b for b in out if b["kind"] != "doc" or not b["allowed"]]


def read_baseline(root: Path) -> dict:
    path = root / BASELINE
    if not path.exists():
        return {"version": 1, "exclude": [], "files": {}}
    data = json.loads(path.read_text())
    data.setdefault("exclude", [])
    data.setdefault("files", {})
    return data


def write_baseline(root: Path, data: dict) -> None:
    data = {
        "version": 1,
        "exclude": sorted(data.get("exclude", [])),
        "files": dict(sorted((k, v) for k, v in data["files"].items() if v > 0)),
    }
    (root / BASELINE).write_text(json.dumps(data, indent=2) + "\n")


def tracked_files(root: Path, excludes) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8", "replace")
    patterns = [glob_regex(p) for p in (*DEFAULT_EXCLUDE, *excludes)]
    files = []
    for path in out.split("\0"):
        if path and path != BASELINE and not any(p.match(path) for p in patterns):
            files.append(path)
    return sorted(files)


def scan(root: Path, excludes) -> dict[str, list[dict]]:
    found = {}
    for path in tracked_files(root, excludes):
        full = root / path
        if not full.is_file():
            continue
        data = full.read_bytes()
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", "replace")
        if language(path, text) is None:
            continue
        file_blocks = blocks(path, text)
        if file_blocks:
            found[path] = file_blocks
    return found


def disallowed_lines(file_blocks) -> int:
    return sum(b["end"] - b["start"] + 1 for b in file_blocks if not b["allowed"])


def report_increase(path, current, allowed, file_blocks):
    print(f"{path}: {current} disallowed comment lines (baseline {allowed})")
    shown = [b for b in file_blocks if not b["allowed"]][:MAX_SHOWN]
    for block in shown:
        print(f"  {path}:{block['start']}: {block['text'].splitlines()[0]}")


def main(argv) -> int:
    mode = argv[0] if argv else "--check"
    if mode in ("-h", "--help"):
        print(__doc__)
        return 0
    if mode not in ("--check", "--update", "--init", "--list"):
        print(f"unknown argument {mode!r}; see --help", file=sys.stderr)
        return 2
    root = repo_root()
    baseline = read_baseline(root)
    found = scan(root, baseline["exclude"])

    if mode == "--list":
        prefixes = argv[1:]
        for path, file_blocks in found.items():
            if prefixes and not any(
                path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes
            ):
                continue
            for block in file_blocks:
                print(json.dumps(block))
        return 0

    counts = {path: disallowed_lines(b) for path, b in found.items()}
    counts = {path: n for path, n in counts.items() if n}

    if mode == "--init":
        if baseline["files"]:
            print(
                f"{BASELINE} already has entries; use --update to tighten it",
                file=sys.stderr,
            )
            return 2
        baseline["files"] = counts
        write_baseline(root, baseline)
        print(
            f"wrote {BASELINE}: {sum(counts.values())} disallowed comment lines "
            f"in {len(counts)} files"
        )
        return 0

    allowed = baseline["files"]
    increases = sorted(p for p, n in counts.items() if n > allowed.get(p, 0))
    shrunk = sorted(
        p for p in set(allowed) | set(counts) if counts.get(p, 0) < allowed.get(p, 0)
    )

    for path in increases:
        report_increase(path, counts[path], allowed.get(path, 0), found[path])

    if mode == "--update":
        for path in shrunk:
            allowed[path] = counts.get(path, 0)
        write_baseline(root, baseline)
        print(f"tightened {len(shrunk)} baseline entries")
    else:
        for path in shrunk:
            print(
                f"{path}: {counts.get(path, 0)} disallowed comment lines, "
                f"baseline still {allowed[path]}"
            )
        if shrunk:
            print(
                "Comments were removed: run scripts/check_comments.py --update "
                f"to tighten {BASELINE}."
            )

    if increases:
        print(
            f'New comments must start with "{MARKER}" and record a non-obvious '
            "decision the code cannot express; delete comments that restate the "
            "code, excuse a shortcut, or are stale."
        )
    return 1 if increases or (shrunk and mode == "--check") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
