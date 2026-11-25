#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Wave 6, phase 4.1: rename "pack" to "agent" across Mendari, then regenerate what is derived.

    python3 codemod.py REPO              # rename, move, format, regenerate, report
    python3 codemod.py REPO --dry-run    # every rule's counts and the gates; nothing is written
    python3 codemod.py REPO --residue    # only the residue report, on the tree as it is

Specification: `wave6-inventory.md` section 4 (4.1 to 4.13) and `wave6-decisions.md` (N1 to N6,
O2), which wins where they differ (G6: `site-codes/1` and `ehr-declarations/1` stay).

The tree must be clean (`git status --porcelain` empty). The rename is computed in memory, then
checked (the collision gates); nothing is written unless every gate passes. It never commits.
Python 3.12 or later (it reads f-strings' positions); `uv` in the repository for ruff and the
generators; Node with the Hub's `typescript` (`hub/node_modules`) for the JS/TS gate (skipped,
with a warning, when absent).

Rules, in order; each prints the files and occurrences it changed:

  S   the agent's service first (4.10): `agent_version` -> `service_version`, the service's
      `agent_id` -> `service_id`, `AGENT_ID_PATTERN` -> `SERVICE_ID_PATTERN`, the kit's
      `Case.agent` -> `agent_id`, the calibration file's and report's `agent` -> `service`, and a
      local `agent` that means the service -> `service`
  C   other names an `agent` would meet: a local `agent` that is a folder, an id, a Decision, a
      module or a listing gets a name of its own first (or the `pack` beside it does)
  E   event kinds and chain subjects (4.7): `pack_version.*` -> `store_version.*`, the subject
      `pack_version:` -> `store_version:`, `_pack_subject` -> `_version_subject`
  F   FHIR (4.8): `decision-pack` -> `agent-version` (CodeSystem, NamingSystem)
  V   formats (4.12, O2): the six that restart at `/1`, and their `V2` names -> `V1`
  M   modules and names the table names (4.1, 4.2, 4.11, 4.13): `pack_tests` ->
      `behaviour_tests`, `pack.js` -> `manifest.js`, `_PackLoader` -> `_ManifestLoader`,
      `packless` -> `agentless`, `repack` -> `rearchive`, "re-packed" -> "re-zipped"
  G   every other "pack" word part in code, keys, strings, paths, columns, CSS classes:
      `pack` -> `agent`, `packs` -> `agents`, in any case, with "a" -> "an" before it
  P   prose (comments, docstrings, Markdown text): a bare "pack" or "packs" only in a sentence
      that says nothing of agents, old names or history; anything else is left and listed
  W   the four vocabulary tests: names only (their strings are the rule's own test data), and
      the allow-list entries the rename leaves with nothing to allow
  R   files (4.1): `git mv`
  X   ruff: imports sorted, formatted; prose lines over 100 characters re-wrapped
  D   derived files, each by the repository's own generator
  Q   the residue: `(?i)pack(?!ag|et)` once 4.13's words are taken out, code and prose apart
"""

from __future__ import annotations

import argparse
import ast
import bisect
import collections
import fnmatch
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info < (3, 12):
    sys.exit("codemod.py needs Python 3.12 or later (f-string positions)")

# -- what is read -------------------------------------------------------------------------------

# Never touched: history, plans, licences, others' code, lockfiles, a file pinned by its hash,
# and the two root files Phase 5A rewrites (the changelog's 0.1.0, O5; CLAUDE.md's rules).
EXCLUDED_DIRS = (
    "notes/",
    "tasks/",
    "licenses/",
    ".claude/",
    "src/mendari/dashboard/vendor/",
    "tests/browser/vendor/",
)
EXCLUDED_FILES = {
    "CHANGELOG.md",
    "CLAUDE.md",
    "uv.lock",
    "hub/package-lock.json",
    "sandbox/inferno/scripted/Gemfile.lock",
    "scripts/fhir_packages.lock",
    "docs/grading/method-1.0.md",  # pinned by the SHA-256 of its bytes (`evals/method.py`)
    "src/mendari/standards/fhir_r4_structure.json",  # HL7's schema, pinned by its digest
    "tests/fixtures/fhir/r4-search-parameters.json",  # HL7's
}
BINARY_SUFFIXES = {
    ".png", ".ico", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf", ".otf",
    ".pdf", ".zip", ".gz", ".tgz", ".db", ".sqlite", ".pyc", ".mendari",
}
# Read for names only: their strings and prose are the old word's own test data (Phase 5C
# rewrites them as one rule).
NAMES_ONLY = {
    "tests/test_vocabulary.py",
    "tests/test_vocabulary_docs.py",
    "tests/test_vocabulary_frontends.py",
    "tests/test_cli_vocabulary.py",
}
# Never renamed, wherever written: the manifest's former name (only a sentence on it and a check
# that nothing asks for it hold it), and a pattern that finds the old word.
PROTECTED = ("pack.yaml", r"\bpacks?\b")

# -- reporting ----------------------------------------------------------------------------------


@dataclass
class Report:
    counts: dict[str, collections.Counter[str]] = field(
        default_factory=lambda: collections.defaultdict(collections.Counter)
    )
    titles: dict[str, str] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    doubts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def rule(self, key: str, title: str) -> None:
        if key not in self.titles:
            self.titles[key] = title
            self.order.append(key)

    def add(self, key: str, path: str, n: int = 1) -> None:
        if n:
            self.counts[key][path] += n

    def moved(self, moves: dict[str, str]) -> None:
        """Every file named by its path after rule R (R's own lines keep both)."""
        for key, files in self.counts.items():
            if key != "R1":
                self.counts[key] = collections.Counter({moves.get(p, p): n for p, n in files.items()})
        self.doubts = [
            f"{moves.get(line.split(':', 1)[0], line.split(':', 1)[0])}:{line.split(':', 1)[1]}"
            for line in self.doubts
        ]

    def print(self, *, full: bool = False) -> None:
        print("\n== rules, in order ==")
        for key in self.order:
            files = self.counts.get(key, collections.Counter())
            total = sum(files.values())
            print(f"{key:4} {self.titles[key]}: {len(files)} files, {total} occurrences")
            shown = sorted(files.items()) if full or len(files) <= 40 or key == "R1" else sorted(files.items(), key=lambda item: -item[1])[:15]
            for path, n in shown:
                print(f"       {n:5}  {path}")
            if len(shown) < len(files):
                print(f"       ... and {len(files) - len(shown)} more files")
        if self.doubts:
            by: collections.Counter[str] = collections.Counter()
            for line in self.doubts:
                path = line.split(":", 1)[0]
                by["/".join(path.split("/")[:3]) if path.startswith(("src/", "tests/")) else path.split("/")[0]] += 1
            print("\n== sentences rule P left for the prose sweep, by directory (--doubts lists each) ==")
            for folder, n in sorted(by.items()):
                print(f"  {n:5}  {folder}")
        if self.warnings:
            print("\n== warnings ==")
            for line in self.warnings:
                print(f"  {line}")
        if self.notes:
            print("\n== notes ==")
            for line in self.notes:
                print(f"  {line}")


REPORT = Report()

# -- the word -------------------------------------------------------------------------------------

# One part of a name: an acronym, a capitalised or lower-case word, or digits.
PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
WORDS = {"pack": "agent", "packs": "agents"}
TOKEN = re.compile(r"[A-Za-z0-9_$]+")
# Whole names with a name of their own (4.1, 4.2, 4.7, 4.8, 4.11, 4.13).
SPECIAL = {
    "_PackLoader": "_ManifestLoader",
    "pack_tests": "behaviour_tests",
    "run_pack_tests": "run_behaviour_tests",
    "_pack_subject": "_version_subject",
    "_decision_packs": "_agent_versions",
    "packless": "agentless",
    "repack": "rearchive",
    # tests/test_agent_provider.py's fixture: the agent `mendari new` writes, whose provider is an
    # `agent` variant ("agent pack" before): its name says how it answers.
    "agent_pack": "http_agent",
    "agent_pack_dir": "http_agent_dir",
    "test_agent_pack_may_not_choose_deployment_env_vars": "test_http_agent_may_not_choose_deployment_env_vars",
    "test_new_agent_pack_works_offline_as_written": "test_new_agent_works_offline_as_written",
}
# The words of 4.13, and the rule that takes them out before the residue is counted.
KEPT_WORDS = re.compile(r"(?i)(?<![a-z0-9])(?:un|back)pack|struct\.pack\b")
RESIDUE = re.compile(r"(?i)pack(?!ag|et)")


def _case_like(source: str, new: str) -> str:
    if source.isupper() and len(source) > 1:
        return new.upper()
    if source[:1].isupper():
        return new[:1].upper() + new[1:]
    return new


def rename_name(name: str) -> str:
    """`name` with each part that is "pack" or "packs" (any case) said as "agent(s)", and an "a"
    part before one as "an" (`test_a_pack_x` -> `test_an_agent_x`); a name of `SPECIAL` as it
    says. Parts are split at `_`, `$`, digits and case changes, so `package`, `packet`, `unpack`,
    `webpack` and `msgpack` are never one."""
    if name in SPECIAL:
        return SPECIAL[name]
    parts = list(PART.finditer(name))
    out: list[str] = []
    pos = 0
    changed = False
    for index, found in enumerate(parts):
        out.append(name[pos : found.start()])
        part = found.group(0)
        following = parts[index + 1] if index + 1 < len(parts) else None
        if part.lower() in WORDS:
            out.append(_case_like(part, WORDS[part.lower()]))
            changed = True
        elif (
            part in ("a", "A")
            and following is not None
            and following.group(0).lower() in WORDS
            and name[found.end() : following.start()] in ("", "_")
        ):
            nxt = following.group(0)
            out.append("AN" if nxt.isupper() and len(nxt) > 1 else ("An" if part == "A" else "an"))
        else:
            out.append(part)
        pos = found.end()
    out.append(name[pos:])
    return "".join(out) if changed else name


# -- files ----------------------------------------------------------------------------------------


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and done.returncode:
        raise SystemExit(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout


def tracked(repo: Path) -> list[str]:
    return sorted(git(repo, "ls-files", "-z").split("\0")[:-1])


def excluded(path: str) -> bool:
    if path in EXCLUDED_FILES or path.startswith(EXCLUDED_DIRS):
        return True
    name = path.rsplit("/", 1)[-1]
    if name in ("package-lock.json", "Gemfile.lock") or name.endswith(".lock"):
        return True
    return Path(path).suffix.lower() in BINARY_SUFFIXES


def read_text(repo: Path, path: str) -> str | None:
    data = (repo / path).read_bytes()
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# -- segments: what is code and what is prose ------------------------------------------------------


@dataclass(frozen=True)
class Prose:
    """A span of prose, and the span its sentences are read in (a comment block, a docstring,
    a Markdown document). `inline`: a Markdown code span, which is code, read in its sentence."""

    start: int
    end: int
    context_start: int
    context_end: int
    inline: bool = False


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def python_prose(text: str) -> list[Prose]:
    """Comments (a block of whole-line comments is one context) and docstrings."""
    starts = _line_starts(text)

    def at(line: int, col: int) -> int:
        return starts[line - 1] + col

    spans: list[Prose] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return spans
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                value = body[0].value
                if isinstance(value.value, str) and value.end_lineno is not None:
                    s = at(value.lineno, value.col_offset)
                    e = at(value.end_lineno, value.end_col_offset or 0)
                    spans.append(Prose(s, e, s, e))
    comments: list[tuple[int, int, int, bool]] = []  # start, end, line, whole line
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return spans
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            s, e = at(*tok.start), at(*tok.end)
            whole = not tok.line[: tok.start[1]].strip()
            comments.append((s, e, tok.start[0], whole))
    block: list[tuple[int, int, int, bool]] = []

    def flush() -> None:
        if block:
            cs, ce = block[0][0], block[-1][1]
            spans.extend(Prose(s, e, cs, ce) for s, e, _, _ in block)
            block.clear()

    for item in comments:
        if block and item[3] and block[-1][3] and item[2] == block[-1][2] + 1:
            block.append(item)
        else:
            flush()
            block.append(item)
            if not item[3]:
                flush()
    flush()
    return sorted(spans, key=lambda p: p.start)


def hash_prose(text: str) -> list[Prose]:
    """`#` comments of YAML, shell, TOML, Ruby, Dockerfiles and the like: a `#` at a line's
    start or after a space, outside quotes. Consecutive comment lines are one context."""
    spans: list[tuple[int, int, int]] = []
    offset = 0
    for number, line in enumerate(text.splitlines(keepends=True)):
        quote = None
        for index, char in enumerate(line):
            if quote:
                if char == quote and line[index - 1 : index] != "\\":
                    quote = None
                continue
            if char in "\"'" and (index == 0 or not line[index - 1].isalnum()):
                quote = char
            elif char == "#" and (index == 0 or line[index - 1] in " \t"):
                end = len(line.rstrip("\n"))
                spans.append((offset + index, offset + end, number))
                break
        offset += len(line)
    out: list[Prose] = []
    group: list[tuple[int, int, int]] = []

    def flush() -> None:
        if group:
            out.extend(Prose(s, e, group[0][0], group[-1][1]) for s, e, _ in group)
            group.clear()

    for item in spans:
        if group and item[2] != group[-1][2] + 1:
            flush()
        group.append(item)
    flush()
    return out


def js_prose(text: str) -> list[Prose]:
    """Comments of JavaScript and TypeScript, found by a small lexer that skips strings,
    template literals (and the code inside their `${}`) and regular expressions."""
    spans: list[Prose] = []
    n = len(text)
    i = 0
    last = ""  # the last significant character outside comments

    def skip_string(i: int, quote: str) -> int:
        i += 1
        while i < n and text[i] != quote:
            if text[i] == "\\":
                i += 1
            elif text[i] == "\n" and quote != "`":
                return i
            elif quote == "`" and text.startswith("${", i):
                i = skip_code(i + 2, "}")
                continue
            i += 1
        return i + 1

    def skip_code(i: int, until: str) -> int:
        depth = 0
        while i < n:
            c = text[i]
            if c in "\"'`":
                i = skip_string(i, c)
                continue
            if text.startswith("//", i) or text.startswith("/*", i):
                i = comment(i)
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0 and until == "}":
                    return i + 1
                depth -= 1
            i += 1
        return i

    def comment(i: int) -> int:
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
        else:
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
        spans.append(Prose(i, end, i, end))
        return end

    regex_before = set("(,=:[!&|?{};+-*%<>~^") | {""}
    while i < n:
        c = text[i]
        if text.startswith("//", i) or text.startswith("/*", i):
            i = comment(i)
            continue
        if c in "\"'`":
            i = skip_string(i, c)
            last = c
            continue
        if c == "/" and last in regex_before:
            j = i + 1
            klass = False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    klass = True
                elif text[j] == "]":
                    klass = False
                elif text[j] == "/" and not klass:
                    break
                j += 1
            i = j + 1
            last = "/"
            continue
        if not c.isspace():
            last = c
            if c.isalnum() or c == "_":
                found = TOKEN.match(text, i)
                assert found is not None
                word = found.group(0)
                last = "" if word in ("return", "typeof", "case", "in", "of", "new") else "a"
                i = found.end()
                continue
        i += 1
    # consecutive `//` lines are one context
    merged: list[Prose] = []
    for span in spans:
        if (
            merged
            and text.startswith("//", span.start)
            and text.startswith("//", merged[-1].start)
            and text[merged[-1].end : span.start].strip() == ""
            and text[merged[-1].end : span.start].count("\n") == 1
        ):
            first = merged[-1].context_start
            merged = [
                Prose(p.start, p.end, first, span.end) if p.context_start == first else p
                for p in merged
            ]
            merged.append(Prose(span.start, span.end, first, span.end))
        else:
            merged.append(span)
    return merged


def delimited_prose(text: str, opening: str, closing: str) -> list[Prose]:
    spans = []
    for found in re.finditer(re.escape(opening) + r".*?" + re.escape(closing), text, re.S):
        spans.append(Prose(found.start(), found.end(), found.start(), found.end()))
    return spans


_FENCE = re.compile(r"(?m)^( {0,3}|\s*[-*] +| *\d+\. +)?(`{3,}|~{3,})[^\n]*\n(.*?)^\s*\2[ \t]*$", re.S)
_MD_CODE = re.compile(
    r"(?P<code>(?P<ticks>`+)(?!`).+?(?<!`)(?P=ticks)(?!`))"  # an inline code span
    r"|\]\((?P<target>[^)\s]*)"  # a link's target
    r"|(?P<url>https?://[^\s)>\]`\"']+)"  # a bare address
    r"|(?P<tag></?[A-Za-z][^>\n]*>)"  # an HTML tag
    r"|(?m:^\[[^\]\n]+\]:\s*\S+)",  # a reference definition
    re.S,
)


def markdown_prose(text: str) -> list[Prose]:
    """Markdown's text: everything outside fenced code, inline code, link targets, addresses
    and HTML tags; and its inline code spans (`inline`). Its sentences are read in the whole
    document."""
    n = len(text)
    code: list[tuple[int, int, bool]] = [(m.start(), m.end(), False) for m in _FENCE.finditer(text)]
    for found in _MD_CODE.finditer(text):
        if any(s <= found.start() < e for s, e, _ in code):
            continue
        if found.group("target") is not None:
            code.append((found.start("target"), found.end("target"), False))
        else:
            code.append((found.start(), found.end(), found.group("code") is not None))
    code.sort()
    spans: list[Prose] = []
    pos = 0
    for s, e, inline in code:
        if s > pos:
            spans.append(Prose(pos, s, 0, n))
        if inline and s >= pos:
            spans.append(Prose(s, e, 0, n, inline=True))
        pos = max(pos, e)
    if pos < n:
        spans.append(Prose(pos, n, 0, n))
    return spans


def prose_spans(path: str, text: str) -> list[Prose]:
    name = path.rsplit("/", 1)[-1]
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return python_prose(text)
    if suffix in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
        return js_prose(text)
    if suffix == ".css":
        return delimited_prose(text, "/*", "*/")
    if suffix in (".html", ".htm", ".svg", ".xml"):
        return delimited_prose(text, "<!--", "-->")
    if suffix == ".md":
        return markdown_prose(text)
    if suffix in (".json", ".jsonl"):
        return []
    if suffix in (".yml", ".yaml", ".sh", ".toml", ".cfg", ".ini", ".rb", ".txt", ".conf", ".example", ".env") or name in (
        "Dockerfile",
        "CODEOWNERS",
        "Gemfile",
        ".gitignore",
        ".dockerignore",
        ".vercelignore",
        "mendari-site",
    ) or suffix == "":
        return hash_prose(text)
    return []


# -- sentences ------------------------------------------------------------------------------------

# A sentence is left for the prose sweep (rule P2) when renaming its "pack" may say something else:
# it speaks of agents already (an agent's service and the agent it answers for, or both words as
# two things), or it is about names and their history (which the rename may make false).
HISTORY = re.compile(
    r"(?i)decision[\s_-]*packs?|\bformer\b|\bwords?\b|\bvocabulary\b|\brenam|\blegacy\b|\bold name"
    r"|\bcalled\b|\bused to\b|\bno longer\b|\bas (?:it|they) (?:was|were|did) before\b|\bas before\b"
    r"|\bhad before\b|\bbefore (?:wave|the rename|LP-)|\bwave [0-9]|\bkeeps? (?:its|their) names?"
    r"|\bthe wire\b|\bwire names?\b|\bused before\b|\bthe word\b|\bstill (?:read|load|work)s?\b"
    r"|\bstays?\s+`?packs?\b"
)
_CODE_SPAN = re.compile(r"`(?!agents?`)[^`\n]*`")
_PATHLIKE = re.compile(r"\S*[/_]\S*")
_AGENT_WORD = re.compile(r"(?i)(?<![\w-])agents?(?:'s?)?(?![\w-])|`agents?`")


def doubtful(said: str) -> bool:
    """Whether a sentence is one rule P leaves (module comment above): what it says of agents
    is read outside code spans and paths, but `agent` (the provider type) counts."""
    if HISTORY.search(said):
        return True
    plain = _PATHLIKE.sub(" ", _CODE_SPAN.sub(" ", said))
    return bool(_AGENT_WORD.search(plain)) or "`agent`" in said or "`agents`" in said


_SENTENCE_END = re.compile(r"[.!?](?=[\s\"')\]]|$)|\n[ \t]*(?:#|//|\*)?[ \t]*\n|\n[ \t]*(?:#|//)?[ \t]*(?:[-*+]|\d+\.|\|)[ \t]|\|")


def sentence(text: str, start: int, end: int, lo: int, hi: int) -> str:
    """The sentence of `text[lo:hi]` around `start:end`. (The context is searched whole: a search
    that ended at `start` would take a `?` or a `.` just before the word for a sentence's end.)"""
    left, right = lo, hi
    for found in _SENTENCE_END.finditer(text, lo, hi):
        if found.end() <= start:
            left = found.end()
        elif found.start() >= end:
            right = found.start() + 1
            break
    return text[left:right]


def paragraph(text: str, start: int, end: int, lo: int, hi: int) -> str:
    """The paragraph of `text[lo:hi]` around `start:end`: between blank lines."""
    blank = re.compile(r"\n[ \t]*(?:#|//|\*)?[ \t]*\n")
    left, right = lo, hi
    for found in blank.finditer(text, lo, hi):
        if found.end() <= start:
            left = found.end()
        elif found.start() >= end:
            right = found.start()
            break
    return text[left:right]


# -- the rename of one file's text ----------------------------------------------------------------

# "a" before a renamed word: on the same line (with a space, or a quote or backtick), or, in
# prose only, at the end of the line before (a comment's marker after the line break).
_ARTICLE = re.compile(r"(?<![\w$.\-/])([Aa])[ \t]+[`\"']?\Z")
_ARTICLE_ACROSS = re.compile(r"(?<![\w$.\-/])([Aa])[ \t]*\n[ \t]*(?:(?:#+|//+|\*)[ \t]*)?[`\"']?\Z")
_BARE_BEFORE = set("`_-./$\\#@{<=:\"'~")
_BARE_AFTER = set("`_-/$\\(=[{<@\"'~")


def _bare(text: str, start: int, end: int) -> bool:
    """Whether the word at `start:end` is a word of a sentence, not a name: nothing that joins
    names touches it ("pack's" and "packs." are words, `pack_id`, `pack.yaml`, `--pack` and
    "pack" in quotes are not)."""
    before = text[start - 1] if start else " "
    after = text[end] if end < len(text) else " "
    if before.isalnum() or before in _BARE_BEFORE:
        return False
    if after.isalnum() or after in _BARE_AFTER:
        return after == "'" and text[end + 1 : end + 2] in ("s", " ", "") and before != "'"
    if after in ".:" and end + 1 < len(text) and (text[end + 1].isalnum() or text[end + 1] in "_/"):
        return False
    return True


@dataclass
class Outcome:
    text: str
    code: int = 0
    prose: int = 0
    articles: int = 0
    doubts: list[str] = field(default_factory=list)


def rename_text(path: str, text: str, *, prose_rule: bool = True) -> Outcome:
    """Rules G and P on one file's text (the protected words kept)."""
    masks: dict[str, str] = {}
    for index, word in enumerate(PROTECTED):
        marker = f"\x00{index}\x00"
        if word in text:
            masks[marker] = word
            text = text.replace(word, marker)
    spans = prose_spans(path, text)
    starts = [p.start for p in spans]
    out: list[str] = []
    renamed_at: list[tuple[int, bool]] = []
    seen: set[str] = set()  # sentences listed for the sweep, once each
    outcome = Outcome(text)
    pos = 0
    length = 0
    starts_of_lines = None
    for found in TOKEN.finditer(text):
        name = found.group(0)
        new = rename_name(name)
        if new == name:
            continue
        s, e = found.start(), found.end()
        index = bisect.bisect_right(starts, s) - 1
        span = spans[index] if index >= 0 and spans[index].start <= s < spans[index].end else None
        in_prose = span is not None and not span.inline
        said = None if span is None else sentence(text, s, e, span.context_start, span.context_end)
        bare = in_prose and name.lower() in WORDS and _bare(text, s, e)
        # In a sentence about names or their history, nothing is renamed (code spans too): the
        # rename may make it false, so the prose sweep rewrites it whole.
        if said is not None and (HISTORY.search(said) or (bare and (not prose_rule or doubtful(said)))):
            if starts_of_lines is None:
                starts_of_lines = _line_starts(text)
            line = bisect.bisect_right(starts_of_lines, s)
            words = " ".join(said.split())[:200]
            if words not in seen:
                seen.add(words)
                outcome.doubts.append(f"{path}:{line}: {words}")
            continue
        if bare:
            outcome.prose += 1
        else:
            outcome.code += 1
        chunk = text[pos:s]
        out.append(chunk)
        length += len(chunk)
        renamed_at.append((length, in_prose))
        out.append(new)
        length += len(new)
        pos = e
    out.append(text[pos:])
    result = "".join(out)
    if renamed_at:
        pieces = []
        last = 0
        for at, said_in_prose in renamed_at:
            if result[at : at + 5].lower() != "agent":
                continue
            head = result[max(0, at - 40) : at]
            found = _ARTICLE.search(head)
            if found is None and (said_in_prose or path.endswith(".md")):
                found = _ARTICLE_ACROSS.search(head)
            if found:
                cut = at - len(head) + found.end(1)
                pieces.append(result[last:cut])
                pieces.append("n")
                last = cut
                outcome.articles += 1
        pieces.append(result[last:])
        result = "".join(pieces)
    for marker, word in masks.items():
        result = result.replace(marker, word)
    outcome.text = result
    return outcome


def rename_python_names(text: str) -> tuple[str, int]:
    """Rule W's names-only rename: each NAME token, nothing else."""
    starts = _line_starts(text)
    edits: list[tuple[int, int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.NAME:
            new = rename_name(tok.string)
            if new != tok.string:
                s = starts[tok.start[0] - 1] + tok.start[1]
                edits.append((s, s + len(tok.string), new))
    return _apply(text, edits), len(edits)


def _apply(text: str, edits: Sequence[tuple[int, int, str]]) -> str:
    out, pos = [], 0
    for s, e, new in sorted(edits):
        out.append(text[pos:s])
        out.append(new)
        pos = e
    out.append(text[pos:])
    return "".join(out)


# -- Python variables, by scope (rules S and C) ---------------------------------------------------


def _scope(tree: ast.AST, qualname: str) -> ast.AST | None:
    if not qualname:
        return tree
    node: ast.AST = tree
    for part in qualname.split("."):
        nxt = None
        for child in ast.walk(node):
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and child.name == part
                and child is not node
            ):
                nxt = child
                break
        if nxt is None:
            return None
        node = nxt
    return node


def rename_variable(text: str, qualname: str, old: str, new: str) -> tuple[str, int]:
    """Every use of the variable `old` (a name, a parameter) in the function `qualname`, its
    nested functions included, as `new`; never an attribute, a key or a keyword."""
    tree = ast.parse(text)
    scope = _scope(tree, qualname)
    if scope is None:
        return text, 0
    starts = _line_starts(text)
    edits = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Name) and node.id == old:
            s = starts[node.lineno - 1] + node.col_offset
        elif isinstance(node, ast.arg) and node.arg == old:
            s = starts[node.lineno - 1] + node.col_offset
        else:
            continue
        assert text[s : s + len(old)] == old, (qualname, old, node.lineno)
        edits.append((s, s + len(old), new))
    return _apply(text, edits), len(edits)


# -- the rules' data ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Edit:
    """A pattern replaced in the files `paths` names (globs): `rule` reports it."""

    rule: str
    paths: tuple[str, ...]
    pattern: str
    repl: str
    flags: int = 0


@dataclass(frozen=True)
class Var:
    """A variable renamed in one function (rule S or C)."""

    rule: str
    path: str
    scope: str
    old: str
    new: str


def matches(path: str, globs: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, glob) for glob in globs)


# S: Python files where every `agent_id` (and `AGENT_ID`) is the service's (4.10), checked
# against the tree at 9dda7607. A Python file with `agent_id` in neither list is listed for a look.
SERVICE_FILES = (
    "src/mendari/agent_kit.py",
    "examples/*/mendari_agent_kit.py",
    "examples/*/agent.py",
    "examples/*/api/index.py",
    "src/mendari/providers/agent.py",
    "src/mendari/providers/agent_calibration.py",
    "src/mendari/core/card.py",
    "src/mendari/core/packet.py",
    "src/mendari/evals/confidence.py",
    "src/mendari/evals/runs.py",
    "tests/support/confidence_agents.py",
    "tests/test_action_decisions.py",
    "tests/test_agent_calibration.py",
    "tests/test_agent_kit.py",
    "tests/test_agent_provider.py",
    "tests/test_caps_together.py",
    "tests/test_claude_agent_example.py",
    "tests/test_fixture_agents.py",
    "tests/test_packet.py",
    "tests/test_provider_configs.py",
    "tests/test_providers.py",
    "tests/test_second_signal.py",
)
# Python files whose `agent_id` is the governed agent already: left alone by rule S.
GOVERNED_FILES = (
    "src/mendari/evals/gate.py",
    "src/mendari/hub/registry.py",
    "scripts/fhir_output_sample.py",
    "tests/conftest.py",
    "tests/support/chart_fixture.py",
    "tests/support/fhir_decisions_golden.py",
    "tests/support/registry.py",
    "tests/test_capability.py",
    "tests/test_fhir_decisions.py",
    "tests/test_server.py",
    "tests/test_smart_launch.py",
    "tests/test_terminology.py",
    *NAMES_ONLY,
)
TEXT_SERVICE_FILES = ("*.md", "*.yml", "*.yaml")  # docs and manifests: the variant's key

EDITS: list[Edit] = [
    # -- S: the service ---------------------------------------------------------------------------
    # The service's version, everywhere but the registry route that names a governed version.
    Edit("S1", ("*",), r'(?<!name=")\bagent_version\b', "service_version"),
    Edit("S1", ("examples/*", "tests/test_claude_agent_example.py"), r"\bAGENT_VERSION\b", "SERVICE_VERSION"),
    Edit("S2", ("*",), r"\bAGENT_ID_PATTERN\b", "SERVICE_ID_PATTERN"),
    Edit("S3", SERVICE_FILES, r"\bagent_id\b", "service_id"),
    Edit("S3", ("tests/support/confidence_agents.py", "tests/test_agent_calibration.py"), r"\bAGENT_ID\b", "SERVICE_ID"),
    Edit("S3", TEXT_SERVICE_FILES, r"\bagent_id\b", "service_id"),
    # The kit's `Case`: the governed agent's id is `agent_id` (its service's is `service_id` now).
    Edit("S4", ("src/mendari/agent_kit.py", "examples/*/mendari_agent_kit.py"), r"(?m)^(\s+)agent: str \| None(\s+# the agent's id)", r"\1agent_id: str | None\2"),
    Edit("S4", ("src/mendari/agent_kit.py", "examples/*/mendari_agent_kit.py"), r"\bagent=_text\(body\.get\(\"pack_id\"\)\)", 'agent_id=_text(body.get("pack_id"))'),
    Edit("S4", ("tests/test_agent_kit.py",), r"\bcase\.agent\b(?!_)", "case.agent_id"),
    # The calibration file's and report's `agent` key is the service's (`agent-calibration/1`,
    # `calibration-report/1`, the committee packet's calibration entries).
    Edit("S5", ("src/mendari/providers/agent_calibration.py",), r'\bdata(\.get\(|\[)"agent"', r'data\1"service"'),
    Edit("S5", ("src/mendari/providers/agent_calibration.py",), r'(?m)^(\s+)"agent": \{$', r'\1"service": {'),
    Edit("S5", ("src/mendari/cli.py",), r'\bfitted\["agent"\]', 'fitted["service"]'),
    Edit("S5", ("src/mendari/core/packet.py", "src/mendari/evals/confidence.py"), r'\bfitted\.get\("agent"\)', 'fitted.get("service")'),
    Edit("S5", ("src/mendari/evals/confidence.py",), r'"agent": \{"id": found\.', '"service": {"id": found.'),
    Edit("S5", ("tests/support/confidence_agents.py",), r'"agent": \{"id": (agent_id|service_id),', r'"service": {"id": \1,'),
    Edit("S5", ("tests/test_agent_calibration.py",), r'\b(found|written)\["agent"\]', r'\1["service"]'),
    Edit("S5", ("tests/test_agent_calibration.py",), r'\{"agent": \{"id": (AGENT_ID|SERVICE_ID)\}\}', r'{"service": {"id": \1}}'),
    Edit("S5", ("tests/test_agent_calibration.py",), r'\["fitted"\]\["agent"\]', '["fitted"]["service"]'),
    Edit("S5", ("tests/test_packet.py",), r'"agent": \{"id": "portal-agent", "version": "2\.1\.0"\}', '"service": {"id": "portal-agent", "version": "2.1.0"}'),
    Edit("S5", ("docs/spec/agent-manifest-0.4.md",), r'"agent": \{"id": "portal-agent", "version"', '"service": {"id": "portal-agent", "version"'),
    Edit("S5", ("docs/spec/agent-manifest-0.4.md",), r"`agent\.id` and `agent\.version` of the file", "`service.id` and `service.version` of the file"),
    # What a calibration file names is the service's version and id (its `service` key).
    Edit("S5", ("src/mendari/providers/agent_calibration.py", "tests/test_agent_calibration.py"), r"does not say which agent version it is for", "does not say which service version it is for"),
    Edit("S5", ("src/mendari/providers/agent_calibration.py", "tests/test_agent_calibration.py"), r"the calibration file's agent id is not a name", "the calibration file's service id is not a name"),
    # -- E: event kinds and chain subjects (4.7) ---------------------------------------------------
    Edit("E1", ("*",), r"(?<![\w.])pack_version\.(created|held|released|withdrawn|promoted|retired|\*|%)", r"store_version.\1"),
    # The subject prefix, however its id follows: `pack_version:{id}`, `pack_version:7`,
    # `pack_version:<id>`, a quoted `"pack_version:"` (never `pack_version: str`, nor a key).
    Edit("E1", ("*",), r"(?<![\w.])pack_version:(?=[{<\w\"'`])", "store_version:"),
    Edit("E1", ("src/mendari/core/audit.py",), r'"pack_version": \("store_versions"', '"store_version": ("store_versions"'),
    Edit("E1", ("src/mendari/core/audit.py",), r'(": )"pack_version",', r'\1"store_version",'),
    Edit("E1", ("tests/*.py",), r'subject_history\("pack_version"', 'subject_history("store_version"'),
    Edit("E1", ("src/mendari/dashboard/admin.js",), r"\bpack_version: \"Agent store version\"", 'store_version: "Agent store version"'),
    # -- F: FHIR identifiers (4.8) ----------------------------------------------------------------
    Edit("F1", ("*",), r"(?<![\w-])decision-pack(?![\w-])", "agent-version"),
    # -- V: formats that restart at /1 (4.12) --------------------------------------------------
    Edit("V1", ("*.py",), r"\b(SiteReport|SITE_REPORT_|PublisherEvaluation|Claimed)V2\b", r"\1V1"),
    Edit("V1", ("src/mendari/hub/evaluations.py",), r"\bFORMAT_V2\b", "FORMAT_V1"),
    # The format a registry once refused by its name is the one it takes now: the dead constant goes.
    Edit("V1", ("src/mendari/evals/share.py",), r'(?m)^SITE_REPORT_FORMAT = "mendari\.site-report/1"  # the format a registry refuses by its name\n', ""),
    # -- M: names of their own (4.1, 4.11, 4.13) --------------------------------------------------
    Edit("M1", ("*",), r"(?<![\w.-])pack\.js\b", "manifest.js"),
    Edit("M1", ("scripts/fhir_r4_schema.py",), r"\bre-packed\b", "re-zipped"),
    Edit("M1", ("*",), r"(?<![\w-])agent-pack(?![\w-])", "http-agent"),
    Edit("M1", ("*",), r"\b([Aa])gent (pack)(s?)\b", r"\1gent\3"),
    # Roles listed in order: `agent_author` sorts before `labeller`, where `pack_author` sorted after.
    Edit("M2", ("tests/test_accounts_store.py",), r'\["admin", "labeller", "pack_author"\]', '["admin", "pack_author", "labeller"]'),
    Edit("M2", ("tests/test_memberships.py",), r'\["labeller", "pack_author"\]', '["pack_author", "labeller"]'),
]

VARS: list[Var] = [
    # S6: a local `agent` that is the service's
    Var("S6", "src/mendari/core/card.py", "_confidence", "agent", "service"),
    Var("S6", "src/mendari/core/packet.py", "_running", "agent", "service"),
    Var("S6", "src/mendari/core/packet.py", "_evaluated_calibration", "agent", "service"),
    Var("S6", "src/mendari/core/packet.py", "_html_confidence", "agent", "service"),
    Var("S6", "src/mendari/providers/agent_calibration.py", "Calibration.words", "agent", "service"),
    Var("S6", "src/mendari/providers/agent_calibration.py", "shape_problems", "agent", "service"),
    Var("S6", "src/mendari/providers/agent_calibration.py", "of", "agent", "service"),
    Var("S6", "src/mendari/cli.py", "_calibrate_agent", "agent", "service"),
    Var("S6", "src/mendari/evals/confidence.py", "_fitted_words", "agent", "service"),
    # C: an `agent` that is something else meets the renamed `pack`
    Var("C1", "src/mendari/mcp.py", "_decision", "agent", "agent_id"),
    Var("C1", "src/mendari/sandbox/demo.py", "_run", "agent", "decision"),
    Var("C1", "src/mendari/sandbox/fhir_workflow.py", "seed", "pack", "loaded"),
    Var("C1", "src/mendari/sandbox/pilot_check.py", "run", "pack", "loaded"),
    Var("C1", "tests/support/engine.py", "_evaluated", "agent", "folder"),
    Var("C1", "tests/support/engine.py", "published", "agent", "folder"),
    Var("C1", "tests/test_claude_agent_example.py", "test_its_answers_pass_mendaris_own_validation", "pack", "loaded"),
    Var("C1", "tests/test_hub_routes.py", "test_a_search_and_a_listing_say_what_the_registry_checked_apart_from_what_is_claimed", "agent", "listed"),
    Var("C1", "tests/test_monitoring.py", "test_a_pack_doing_as_its_benchmark_says_rarely_alerts", "agent", "folder"),
    Var("C1", "tests/test_packet.py", "test_with_a_database_the_command_writes_what_the_deployment_holds_as_before", "agent", "folder"),
    Var("C1", "tests/test_ranking.py", "test_the_card_says_the_order_its_rows_are_in", "agent", "folder"),
    Var("C1", "tests/test_workflow_safety.py", "test_changed_files_map_to_their_pack", "pack", "folder"),
    Var("C1", "tests/test_two_organizations.py", "seed", "packed", "answered"),
]


# -- the collision gates --------------------------------------------------------------------------

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


@dataclass
class _Scope:
    node: ast.AST
    bound: set[str] = field(default_factory=set)
    used: set[str] = field(default_factory=set)
    kids: list[_Scope] = field(default_factory=list)


def _build(node: ast.AST) -> _Scope:
    scope = _Scope(node)

    def visit(n: ast.AST, top: bool) -> None:
        if not top and isinstance(n, _SCOPES):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope.bound.add(n.name)
                for d in n.decorator_list:
                    visit(d, False)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for d in [*n.args.defaults, *n.args.kw_defaults]:
                    if d is not None:
                        visit(d, False)
            scope.kids.append(_build(n))
            return
        if isinstance(n, ast.Name):
            (scope.bound if isinstance(n.ctx, (ast.Store, ast.Del)) else scope.used).add(n.id)
        elif isinstance(n, ast.arg):
            scope.bound.add(n.arg)
        elif isinstance(n, ast.alias):
            scope.bound.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            scope.bound.add(n.name)
        children: Iterable[ast.AST]
        if top and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            children = [n.args, *n.body]
        elif top and isinstance(n, ast.Lambda):
            children = [n.args, n.body]
        elif top and isinstance(n, ast.ClassDef):
            children = n.body
        else:
            children = ast.iter_child_nodes(n)
        for child in children:
            if isinstance(child, ast.arguments) and top:
                for a in [*child.posonlyargs, *child.args, *child.kwonlyargs, child.vararg, child.kwarg]:
                    if a is not None:
                        scope.bound.add(a.arg)
                continue
            visit(child, False)

    visit(node, True)
    return scope


def python_collisions(path: str, text: str) -> list[str]:
    """Where the rename would make two names one: in one scope, in a nested scope that reads the
    outer name, in a call's keywords, a dict literal's keys or a class's attributes."""
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        return [f"{path}: does not parse: {error}"]
    found: list[str] = []

    def walk(scope: _Scope) -> None:
        names = scope.bound | scope.used
        line = getattr(scope.node, "lineno", 0)
        for name in names:
            new = rename_name(name)
            if new == name:
                continue
            if new in names:
                found.append(f"{path}:{line}: in one scope: {name} and {new}")
            elif name in scope.bound:
                stack = list(scope.kids)
                while stack:
                    kid = stack.pop()
                    if new in kid.used and new not in kid.bound:
                        found.append(f"{path}:{getattr(kid.node, 'lineno', 0)}: reads {new}, which the renamed {name} of line {line} would hide")
                    if new not in kid.bound:
                        stack.extend(kid.kids)
        for kid in scope.kids:
            walk(kid)

    walk(_build(tree))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            keywords = {k.arg for k in node.keywords if k.arg}
            for k in keywords:
                if rename_name(k) != k and rename_name(k) in keywords:
                    found.append(f"{path}:{node.lineno}: keywords {k} and {rename_name(k)}")
        elif isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            for k in keys:
                new = rename_text("x.json", k).text
                if new != k and new in keys:
                    found.append(f"{path}:{node.lineno}: keys {k!r} and {new!r}")
        elif isinstance(node, ast.ClassDef):
            attrs = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    attrs.add(item.target.id)
                elif isinstance(item, ast.Assign):
                    attrs.update(t.id for t in item.targets if isinstance(t, ast.Name))
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    attrs.add(item.name)
            for item in ast.walk(node):
                if isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Store) and isinstance(item.value, ast.Name) and item.value.id in ("self", "cls"):
                    attrs.add(item.attr)
            for a in attrs:
                if rename_name(a) != a and rename_name(a) in attrs:
                    found.append(f"{path}:{node.lineno}: class {node.name}: {a} and {rename_name(a)}")
    return found


JS_GATE = r"""
const ts = require(process.argv[2]);
const fs = require("fs");
const PART = /[A-Z]+(?=[A-Z][a-z])|[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+/g;
const WORDS = { pack: "agent", packs: "agents" };
const like = (s, n) => s.length > 1 && s === s.toUpperCase() ? n.toUpperCase() : s[0] === s[0].toUpperCase() ? n[0].toUpperCase() + n.slice(1) : n;
const rn = (name) => { let c = false; const o = name.replace(PART, (p) => { const w = WORDS[p.toLowerCase()]; if (w) { c = true; return like(p, w); } return p; }); return c ? o : name; };
const isScope = (n) => ts.isFunctionLike(n) || ts.isBlock(n) || ts.isSourceFile(n) || ts.isClassLike(n) || ts.isForStatement(n) || ts.isForOfStatement(n) || ts.isForInStatement(n) || ts.isCatchClause(n);
for (const file of process.argv.slice(3)) {
  const text = fs.readFileSync(file, "utf8");
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : file.endsWith(".ts") ? ts.ScriptKind.TS : ts.ScriptKind.JSX;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const say = (what, node, a, b) => console.log(`${file}:${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}: ${what}: ${a} and ${b}`);
  const direct = (scope) => {
    const names = new Set();
    const visit = (n, top) => {
      if (!top && isScope(n)) { if ((ts.isFunctionDeclaration(n) || ts.isClassDeclaration(n)) && n.name) names.add(n.name.text); return; }
      if (ts.isIdentifier(n)) {
        const p = n.parent;
        const prop = (ts.isPropertyAccessExpression(p) && p.name === n) || (ts.isPropertyAssignment(p) && p.name === n) || (ts.isPropertyDeclaration(p) && p.name === n) || (ts.isMethodDeclaration(p) && p.name === n) || (ts.isPropertySignature(p) && p.name === n) || ts.isJsxAttribute(p) || (ts.isQualifiedName(p) && p.right === n) || (ts.isBindingElement(p) && p.propertyName === n);
        if (!prop) names.add(n.text);
      }
      ts.forEachChild(n, (c) => visit(c, false));
    };
    visit(scope, true);
    return names;
  };
  const kids = (scope) => { const out = []; const v = (n, top) => { if (!top && isScope(n)) { out.push(n); return; } ts.forEachChild(n, (c) => v(c, false)); }; v(scope, true); return out; };
  const walk = (scope, inherited) => {
    const here = direct(scope);
    const seen = new Set([...here, ...inherited]);
    for (const n of here) { const r = rn(n); if (r !== n && seen.has(r)) say("names", scope, n, r); }
    for (const k of kids(scope)) walk(k, seen);
  };
  walk(sf, new Set());
  const keys = (n) => {
    let ks = null;
    if (ts.isObjectLiteralExpression(n)) ks = n.properties.map((p) => p.name && (ts.isIdentifier(p.name) || ts.isStringLiteral(p.name)) ? p.name.text : null);
    else if (ts.isClassLike(n) || ts.isInterfaceDeclaration(n) || ts.isTypeLiteralNode(n)) ks = n.members.map((m) => m.name && (ts.isIdentifier(m.name) || ts.isStringLiteral(m.name)) ? m.name.text : null);
    else if (ts.isJsxAttributes(n)) ks = n.properties.map((p) => p.name && ts.isIdentifier(p.name) ? p.name.text : null);
    if (ks) { const s = new Set(ks.filter(Boolean)); for (const k of s) { const r = rn(k); if (r !== k && s.has(r)) say("keys", n, k, r); } }
    ts.forEachChild(n, keys);
  };
  keys(sf);
}
"""


def js_collisions(repo: Path, texts: dict[str, str], scratch: Path) -> list[str] | None:
    typescript = Path(os.environ.get("CODEMOD_TYPESCRIPT") or repo / "hub" / "node_modules" / "typescript")
    if not typescript.is_dir() or shutil.which("node") is None:
        return None
    files = [p for p in texts if Path(p).suffix in (".js", ".mjs", ".ts", ".tsx") and RESIDUE.search(texts[p])]
    if not files:
        return []
    scratch.mkdir(parents=True, exist_ok=True)
    gate = scratch / "js_gate.cjs"
    gate.write_text(JS_GATE, encoding="utf-8")
    copies = []
    for path in files:
        copy = scratch / "js" / path
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(texts[path], encoding="utf-8")
        copies.append(str(copy))
    done = subprocess.run(["node", str(gate), str(typescript), *copies], capture_output=True, text=True)
    if done.returncode:
        return [f"the JS gate failed: {done.stderr.strip()[:500]}"]
    prefix = str(scratch / "js") + "/"
    return [line.replace(prefix, "") for line in done.stdout.splitlines() if line.strip()]


# -- rule W: the vocabulary tests -----------------------------------------------------------------

# What the rename leaves these tests with nothing to allow, or checking a name that is gone.
# (Phase 5C rewrites `test_vocabulary.py` as one rule and deletes the other three.)
W_EDITS: list[Edit] = [
    # `--agent` is read as `args.agent` everywhere now, the written rounds' list's too.
    Edit("W1", ("tests/test_cli_vocabulary.py",), r'action\.dest == "pack"', 'action.dest == "agent"'),
    Edit("W1", ("tests/test_cli_vocabulary.py",), r'(?m)^(    "store versions": \["--org", "clinic-a"\],\n)', r'\1    "written round list": [],\n'),
]


def drop_function(text: str, name: str) -> tuple[str, int]:
    """The module-level function `name` deleted, with the blank lines after it."""
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            lines = text.split("\n")
            first = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
            last = node.end_lineno or node.lineno
            while last < len(lines) and not lines[last].strip():
                last += 1
            return "\n".join(lines[:first] + lines[last:]), 1
    return text, 0


W_DROPPED: list[tuple[str, str]] = [
    # What these three kept honest is gone with the rename: no API description types a name with
    # the old word, the two kept canonical URLs are written nowhere, and `SharedPack` is
    # `SharedAgent`, under its own name (`server/api_docs.py`'s `TITLED` is a no-op now).
    ("tests/test_vocabulary.py", "test_an_api_description_still_types_its_wire_names"),
    ("tests/test_vocabulary.py", "test_every_kept_name_is_still_written_where_the_rule_is_held"),
    ("tests/test_vocabulary.py", "test_a_class_named_for_the_old_word_is_listed_under_its_title"),
]

# What the docs' vocabulary test reads (`tests/test_vocabulary_docs.py` `files()`), as globs.
_DOCS_READ = (
    "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "GOVERNANCE.md", "SECURITY.md",
    "CODE_OF_CONDUCT.md", "mkdocs.yml", "action.yml", "hub/README.md", "docker-compose.yml",
    "deploy/*", "docs/*", "docs_theme/*", ".github/*", "agents/*README*",
    "community-agents/*README*", "community-agents/MAINTAINERS.yaml", "sandbox/*README*",
    "sandbox/*.yml", "sandbox/*.json", "web/*README*", "examples/*/README.md",
)


def _allowed_entries(text: str, name: str = "ALLOWED") -> list[tuple[ast.expr | None, ast.expr]]:
    tree = ast.parse(text)
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == name and isinstance(node.value, ast.Dict):
            return list(zip(node.value.keys, node.value.values))
    return []


def prune_allowed(path: str, texts: dict[str, str], said: Callable[[tuple[str, ...]], bool | None]) -> int:
    """Each entry of the module-level `ALLOWED` of `path` whose text `said` finds written nowhere
    any more, deleted (None from `said`: not known here, kept). Entries of a `**{...: ... for name
    in (...)}` are pruned name by name."""
    text = texts[path]
    lines = text.split("\n")
    drop: set[int] = set()
    for key, value in _allowed_entries(text):
        if key is None and isinstance(value, ast.DictComp) and isinstance(value.key, ast.Tuple):
            generator = value.generators[0]
            pattern = value.key.elts[0]
            if not (isinstance(pattern, ast.Constant) and isinstance(generator.iter, ast.Tuple)):
                continue
            for element in generator.iter.elts:
                if isinstance(element, ast.Constant) and said((pattern.value, f"`{element.value}`")) is False:
                    drop.add(element.lineno - 1)
            continue
        try:
            entry = ast.literal_eval(key) if key is not None else None
        except ValueError:
            continue
        if isinstance(entry, tuple) and said(entry) is False:
            drop.update(range(key.lineno - 1, value.end_lineno or value.lineno))
    if drop:
        texts[path] = "\n".join(line for index, line in enumerate(lines) if index not in drop)
    return len(drop)


def _docs_skipped(test_source: str) -> tuple[set[str], list[tuple[str, str, str | None]]]:
    """The docs vocabulary test's own `NOT_READ` files and `PARTS` (file, first, before), read
    from its source: its module constants stand for themselves."""
    tree = ast.parse(test_source)
    constants: dict[str, object] = {}
    not_read: set[str] = set()
    parts: list[tuple[str, str, str | None]] = []
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
        if not isinstance(target, ast.Name) or node.value is None:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            constants[target.id] = node.value.value

        def value(expr: ast.expr) -> object:
            if isinstance(expr, ast.Name):
                return constants.get(expr.id)
            try:
                return ast.literal_eval(expr)
            except ValueError:
                return None

        if target.id == "NOT_READ" and isinstance(node.value, ast.Dict):
            not_read = {k for k in (value(key) for key in node.value.keys if key is not None) if isinstance(k, str)}
        elif target.id == "PARTS" and isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Tuple) and len(key.elts) == 3:
                    file, first, before = (value(e) for e in key.elts)
                    if isinstance(file, str) and isinstance(first, str):
                        parts.append((file, first, before if isinstance(before, str) else None))
    return not_read, parts


