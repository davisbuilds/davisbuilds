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
  check_comments.py --rebaseline recompute every count; use it only in the commit
                                 that upgrades this file, so review sees the change
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
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

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
JS_FAMILY = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}
REGEX_PREV = set("(,=:[!&|?{};+-*%~^")
REGEX_WORDS = {
    "return",
    "typeof",
    "case",
    "in",
    "of",
    "delete",
    "void",
    "throw",
    "yield",
}
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
    r"|shellcheck\s|yamllint|zizmor:|renovate:|hadolint|syntax=|escape=|check=|yaml-language-server"
    r"|SPDX-|Copyright|\(c\)|-\*-|vim:|\+goose|migrate:|<reference\s"
    r"|(pin@)?(v\d+(\.\d+)*|\d+\.\d+(\.\d+)*)\s*$"
    r"|(todo)?ruleid:|(todo)?ok:|nosemgrep|AUTO-GENERATED|DO NOT EDIT|Code generated)",
    re.IGNORECASE,
)
EXCUSE_MARKER = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
EXCUSE_PHRASE = re.compile(
    r"\b(for now|quick (fix|hack)|temporary (fix|hack|workaround)|hacky)\b",
    re.IGNORECASE,
)
GO_DECL = re.compile(r"(func|type|var|const|package)\b")
HEREDOC = re.compile(r'<<-?\s*([\'"]?)([A-Za-z_][A-Za-z0-9_]*)\1')


Raw = tuple[int, int, str, bool, bool]
Block = dict[str, Any]


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(out)


def glob_regex(pattern: str) -> re.Pattern[str]:
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


def python_docs(text: str) -> Iterator[Raw]:
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
            end = first.end_lineno or first.lineno
            yield first.lineno, end, first.value.value.strip(), True, True


def python_comments(text: str) -> Iterator[Raw]:
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


def scan_hash_line(line: str, shell: bool, quote: str | None) -> tuple[int, str | None]:
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quote != "'" and (shell or quote == '"'):
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and i > 0 and line[i - 1] in " \t":
            return i, quote
        i += 1
    return -1, quote


def hash_comments(text: str, shell: bool, carry: bool = False) -> Iterator[Raw]:
    heredoc = None
    header = shell
    quote: str | None = None
    for row, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if heredoc:
            if stripped == heredoc:
                heredoc = None
            continue
        if row == 1 and stripped.startswith("#!"):
            continue
        if quote is None and stripped.startswith("#"):
            yield row, row, stripped, True, header
            continue
        header = False
        at, after = scan_hash_line(line, shell, quote)
        quote = after if carry else None
        if at != -1:
            yield row, row, line[at:].rstrip(), False, False
        if shell:
            match = HEREDOC.search(line if at == -1 else line[:at])
            if match:
                heredoc = match.group(2)


