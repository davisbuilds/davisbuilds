#!/usr/bin/env python3
"""Render the profile README's images in davisbuilds.net's drawing-sheet style.

Writes four SVGs to profile/: a masthead and a GitHub readout, each in a light
and a dark variant for the README's <picture> elements.

The readout reads the same public contribution calendar GitHub draws on the
profile page, so its totals, streaks, and best day agree with the graph below
it. (The GraphQL contributionsCollection API reports fewer contributions than
that calendar for this account, so it is not used.) Languages and repository
counts come from public, non-fork repositories through the REST API.

GitHub strips CSS and web fonts from READMEs, so each SVG embeds its own fonts,
instanced from the site's variable fonts and subset to the characters it uses.

Needs fontTools and brotli: uv run --with fonttools --with brotli python ...
A calendar that fails its parse control exits 1 without writing anything.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import io
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parents[1]
FONTS = ROOT / "assets" / "fonts"
OUT = ROOT / "profile"
USER = "davisbuilds"
CALENDAR_URL = f"https://github.com/users/{USER}/contributions"
API = "https://api.github.com"
# Markup and build files say little about what the code is written in.
IGNORED_LANGUAGES = {"HTML", "CSS", "Shell", "Dockerfile", "Makefile"}
WIDTH = 840

# Tokens from davisbuilds.net src/styles/tokens.css.
THEMES = {
    "light": {
        "paper": "#e9ece8", "sheet": "#f4f5f2", "ink": "#16212b", "muted": "#5a6772",
        "rule": "#c2cbd2", "grid": "rgba(58,98,138,0.07)", "signal": "#b7460a",
    },
    "dark": {
        "paper": "#0d1317", "sheet": "#121a1f", "ink": "#dbe3e7", "muted": "#8796a0",
        "rule": "#2a3740", "grid": "rgba(140,180,210,0.05)", "signal": "#ff7a33",
    },
}

# Font instances: (family name in the SVG, source file, axis location).
FACES = {
    "display": ("Archivo Display", "archivo.woff2", {"wght": 820, "wdth": 112}),
    "sub": ("Archivo Sub", "archivo.woff2", {"wght": 760, "wdth": 108}),
    "mono": ("JetBrains Mono", "jetbrains-mono.woff2", {"wght": 450}),
    "mono-bold": ("JetBrains Mono Bold", "jetbrains-mono.woff2", {"wght": 700}),
}


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


@dataclass
class Day:
    date: dt.date
    count: int
    level: int


@dataclass
class Calendar:
    days: list[Day]
    total: int


def fetch(url: str, token: str | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"{USER}-profile-renderer"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def parse_calendar(page: str) -> Calendar:
    """Parse GitHub's contribution calendar fragment.

    Each cell carries data-date, data-level (GitHub's own 0-4 shading) and an
    id that a <tool-tip> names with the day's count. The page also states the
    year's total; a parse whose days don't sum to it is rejected rather than
    rendered, so a markup change can't quietly produce wrong numbers.
    """
    cells = re.findall(
        r'<td[^>]*?data-date="(\d{4}-\d{2}-\d{2})"[^>]*?id="([^"]+)"[^>]*?data-level="(\d)"', page
    )
    tips = dict(re.findall(r'<tool-tip[^>]*?for="([^"]+)"[^>]*>([^<]*)</tool-tip>', page))
    days = []
    for date, cell_id, level in cells:
        match = re.match(r"(\d[\d,]*|No) contributions?", tips.get(cell_id, ""))
        if not match:
            raise ValueError(f"no count for {date}")
        count = 0 if match.group(1) == "No" else int(match.group(1).replace(",", ""))
        days.append(Day(dt.date.fromisoformat(date), count, int(level)))
    days.sort(key=lambda d: d.date)
    stated = re.search(r"([\d,]+)\s+contributions?\s+in the last year", page)
    if not stated:
        raise ValueError("calendar states no yearly total")
    total = int(stated.group(1).replace(",", ""))
    if len(days) < 365:
        raise ValueError(f"calendar has {len(days)} days, expected a year")
    if sum(d.count for d in days) != total:
        raise ValueError(f"days sum to {sum(d.count for d in days)}, calendar states {total}")
    return Calendar(days, total)


@dataclass
class Streak:
    length: int
    start: dt.date | None
    end: dt.date | None


def longest_streak(days: list[Day]) -> Streak:
    best, run, start = Streak(0, None, None), 0, None
    for day in days:
        if day.count:
            run, start = run + 1, start or day.date
            if run > best.length:
                best = Streak(run, start, day.date)
        else:
            run, start = 0, None
    return best


def current_streak(days: list[Day]) -> Streak:
    # Today may simply not have contributions yet, so it doesn't break a streak.
    tail = days[:-1] if days and days[-1].count == 0 else days
    run = 0
    for day in reversed(tail):
        if not day.count:
            break
        run += 1
    if not run:
        return Streak(0, None, None)
    return Streak(run, tail[-run].date, tail[-1].date)


@dataclass
class Repos:
    public: int
    stars: int
    followers: int
    languages: list[tuple[str, float]]


def read_repos(token: str | None) -> Repos:
    user = json.loads(fetch(f"{API}/users/{USER}", token))
    repos = json.loads(fetch(f"{API}/users/{USER}/repos?type=owner&per_page=100", token))
    own = [r for r in repos if not r["fork"] and not r["archived"]]
    totals: dict[str, int] = {}
    for repo in own:
        for name, size in json.loads(fetch(repo["languages_url"], token)).items():
            if name not in IGNORED_LANGUAGES:
                totals[name] = totals.get(name, 0) + size
    whole = sum(totals.values()) or 1
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    top = [(name, size / whole) for name, size in ranked[:6]]
    rest = 1 - sum(share for _, share in top)
    if rest > 0.0005:
        top.append(("Other", rest))
    return Repos(
        public=len(own),
        stars=sum(r["stargazers_count"] for r in own),
        followers=user["followers"],
        languages=top,
    )


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------


def font_face(face: str, text: str) -> str:
    family, source, location = FACES[face]
    font = TTFont(FONTS / source)
    font = instancer.instantiateVariableFont(font, location)
    options = subset.Options()
    options.flavor = "woff2"
    options.layout_features = ["kern", "liga", "tnum"]
    options.name_IDs = []
    subsetter = subset.Subsetter(options)
    subsetter.populate(text=text + " ")
    subsetter.subset(font)
    buffer = io.BytesIO()
    font.flavor = "woff2"
    font.save(buffer)
    data = base64.b64encode(buffer.getvalue()).decode()
    return f"@font-face{{font-family:'{family}';src:url(data:font/woff2;base64,{data}) format('woff2');}}"


class Svg:
    """Collects elements and the text each font must cover."""

    def __init__(self, height: int, theme: dict[str, str], title: str):
        self.height, self.t, self.title = height, theme, title
        self.parts: list[str] = []
        self.text: dict[str, str] = {face: "" for face in FACES}

    def add(self, markup: str) -> None:
        self.parts.append(markup)

    def label(self, x: float, y: float, text: str, anchor: str = "start", fill: str | None = None,
              size: float = 10.5, upper: bool = True) -> None:
        shown = text.upper() if upper else text
        spacing = ' letter-spacing="0.08em"' if upper else ""
        self.add(self.txt(x, y, shown, "mono", size, fill or self.t["muted"], anchor, spacing))

    def txt(self, x: float, y: float, content: str | list[tuple[str, str | None]], face: str, size: float,
            fill: str, anchor: str = "start", extra: str = "") -> str:
        runs = [(content, None)] if isinstance(content, str) else content
        family = FACES[face][0]
        body = ""
        for run, color in runs:
            self.text[face] += run
            escaped = html.escape(run, quote=False)
            body += f'<tspan fill="{color}">{escaped}</tspan>' if color else escaped
        return (f'<text x="{x}" y="{y}" font-family="\'{family}\'" font-size="{size}" fill="{fill}" '
                f'text-anchor="{anchor}"{extra}>{body}</text>')

    def render(self) -> str:
        faces = "".join(font_face(face, text) for face, text in self.text.items() if text)
        t = self.t
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{self.height}" '
            f'viewBox="0 0 {WIDTH} {self.height}" role="img" aria-label="{html.escape(self.title)}">'
            f"<title>{html.escape(self.title)}</title><style>{faces}</style>"
            f'<defs><pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse">'
            f'<path d="M24 0H0V24" fill="none" stroke="{t["grid"]}" stroke-width="1"/></pattern></defs>'
            f'<rect width="{WIDTH}" height="{self.height}" fill="{t["sheet"]}"/>'
            f'<rect width="{WIDTH}" height="{self.height}" fill="url(#grid)"/>'
            + "".join(self.parts)
            + f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{self.height - 1}" fill="none" '
            f'stroke="{t["ink"]}" stroke-width="1"/></svg>\n'
        )


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def masthead(theme: dict[str, str]) -> str:
    t = theme
    svg = Svg(244, t, "Hi, I'm Davis. Systems engineer who got obsessed with building.")
    svg.add(f'<line x1="0" y1="34.5" x2="{WIDTH}" y2="34.5" stroke="{t["ink"]}"/>')
    svg.label(20, 22, "Spec. 001 · Operator")
    svg.label(WIDTH - 20, 22, "Sheet 1 of 1 · davisbuilds.net", anchor="end")
    svg.add(svg.txt(18, 112, [("Hi, I'm ", None), ("Davis", t["signal"])], "display", 62, t["ink"],
                    extra=' letter-spacing="-0.025em"'))
    svg.add(svg.txt(20, 152, [("Systems engineer who got obsessed with ", None), ("building", t["signal"]),
                              (".", None)], "sub", 23, t["ink"], extra=' letter-spacing="-0.01em"'))
    top = 184
    svg.add(f'<line x1="0" y1="{top + 0.5}" x2="{WIDTH}" y2="{top + 0.5}" stroke="{t["ink"]}"/>')
    cells = [
        ("Title", "davisbuilds.net"),
        ("Drawn", "D. Gee"),
        ("Role", "Senior AI/ML Engineer"),
        ("Focus", "AI infrastructure · context engineering"),
    ]
    widths = [170, 110, 210, 350]
    x = 0
    for (name, value), width in zip(cells, widths):
        if x:
            svg.add(f'<line x1="{x + 0.5}" y1="{top}" x2="{x + 0.5}" y2="244" stroke="{t["rule"]}"/>')
        svg.label(x + 14, top + 22, name, size=10)
        svg.add(svg.txt(x + 14, top + 44, value, "mono", 12.5, t["ink"]))
        x += width
    return svg.render()


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def fmt_range(streak: Streak) -> str:
    if not streak.start or not streak.end:
        return "no streak"
    def short(d: dt.date) -> str:
        return f"{d:%b} {d.day}, {d.year}"
    return f"{short(streak.start)} – {short(streak.end)}"


def readout(theme: dict[str, str], cal: Calendar, repos: Repos, read: dt.date) -> str:
    t = theme
    best_day = max(cal.days, key=lambda d: d.count)
    longest, current = longest_streak(cal.days), current_streak(cal.days)
    height = 452
    svg = Svg(height, t, f"GitHub readout: {cal.total:,} contributions in the last year, "
              f"longest streak {longest.length} days, best day {best_day.count}.")
    svg.add(f'<line x1="0" y1="34.5" x2="{WIDTH}" y2="34.5" stroke="{t["ink"]}"/>')
    svg.label(20, 22, "Readout · GitHub")
    svg.label(WIDTH - 20, 22, f"Read {read.isoformat()}", anchor="end")

    # Tiles
    tiles = [
        ("Contributions · last year", f"{cal.total:,}", "as shown on this profile", t["ink"]),
        ("Longest streak", f"{longest.length} days", fmt_range(longest), t["ink"]),
        ("Current streak", f"{current.length} days",
         f"since {current.start:%b} {current.start.day}" if current.start else "none running", t["ink"]),
        ("Best day", f"{best_day.count}", f"{best_day.date:%b} {best_day.date.day}, {best_day.date.year}",
         t["signal"]),
    ]
    tile_w = WIDTH / 4
    for i, (name, value, note, color) in enumerate(tiles):
        x = i * tile_w
        if i:
            svg.add(f'<line x1="{x + 0.5}" y1="35" x2="{x + 0.5}" y2="128" stroke="{t["rule"]}"/>')
        svg.label(x + 14, 58, name, size=10)
        svg.add(svg.txt(x + 14, 94, value, "mono-bold", 28, color, extra=' letter-spacing="-0.03em"'))
        svg.label(x + 14, 116, note, size=10.5, upper=False)
    svg.add(f'<line x1="0" y1="128.5" x2="{WIDTH}" y2="128.5" stroke="{t["ink"]}"/>')

    # Calendar, GitHub's own levels and week layout
    svg.label(20, 154, "Contributions · trailing year · GitHub's shading")
    pitch, cell, x0, y0 = 14, 11, 48, 182
    first = cal.days[0].date
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)  # weeks start on Sunday
    fills = ["", "0.3", "0.52", "0.76", "1"]
    month_seen = set()
    for day in cal.days:
        col = (day.date - start).days // 7
        row = (day.date.weekday() + 1) % 7
        x, y = x0 + col * pitch, y0 + row * pitch
        if day.level:
            svg.add(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="{t["signal"]}" '
                    f'fill-opacity="{fills[day.level]}"/>')
        else:
            svg.add(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="{t["rule"]}" '
                    f'fill-opacity="0.45"/>')
        key = (day.date.year, day.date.month)
        if day.date.day <= 7 and row == 0 and key not in month_seen:
            month_seen.add(key)
            svg.label(x, y0 - 8, f"{day.date:%b}", size=9.5, upper=False)
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        svg.label(20, y0 + row * pitch + 9.5, name, size=9.5, upper=False)
    legend_x = WIDTH - 58 - 5 * pitch
    svg.label(legend_x - 8, 300, "Less", anchor="end", size=9.5, upper=False)
    for level in range(5):
        x = legend_x + level * pitch
        if level:
            svg.add(f'<rect x="{x}" y="291" width="{cell}" height="{cell}" fill="{t["signal"]}" '
                    f'fill-opacity="{fills[level]}"/>')
        else:
            svg.add(f'<rect x="{x}" y="291" width="{cell}" height="{cell}" fill="{t["rule"]}" '
                    f'fill-opacity="0.45"/>')
    svg.label(legend_x + 5 * pitch + 4, 300, "More", size=9.5, upper=False)

    # Languages
    top = 318
    svg.add(f'<line x1="20" y1="{top + 0.5}" x2="{WIDTH - 20}" y2="{top + 0.5}" stroke="{t["rule"]}" '
            f'stroke-dasharray="3 3"/>')
    svg.label(20, top + 24, "Languages · public repos, by bytes")
    svg.label(WIDTH - 20, top + 24,
              f"{plural(repos.public, 'public repo')} · {plural(repos.stars, 'star')} · "
              f"{plural(repos.followers, 'follower')}",
              anchor="end", upper=False)
    bar_x, bar_w, bar_y = 20, WIDTH - 40, top + 36
    shades = ["1", "0.72", "0.52", "0.36", "0.24", "0.15", "0.08"]
    x = bar_x
    for i, (name, share) in enumerate(repos.languages):
        w = bar_w * share
        color = t["signal"] if i == 0 else t["ink"]
        opacity = "1" if i == 0 else shades[i]
        svg.add(f'<rect x="{x:.2f}" y="{bar_y}" width="{max(w, 1):.2f}" height="10" fill="{color}" '
                f'fill-opacity="{opacity}"/>')
        x += w
    col_w = bar_w / 4
    for i, (name, share) in enumerate(repos.languages):
        cx, cy = bar_x + (i % 4) * col_w, bar_y + 30 + (i // 4) * 18
        color = t["signal"] if i == 0 else t["ink"]
        opacity = "1" if i == 0 else shades[i]
        svg.add(f'<rect x="{cx}" y="{cy - 8}" width="8" height="8" fill="{color}" fill-opacity="{opacity}"/>')
        svg.add(svg.txt(cx + 14, cy, f"{name} {share * 100:.1f}%", "mono", 11, t["ink"]))

    # Provenance
    svg.add(f'<line x1="0" y1="{height - 34.5}" x2="{WIDTH}" y2="{height - 34.5}" stroke="{t["ink"]}"/>')
    svg.add(svg.txt(20, height - 13, [
        ("SRC ", t["ink"]), ("GitHub contribution calendar + REST API · ", None),
        ("READ ", t["ink"]), (f"{read.isoformat()} · ", None),
        ("SCOPE ", t["ink"]), ("all contributions GitHub counts; languages from public repos", None),
    ], "mono", 10, t["muted"]))
    return svg.render()


# --------------------------------------------------------------------------
# Controls and entry point
# --------------------------------------------------------------------------


def verify_detectors() -> None:
    """Known-positive and known-negative cases for the parser and streaks."""
    def cell(date: str, n: int, level: int) -> str:
        tip = "No contributions" if n == 0 else f"{n} contribution{'s' if n != 1 else ''}"
        return (f'<td data-date="{date}" id="c-{date}" data-level="{level}"></td>'
                f'<tool-tip for="c-{date}">{tip} on that day.</tool-tip>')
    start = dt.date(2025, 1, 1)
    counts = [1] * 365
    counts[10] = 0  # breaks the first run after 10 days
    counts[200] = 0
    counts[-1] = 0  # today, still empty: the current streak continues
    page = "".join(cell((start + dt.timedelta(days=i)).isoformat(), n, 1 if n else 0)
                   for i, n in enumerate(counts))
    total = sum(counts)
    cal = parse_calendar(page + f"<h2>{total:,} contributions in the last year</h2>")
    assert longest_streak(cal.days).length == 189, longest_streak(cal.days)
    assert current_streak(cal.days).length == 163, current_streak(cal.days)
    try:
        parse_calendar(page + f"<h2>{total + 1:,} contributions in the last year</h2>")
    except ValueError:
        pass
    else:
        raise AssertionError("a calendar whose days don't match its stated total was accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--calendar", type=Path, help="read a saved calendar fragment instead of fetching")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    verify_detectors()
    page = args.calendar.read_text() if args.calendar else fetch(CALENDAR_URL).decode()
    try:
        cal = parse_calendar(page)
    except ValueError as error:
        print(f"Calendar rejected, nothing written: {error}", file=sys.stderr)
        return 1
    repos = read_repos(os.environ.get("GITHUB_TOKEN"))
    read = dt.datetime.now(dt.timezone.utc).date()

    args.out.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        (args.out / f"masthead-{name}.svg").write_text(masthead(theme))
        (args.out / f"readout-{name}.svg").write_text(readout(theme, cal, repos, read))
    longest = longest_streak(cal.days)
    print(f"Rendered {args.out}: {cal.total:,} contributions, longest streak {longest.length} days, "
          f"best day {max(d.count for d in cal.days)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