def _without_parts(text: str, path: str, parts: list[tuple[str, str, str | None]]) -> str:
    lines = text.split("\n")
    skipped: set[int] = set()
    for file, first, before in parts:
        if file != path:
            continue
        starts = [i for i, line in enumerate(lines) if line.startswith(first)]
        ends = [i for i, line in enumerate(lines) if before is not None and line.startswith(before)]
        if starts:
            skipped.update(range(starts[0], ends[0] if ends else len(lines)))
    return "\n".join(line for i, line in enumerate(lines) if i not in skipped)


def written_anywhere(
    repo: Path,
    texts: dict[str, str],
    globs: Sequence[str] | None,
    skipped: tuple[set[str], list[tuple[str, str, str | None]]] = (set(), []),
) -> Callable[[str, str], bool]:
    """Whether a text is written in a file of `globs` (every text file when None), the excluded
    files read from disk; `skipped`: files not read, and parts of files (as the docs' test has
    them)."""
    not_read, parts = skipped
    every: dict[str, str] = dict(texts)
    for path in tracked(repo):
        if path not in every and not path.startswith(("notes/", "tasks/", "licenses/")) and (repo / path).is_file():
            text = read_text(repo, path)
            if text is not None:
                every[path] = text
    every = {path: _without_parts(text, path, parts) for path, text in every.items() if path not in not_read}

    def found(pattern: str, text: str) -> bool:
        return any(
            text in content
            for path, content in every.items()
            if path not in NAMES_ONLY
            and (globs is None or matches(path, globs))
            and fnmatch.fnmatchcase(path, pattern)
        )

    return found