def sql_comments(text: str) -> Iterator[Raw]:
    for row, line in enumerate(text.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("--"):
            yield row, row, stripped, True, False


def skip_quoted(text: str, i: int) -> int:
    quote, j, n = text[i], i + 1, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == quote:
            return j + 1
        elif c == "\n":
            return j
        else:
            j += 1
    return n


def skip_template(text: str, i: int) -> int:
    j, n = i + 1, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == "`":
            return j + 1
        elif text.startswith("${", j):
            j = skip_expression(text, j + 2)
        else:
            j += 1
    return n


def skip_expression(text: str, j: int) -> int:
    depth, n = 0, len(text)
    while j < n:
        c = text[j]
        if c in "\"'":
            j = skip_quoted(text, j)
        elif c == "`":
            j = skip_template(text, j)
        elif c == "{":
            depth += 1
            j += 1
        elif c == "}":
            if depth == 0:
                return j + 1
            depth -= 1
            j += 1
        else:
            j += 1
    return n


def starts_regex(text: str, i: int) -> bool:
    k = i - 1
    while k >= 0 and text[k] in " \t\r\n":
        k -= 1
    if k < 0 or text[k] in REGEX_PREV:
        return True
    end = k + 1
    while k >= 0 and (text[k].isalnum() or text[k] == "_"):
        k -= 1
    return text[k + 1 : end] in REGEX_WORDS


def skip_regex(text: str, i: int) -> int:
    j, n, in_class = i + 1, len(text), False
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return j
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            return j + 1
        j += 1
    return n


def c_comments(
    text: str,
    line_comments: bool = True,
    markup: bool = False,
    html_only: bool = False,
    regex: bool = False,
) -> Iterator[Raw]:
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
        if ch in "\"'`" or (regex and ch == "/" and starts_regex(text, i)):
            if ch == "`":
                j = skip_template(text, i)
            elif ch == "/":
                j = skip_regex(text, i)
            else:
                j = skip_quoted(text, i)
            newlines = text.count("\n", i, j)
            if newlines:
                row += newlines
                line_start = text.rfind("\n", i, j) + 1
            i = j
            continue
        i += 1


def go_doc_rows(text: str, comments: list[Raw]) -> set[int]:
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


def raw_comments(path: str, text: str) -> list[Raw]:
    lang = language(path, text)
    if lang == "python":
        return sorted([*python_comments(text), *python_docs(text)])
    if lang == "shell":
        return list(hash_comments(text, shell=True, carry=True))
    if lang == "hash":
        return list(hash_comments(text, shell=False))
    if lang == "sql":
        return list(sql_comments(text))
    if lang == "css":
        return list(c_comments(text, line_comments=False))
    if lang == "markup":
        return list(c_comments(text, markup=True, regex=True))
    if lang == "html":
        return list(c_comments(text, markup=True, html_only=True))
    if lang in ("c", "go"):
        js = os.path.splitext(path)[1].lower() in JS_FAMILY
        found = list(c_comments(text, regex=js))
        if lang == "go":
            doc = go_doc_rows(text, found)
            found = [(*c[:4], c[4] or c[0] in doc) for c in found]
        return found
    return []


def blocks(path: str, text: str) -> list[Block]:
    out: list[Block] = []
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
            prev is not None
            and full
            and prev["kind"] == "line"
            and not directive
            and not prev["directive"]
            and start == prev["end"] + 1
            and start == end
        )
        if joinable and prev is not None:
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


def read_baseline(root: Path) -> dict[str, Any]:
    path = root / BASELINE
    if not path.exists():
        return {"version": 1, "exclude": [], "files": {}}
    data: dict[str, Any] = json.loads(path.read_text())
    data.setdefault("exclude", [])
    data.setdefault("files", {})
    return data


def write_baseline(root: Path, data: dict[str, Any]) -> None:
    data = {
        "version": 1,
        "exclude": sorted(data.get("exclude", [])),
        "files": dict(sorted((k, v) for k, v in data["files"].items() if v > 0)),
    }
    (root / BASELINE).write_text(json.dumps(data, indent=2) + "\n")


def tracked_files(root: Path, excludes: Sequence[str]) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8", "replace")
    patterns = [glob_regex(p) for p in (*DEFAULT_EXCLUDE, *excludes)]
    files = []
    for path in out.split("\0"):
        if path and path != BASELINE and not any(p.match(path) for p in patterns):
            files.append(path)
    return sorted(files)


def scan(root: Path, excludes: Sequence[str]) -> dict[str, list[Block]]:
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


def disallowed_lines(file_blocks: list[Block]) -> int:
    return sum(b["end"] - b["start"] + 1 for b in file_blocks if not b["allowed"])


def report_increase(
    path: str, current: int, allowed: int, file_blocks: list[Block]
) -> None:
    print(f"{path}: {current} disallowed comment lines (baseline {allowed})")
    shown = [b for b in file_blocks if not b["allowed"]][:MAX_SHOWN]
    for block in shown:
        print(f"  {path}:{block['start']}: {block['text'].splitlines()[0]}")


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "--check"
    if mode in ("-h", "--help"):
        print(__doc__)
        return 0
    if mode not in ("--check", "--update", "--init", "--rebaseline", "--list"):
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

    if mode == "--rebaseline":
        baseline["files"] = counts
        write_baseline(root, baseline)
        print(
            f"rebaselined {BASELINE}: {sum(counts.values())} disallowed comment "
            f"lines in {len(counts)} files"
        )
        return 0

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