def prune_vocabulary_tests(repo: Path, texts: dict[str, str]) -> None:
    """Rule W's allow-lists: what the rename left with nothing to allow is taken off them, as each
    test would ask ("take it off the list")."""
    test = texts.get("tests/test_vocabulary_docs.py", "")
    docs = written_anywhere(repo, texts, _DOCS_READ, _docs_skipped(test) if test else (set(), []))
    anywhere = written_anywhere(repo, texts, None)
    if "tests/test_vocabulary.py" in texts:

        def surface(entry: tuple[str, ...]) -> bool | None:
            where = {"the scripts' messages": "scripts/*.py", "the Python product's messages": "src/mendari/*.py"}
            return anywhere(where[entry[0]], entry[1]) if entry[0] in where else None

        REPORT.add("W1", "tests/test_vocabulary.py", prune_allowed("tests/test_vocabulary.py", texts, surface))
    if "tests/test_vocabulary_docs.py" in texts:
        REPORT.add("W1", "tests/test_vocabulary_docs.py", prune_allowed("tests/test_vocabulary_docs.py", texts, lambda e: docs(e[0], e[1])))
    if "tests/test_vocabulary_frontends.py" in texts:
        REPORT.add("W1", "tests/test_vocabulary_frontends.py", prune_allowed("tests/test_vocabulary_frontends.py", texts, lambda e: anywhere(e[0], e[1])))




# -- rule V's renumbering -------------------------------------------------------------------------

_RESTARTED = r"(?:eval|validation-brief|registry-version|leaderboard|site-report|publisher-evaluation)"
_SECOND = re.compile(rf"\bmendari\.({_RESTARTED})/2\b")
_FIRST = re.compile(rf"\b(?:mendari\.)?{_RESTARTED}/1\b")


def renumber_formats(texts: dict[str, str]) -> None:
    """The six formats that restart at `/1` (4.12, O2): each `mendari.<format>/2` as `/1`, except
    in a paragraph of prose that names a `/1` of them too (the format before it): renumbered, it
    would say the same name twice; it is left and listed."""
    REPORT.rule("V1", TITLES["V1"])
    for path in sorted(texts):
        text = texts[path]
        if path in NAMES_ONLY or not _SECOND.search(text):
            continue
        spans = prose_spans(path, text)
        starts = [span.start for span in spans]
        edits = []
        for found in _SECOND.finditer(text):
            index = bisect.bisect_right(starts, found.start()) - 1
            span = spans[index] if index >= 0 and spans[index].start <= found.start() < spans[index].end else None
            if span is not None:
                said = paragraph(text, found.start(), found.end(), span.context_start, span.context_end)
                if _FIRST.search(said):
                    line = text.count("\n", 0, found.start()) + 1
                    REPORT.doubts.append(f"{path}:{line}: (format) {' '.join(said.split())[:200]}")
                    continue
            edits.append((found.start(), found.end(), f"mendari.{found.group(1)}/1"))
        if edits:
            texts[path] = _apply(text, edits)
            REPORT.add("V1", path, len(edits))  # (file, function): a test of a mechanism the rename ends


# -- the pipeline ---------------------------------------------------------------------------------


def rename_path(path: str) -> str:
    """A tracked file's new path: rule M's `pack.js`, then each name part."""
    path = re.sub(r"(?<![\w.-])pack\.js$", "manifest.js", path)
    return TOKEN.sub(lambda found: rename_name(found.group(0)), path)


def edit_texts(texts: dict[str, str], edits: Iterable[Edit], titles: dict[str, str]) -> None:
    for edit in edits:
        REPORT.rule(edit.rule, titles[edit.rule])
        pattern = re.compile(edit.pattern, edit.flags)
        for path in sorted(texts):
            if path in NAMES_ONLY and edit.rule != "W1":
                continue
            if not matches(path, edit.paths):
                continue
            new, n = pattern.subn(edit.repl, texts[path])
            if n:
                texts[path] = new
                REPORT.add(edit.rule, path, n)


TITLES = {
    "S1": "the service's version: agent_version -> service_version (4.10)",
    "S2": "the service's id pattern: AGENT_ID_PATTERN -> SERVICE_ID_PATTERN (4.10)",
    "S3": "the service's id: agent_id -> service_id where it is the service's (4.10)",
    "S4": "the kit's Case: .agent -> .agent_id (4.10)",
    "S5": "the calibration file's and report's `agent` key -> `service` (4.10)",
    "S6": "a local `agent` that is the service -> `service` (4.10)",
    "C1": "a local `agent` that is something else, or the `pack` beside it, renamed first",
    "E1": "event kinds and subjects: pack_version.* -> store_version.* (4.7)",
    "F1": "FHIR: decision-pack -> agent-version (4.8)",
    "V1": "formats that restart at /1, and their V2 names (4.12)",
    "M1": "names of their own: pack.js -> manifest.js, re-packed -> re-zipped, agent pack -> agent (4.1, 4.13)",
    "M2": "lists in an order the rename changes: roles sorted by name",
    "G1": "every other name part: pack -> agent, packs -> agents (4.2 to 4.6, 4.9, 4.11)",
    "G2": "\"a\" -> \"an\" before a renamed word",
    "P1": "prose: a bare \"pack\"/\"packs\" in a sentence that is safe to say \"agent\" in",
    "P2": "prose left for the sweep: a sentence about agents, names or history (listed)",
    "W1": "the vocabulary tests: names only, and the allow-list entries the rename ends",
    "R1": "files moved (git mv) (4.1)",
    "X1": "ruff: imports sorted and code formatted",
    "X2": "prose lines over 100 characters re-wrapped",
    "D": "derived files regenerated",
}


def load(repo: Path) -> dict[str, str]:
    texts = {}
    for path in tracked(repo):
        if excluded(path) or not (repo / path).is_file():
            continue
        text = read_text(repo, path)
        if text is not None:
            texts[path] = text
    return texts


def unclassified_service_files(texts: dict[str, str]) -> list[str]:
    """Python files with `agent_id` in neither SERVICE_FILES nor GOVERNED_FILES: whether their
    `agent_id` is the service's or the governed agent's is a question rule S cannot answer."""
    found = []
    for path, text in sorted(texts.items()):
        if not path.endswith(".py") or not re.search(r"\bagent_id\b", text):
            continue
        if not matches(path, SERVICE_FILES) and not matches(path, GOVERNED_FILES):
            lines = [n for n, line in enumerate(text.splitlines(), 1) if re.search(r"\bagent_id\b", line)]
            found.append(
                f"S3: {path} has `agent_id` (lines {lines[:8]}): add it to SERVICE_FILES (each is "
                "the service's) or GOVERNED_FILES (each is the governed agent's), or split its rule"
            )
    return found


def transform(repo: Path, scratch: Path, *, force: bool, prose: bool) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Every rule on the texts in memory: (original texts, new texts, moves)."""
    original = load(repo)
    texts = dict(original)
    unclassified = unclassified_service_files(texts)
    edit_texts(texts, EDITS, TITLES)
    renumber_formats(texts)
    for var in VARS:
        REPORT.rule(var.rule, TITLES[var.rule])
        if var.path not in texts:
            REPORT.warnings.append(f"{var.rule}: {var.path} is not in the tree: {var.scope}.{var.old} skipped")
            continue
        new, n = rename_variable(texts[var.path], var.scope, var.old, var.new)
        if not n:
            REPORT.warnings.append(f"{var.rule}: no `{var.old}` in {var.path} {var.scope or '(module)'}: skipped")
        texts[var.path] = new
        REPORT.add(var.rule, var.path, n)
    # The gates, before any `pack` is renamed.
    found = [
        line
        for path, text in sorted(texts.items())
        if path.endswith(".py") and path not in NAMES_ONLY
        for line in python_collisions(path, text)
    ]
    found += unclassified
    js = js_collisions(repo, texts, scratch / "gate")
    if js is None:
        REPORT.warnings.append("the JS/TS collision gate did not run: no node or hub/node_modules/typescript")
    else:
        found += js
    if found:
        print("\n== stopped: two names would become one, or a file rule S cannot read ==", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        if not force:
            raise SystemExit("stopped before writing anything: add a rule (S3's lists, S6 or C1) for each, or --force")
    # G and P: every other name, and the prose that is safe to say.
    for key in ("G1", "G2", "P1", "P2", "W1"):
        REPORT.rule(key, TITLES[key])
    for path in sorted(texts):
        text = texts[path]
        if not RESIDUE.search(KEPT_WORDS.sub("", text)):
            continue
        if path in NAMES_ONLY:
            new, n = rename_python_names(text)
            texts[path] = new
            REPORT.add("W1", path, n)
            continue
        outcome = rename_text(path, text, prose_rule=prose)
        texts[path] = outcome.text
        REPORT.add("G1", path, outcome.code)
        REPORT.add("G2", path, outcome.articles)
        REPORT.add("P1", path, outcome.prose)
        REPORT.add("P2", path, len(outcome.doubts))
        REPORT.doubts += outcome.doubts
    edit_texts(texts, W_EDITS, TITLES)
    prune_vocabulary_tests(repo, texts)
    for path, function in W_DROPPED:
        if path in texts:
            texts[path], n = drop_function(texts[path], function)
            REPORT.add("W1", path, n)
            if not n:
                REPORT.warnings.append(f"W1: {path} has no `{function}` to drop (gone already?)")
    # R: the files' new names.
    REPORT.rule("R1", TITLES["R1"])
    moves: dict[str, str] = {}
    every = set(tracked(repo))
    for path in sorted(every):
        if path.startswith(EXCLUDED_DIRS):
            continue
        new = rename_path(path)
        if new != path:
            if new in every or new in moves.values():
                raise SystemExit(f"R1: {path} would move onto {new}, which exists")
            moves[path] = new
            REPORT.add("R1", f"{path} -> {new}")
    return original, texts, moves


# -- X: ruff, and prose lines over the limit -------------------------------------------------------

LIMIT = 100
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|\||[A-Za-z_]+:\s*$|>>>|\.\.\.)")


def _docstring_lines(text: str) -> dict[int, int]:
    """Line index -> the docstring's indentation, for each line inside a docstring."""
    found: dict[int, int] = {}
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                value = body[0].value
                for line in range(value.lineno - 1, (value.end_lineno or value.lineno)):
                    found[line] = value.col_offset
    return found


_LITERAL = re.compile(
    r"^(?P<indent>\s*)(?P<prefix>[rRbBfFuU]{0,2})(?P<q>[\"'])(?P<body>(?:\\.|(?!(?P=q)).)*)(?P=q)"
    r"(?P<tail>[,)\]}]*\s*(?:#.*)?)$"
)
_OPENING = ('"""', "'''", 'r"""', 'f"""', "r'''")


def _split_literal(line: str) -> list[str] | None:
    """A line that is one string literal, as two literals on two lines broken at a space (the
    caller checks the module means the same)."""
    found = _LITERAL.match(line)
    if found is None:
        return None
    indent, prefix, q, body, tail = (found.group(k) for k in ("indent", "prefix", "q", "body", "tail"))
    formatted = "f" in prefix.lower()
    depth = 0
    cuts = []
    for index, char in enumerate(body):
        if formatted and char == "{":
            depth += 1
        elif formatted and char == "}":
            depth -= 1
        elif char == " " and depth == 0 and (index == 0 or body[index - 1] != "\\"):
            cuts.append(index + 1)
    room = LIMIT - len(indent) - len(prefix) - 2
    cuts = [c for c in cuts if 0 < c <= room and c < len(body)]
    if not cuts:
        return None
    cut = cuts[-1]
    first, second = body[:cut], body[cut:]

    def literal(piece: str) -> str:
        keep = prefix if not formatted or "{" in piece else prefix.replace("f", "").replace("F", "")
        return f"{keep}{q}{piece}{q}"

    return [f"{indent}{literal(first)}", f"{indent}{literal(second)}{tail}"]


def rewrap(path: str, text: str, rows: Iterable[int]) -> tuple[str, int, list[str]]:
    """The lines ruff finds over `LIMIT` (`rows`, from 1): a comment's or a docstring's words past
    it moved to the next line of the same paragraph, or to a new line; a string literal broken in
    two at a space when the module's AST is the same after. Anything else is listed."""
    lines = text.split("\n")
    done = 0
    left: list[str] = []
    docstrings = _docstring_lines(text)
    shift = 0
    for row in sorted(rows):
        i = row - 1 + shift
        line = lines[i]
        if len(line) <= LIMIT:
            continue
        comment = re.match(r"^(\s*#+ ?)", line)
        prefix: str | None = None
        if comment and not line.strip().startswith(("#!", "# type:", "# noqa")):
            prefix = comment.group(1)
        elif (row - 1) in docstrings:
            indent = " " * docstrings[row - 1]
            opening = line.lstrip().startswith(_OPENING)
            prefix = indent if opening else line[: len(line) - len(line.lstrip())]
        if prefix is None:
            pieces = _split_literal(line)
            if pieces is not None:
                candidate = lines[:i] + pieces + lines[i + 1 :]
                try:
                    same = ast.dump(ast.parse("\n".join(candidate))) == ast.dump(ast.parse("\n".join(lines)))
                except SyntaxError:
                    same = False
                if same:
                    lines = candidate
                    shift += 1
                    done += 1
                    continue
            left.append(f"{path}:{row}: {len(line)} characters: {line.strip()[:90]}")
            continue
        cut = line.rfind(" ", len(prefix) + 1, LIMIT + 1)
        if cut <= len(prefix):
            left.append(f"{path}:{row}: no space to break at: {line.strip()[:90]}")
            continue
        head, tail = line[:cut].rstrip(), line[cut + 1 :].strip()
        following = lines[i + 1] if i + 1 < len(lines) else None
        same = (
            following is not None
            and following.startswith(prefix)
            and following[len(prefix) :].strip() != ""
            and not following[len(prefix) :].startswith(" ")
            and not _LIST_ITEM.match(following[len(prefix) :])
            and not following.strip().startswith(_OPENING)
            and (comment is None or re.match(r"^\s*#", following) is not None)
            and (comment is not None or row in docstrings)
            and not head.rstrip().endswith((".", ":", "!", "?"))
            and len(prefix + tail + " " + following[len(prefix) :]) <= LIMIT
        )
        lines[i] = head
        if same:
            assert following is not None
            lines[i + 1] = prefix + tail + " " + following[len(prefix) :]
        else:
            lines.insert(i + 1, prefix + tail)
            shift += 1
        done += 1
    return "\n".join(lines), done, left


def long_lines(repo: Path, files: Sequence[str]) -> dict[str, list[int]]:
    """What ruff finds over the line length, by file."""
    done = run(repo, "uv", "run", "ruff", "check", "--select", "E501", "--output-format", "json", "--quiet", *files)
    found: dict[str, list[int]] = collections.defaultdict(list)
    for item in json.loads(done.stdout or "[]"):
        path = Path(item["filename"]).resolve().relative_to(repo).as_posix()
        found[path].append(item["location"]["row"])
    return found


def run(repo: Path, *command: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    if check and done.returncode:
        raise SystemExit(f"{' '.join(command)} failed:\n{done.stdout}\n{done.stderr}")
    return done


def changed_files(repo: Path) -> set[str]:
    out = git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
    names = set()
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        if not entry:
            i += 1
            continue
        status, name = entry[:2], entry[3:]
        names.add(name)
        if status.startswith("R"):
            i += 1  # the rename's source
        i += 1
    return names


def snapshot(repo: Path, paths: Iterable[str]) -> dict[str, bytes]:
    return {p: (repo / p).read_bytes() for p in paths if (repo / p).is_file()}


def ruff_and_rewrap(repo: Path, touched: set[str]) -> None:
    for key in ("X1", "X2"):
        REPORT.rule(key, TITLES[key])
    python = sorted(p for p in touched if p.endswith(".py") and p.startswith(("src/", "tests/", "scripts/")) and (repo / p).is_file())
    if not python:
        return
    before = snapshot(repo, python)
    run(repo, "uv", "run", "ruff", "check", "--fix", "--select", "I001,RUF022", "--quiet", *python)
    # A test rule W drops may leave an import nothing else in its file uses.
    dropped = sorted({path for path, _ in W_DROPPED if path in python})
    if dropped:
        run(repo, "uv", "run", "ruff", "check", "--fix", "--select", "F401", "--quiet", *dropped)
    run(repo, "uv", "run", "ruff", "format", "--quiet", *python)
    for path, data in before.items():
        if (repo / path).read_bytes() != data:
            REPORT.add("X1", path)
    left: list[str] = []
    for _ in range(4):  # a moved word can push the next line over: again, until none moves
        found = long_lines(repo, python)
        moved = 0
        left = []
        for path, rows in sorted(found.items()):
            text = (repo / path).read_text(encoding="utf-8")
            new, n, unresolved = rewrap(path, text, rows)
            left += unresolved
            if n:
                (repo / path).write_text(new, encoding="utf-8")
                REPORT.add("X2", path, n)
                moved += n
        if not moved:
            break
        run(repo, "uv", "run", "ruff", "format", "--quiet", *python)
    for line in left:
        REPORT.warnings.append(f"X2: over {LIMIT} characters, left as it is: {line}")


# -- D: the derived files -------------------------------------------------------------------------

PIN_TASKS = r"""
import json
from tests import test_task_fhir as t
from mendari.standards import task_fhir
old = t.PINNED.read_text(encoding="utf-8")
ascii_ = json.dumps(json.loads(old), indent=2) + "\n" == old
t.PINNED.write_text(json.dumps(task_fhir.task_versions(t.chain_item(), t.BASE), indent=2, ensure_ascii=ascii_) + "\n", encoding="utf-8")
"""
PIN_OUTCOMES = r"""
import ast, inspect, json
from tests import test_outcomes as t
old = t.PINNED.read_text(encoding="utf-8")
ascii_ = json.dumps(json.loads(old), indent=2) + "\n" == old
# The test's own expression: the dict it compares the pin with.
source = inspect.getsource(t.test_an_outcomes_rendering_is_pinned)
tree = ast.parse(source)
compare = next(n for n in ast.walk(tree) if isinstance(n, ast.Compare))
value = eval(compile(ast.Expression(compare.left), "pin", "eval"), vars(t))
t.PINNED.write_text(json.dumps(value, indent=2, ensure_ascii=ascii_) + "\n", encoding="utf-8")
"""
PIN_TERMINOLOGY = r"""
import ast, inspect, json, re, sys
from tests import test_terminology as t
from mendari import __version__
# The test's own computation of what it pins: its body, without its asserts, returning `found`.
source = inspect.getsource(t.test_fixed_resources_are_pinned_to_the_mendari_version)
tree = ast.parse(source)
function = tree.body[0]
function.body = [s for s in function.body if not isinstance(s, ast.Assert)] + [ast.Return(ast.Name("found", ast.Load()))]
function.decorator_list = []
ast.fix_missing_locations(tree)
space = dict(vars(t))
exec(compile(tree, "pin", "exec"), space)
fixture = lambda f: getattr(f, "__wrapped__", f)
served = fixture(t.served)(fixture(t.packs if hasattr(t, "packs") else t.agents)())
found = space[function.name](served)
path = t.__file__
text = open(path, encoding="utf-8").read()
changed = []
for key, pin in found.items():
    pattern = re.compile(r'("' + re.escape(key) + r'": )"([0-9a-f]{16})"')
    hit = pattern.search(text)
    if hit is None:
        print(f"not pinned yet: {key} {pin}", file=sys.stderr)
        continue
    if hit.group(2) != pin:
        changed.append(key)
        text = text[: hit.start(2)] + pin + text[hit.end(2) :]
open(path, "w", encoding="utf-8").write(text)
print(json.dumps(changed))
"""


@dataclass(frozen=True)
class Generator:
    key: str
    title: str
    write: tuple[str, ...]
    check: tuple[str, ...] | None


GENERATORS = (
    Generator("D1", "the demo agent's data (scripts/demo_agent_data.py)", ("uv", "run", "python", "scripts/demo_agent_data.py"), ("uv", "run", "python", "scripts/demo_agent_data.py", "--check")),
    Generator("D2", "the fixture agents' data (tests.support.fixture_agents)", ("uv", "run", "python", "-m", "tests.support.fixture_agents"), ("uv", "run", "python", "-m", "tests.support.fixture_agents", "--check")),
    Generator("D3", "the chart fixture's records (tests.support.fixture_records)", ("uv", "run", "python", "-m", "tests.support.fixture_records"), ("uv", "run", "python", "-m", "tests.support.fixture_records", "--check")),
    Generator("D4", "the triage fixture's US Core records (tests.support.triage_records)", ("uv", "run", "python", "-m", "tests.support.triage_records"), ("uv", "run", "python", "-m", "tests.support.triage_records", "--check")),
    Generator("D5", "the FHIR decisions golden (tests.support.fhir_decisions_golden)", ("uv", "run", "python", "-m", "tests.support.fhir_decisions_golden"), ("uv", "run", "python", "-m", "tests.support.fhir_decisions_golden", "--check")),
    Generator("D6", "the evaluation pages (tests.support.evaluation_pages)", ("uv", "run", "python", "-m", "tests.support.evaluation_pages"), ("uv", "run", "python", "-m", "tests.support.evaluation_pages", "--check")),
    Generator("D7", "shown.json (scripts/shown_fixture.py)", ("uv", "run", "python", "scripts/shown_fixture.py"), ("uv", "run", "python", "scripts/shown_fixture.py", "--check")),
    Generator("D8", "the provider configuration schema (scripts/provider_config_schema.py)", ("uv", "run", "python", "scripts/provider_config_schema.py"), ("uv", "run", "python", "scripts/provider_config_schema.py", "--check")),
    Generator("D10", "task-versions.json (tests/test_task_fhir.py's own expression)", ("uv", "run", "python", "-c", PIN_TASKS), None),
    Generator("D11", "audit-outcome.json (tests/test_outcomes.py's own expression)", ("uv", "run", "python", "-c", PIN_OUTCOMES), None),
    Generator("D12", "the terminology PINNED (tests/test_terminology.py's own computation)", ("uv", "run", "python", "-c", PIN_TERMINOLOGY), None),
)


def regenerate(repo: Path) -> None:
    REPORT.rule("D", TITLES["D"])
    # D9: the starters' copies of the kit, byte for byte (tests/test_agent_starters.py).
    kit = (repo / "src" / "mendari" / "agent_kit.py").read_bytes()
    copies = [folder / "mendari_agent_kit.py" for folder in sorted((repo / "examples").glob("*-agent"))]
    stale = [copy for copy in copies if copy.is_file() and copy.read_bytes() != kit]
    for copy in stale:
        copy.write_bytes(kit)
    REPORT.add("D", f"D9 the starters' kit (src/mendari/agent_kit.py copied): {len(stale)} of {len(copies)} copies changed")
    for generator in GENERATORS:
        before = {p: (repo / p).read_bytes() for p in changed_files(repo) | set(tracked(repo)) if (repo / p).is_file() and not p.startswith(("notes/", "licenses/"))}
        done = run(repo, *generator.write)
        if done.returncode:
            REPORT.warnings.append(f"{generator.key} {generator.title}: failed: {(done.stderr or done.stdout).strip()[-800:]}")
            continue
        after = [p for p, data in before.items() if (repo / p).is_file() and (repo / p).read_bytes() != data]
        REPORT.add("D", f"{generator.key} {generator.title}: {len(after)} files changed {sorted(after)[:6]}")
        if generator.key == "D12" and done.stdout.strip():
            REPORT.notes.append(f"D12 re-pinned: {done.stdout.strip()}")
        if generator.check:
            checked = run(repo, *generator.check)
            if checked.returncode:
                REPORT.warnings.append(f"{generator.key} --check fails after writing: {(checked.stderr or checked.stdout).strip()[-500:]}")


# -- Q: the residue -------------------------------------------------------------------------------


NAMING = re.compile(
    r"(?i)\bwave 6\b|\bwave-6\b|keeps? (?:its|their) names?|\bon the wire\b|\bwire names?\b"
    r"|until the records|renamed with the|its own versioned change|the plan's wave|\bold word\b"
)


def residue(repo: Path) -> None:
    """Every remaining `(?i)pack(?!ag|et)` once 4.13's words are out: code in full, prose by
    directory."""
    code: list[str] = []
    prose: collections.Counter[str] = collections.Counter()
    skipped: collections.Counter[str] = collections.Counter()
    for path in tracked(repo):
        if not (repo / path).is_file():
            continue
        if excluded(path):
            text = read_text(repo, path) if Path(path).suffix.lower() not in BINARY_SUFFIXES else None
            if text and RESIDUE.search(KEPT_WORDS.sub("", text)):
                skipped[path.split("/")[0] + "/" if "/" in path else path] += 1
            continue
        text = read_text(repo, path)
        if text is None:
            continue
        cleaned = KEPT_WORDS.sub(lambda m: "_" * len(m.group(0)), text)
        if not RESIDUE.search(cleaned):
            continue
        spans = prose_spans(path, text)
        starts = [s.start for s in spans]
        lines = _line_starts(text)
        folder = "/".join(path.split("/")[:3]) if path.startswith(("src/", "tests/")) else path.split("/")[0]
        for found in RESIDUE.finditer(cleaned):
            index = bisect.bisect_right(starts, found.start()) - 1
            in_prose = index >= 0 and spans[index].start <= found.start() < spans[index].end
            line = bisect.bisect_right(lines, found.start())
            if in_prose or path in NAMES_ONLY:
                prose[folder] += 1
            else:
                code.append(f"{path}:{line}: {text.splitlines()[line - 1].strip()[:150]}")
    print("\n== residue: code (identifiers, keys, strings), in full ==")
    for line in code:
        print(f"  {line}")
    print(f"  ({len(code)} in all)")
    print("\n== residue: prose (comments, docstrings, docs) and the vocabulary tests, by directory ==")
    for folder, n in sorted(prose.items()):
        print(f"  {n:6}  {folder}")
    print(f"  ({sum(prose.values())} in all)")
    print("\n== not read by the codemod, with the word (files) ==")
    for folder, n in sorted(skipped.items()):
        print(f"  {n:6}  {folder}")
    print("\n== sentences about names and their history, which the rename may have made false ==")
    said = 0
    for path in tracked(repo):
        if excluded(path) or not (repo / path).is_file():
            continue
        text = read_text(repo, path)
        if text is None or not NAMING.search(text):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if NAMING.search(line):
                said += 1
                print(f"  {path}:{number}: {line.strip()[:150]}")
    print(f"  ({said} in all)")


# -- main -----------------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="counts and gates; write nothing")
    parser.add_argument("--residue", action="store_true", help="only the residue report")
    parser.add_argument("--force", action="store_true", help="write even when a gate finds collisions")
    parser.add_argument("--no-prose", action="store_true", help="leave every bare word in prose")
    parser.add_argument("--no-regenerate", action="store_true", help="skip ruff and rule D")
    parser.add_argument("--doubts", action="store_true", help="list each sentence rule P left")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    if not (repo / "src" / "mendari").is_dir():
        raise SystemExit(f"{repo}: not a Mendari checkout")
    if args.residue:
        residue(repo)
        return 0
    if git(repo, "status", "--porcelain").strip():
        raise SystemExit(f"{repo}: the tree is not clean: commit or stash first")
    scratch = Path(os.environ.get("CODEMOD_SCRATCH") or tempfile.mkdtemp(prefix="codemod-"))
    original, texts, moves = transform(repo, scratch, force=args.force, prose=not args.no_prose)
    changed = sorted(p for p in texts if texts[p] != original[p])
    print(f"{len(changed)} files change, {len(moves)} move")
    REPORT.moved(moves)
    if args.dry_run:
        REPORT.print()
        if args.doubts:
            print("\n== prose left for the sweep (rule P2) ==")
            for line in REPORT.doubts:
                print(f"  {line}")
        return 0
    for path in changed:
        (repo / path).write_text(texts[path], encoding="utf-8")
    for old, new in moves.items():
        (repo / new).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", old, new)
    touched = {moves.get(p, p) for p in changed} | set(moves.values())
    if not args.no_regenerate:
        ruff_and_rewrap(repo, touched)
        regenerate(repo)
        ruff_and_rewrap(repo, {p for p in changed_files(repo) if p.endswith(".py")})
    REPORT.print()
    if args.doubts:
        print("\n== prose left for the sweep (rule P2) ==")
        for line in REPORT.doubts:
            print(f"  {line}")
    residue(repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
