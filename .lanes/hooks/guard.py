#!/usr/bin/env python3
"""The implementer's guard: a PreToolUse hook that stops a lane loosening its own checks.

It denies, by exiting 2 with the reason on stderr, a write to CI, coverage, lint
or Lanes config, and a push to the default branch. The implementer's frontmatter
sets `onFailure: "block"`, so a guard that crashes or is missing blocks the tool
call too. The gates remain the certifier; the guard only stops an honest lane
from going the wrong way, so a shell command that names config is denied unless
it plainly only reads it.
"""

import json
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

# The paths the gates refuse to see changed without the allow-config label, kept in step with `.lanes/gates.py`.
CONFIG_DIRS = [".claude/", ".github/", ".lanes/"]
CONFIG_NAMES = [
    ".coveragerc",
    ".flake8",
    ".isort.cfg",
    ".mypy.ini",
    ".pre-commit-config.yaml",
    ".pylintrc",
    ".ruff.toml",
    "mypy.ini",
    "pylintrc",
    "pyrightconfig.json",
    "pytest.ini",
    "ruff.toml",
    "setup.cfg",
    "tox.ini",
]
CONFIG_TOOLS = ["black", "coverage", "flake8", "isort", "mypy", "pylint", "pyright", "pytest", "ruff"]
# A config path anywhere in a shell word, even inside a quoted `python -c` program. A shell
# command can't edit only pyproject.toml's tool sections, so naming pyproject.toml counts too.
CONFIG_WORDS = [name.rstrip("/") for name in CONFIG_DIRS] + CONFIG_NAMES + ["pyproject.toml"]
MENTIONS_CONFIG = re.compile(r"(?<![\w.-])(?:" + "|".join(map(re.escape, CONFIG_WORDS)) + r")(?![\w.-])")
CONTROL = {"&&", "||", ";", "|", "&", "(", ")", "\n"}
REDIRECTS = {">", ">>", ">|", "<", "<<", ">&", "<&", "&>"}
# Commands that only read the files they are given, whatever their options.
READERS = {"cat", "head", "tail", "less", "more", "grep", "rg", "ls", "wc", "diff", "stat", "file"}
# Commands that run their arguments as another command.
WRAPPERS = {"env", "command", "exec", "nice", "nohup", "time", "xargs"}
SHELLS = {"bash", "sh", "zsh", "dash"}
READING_GIT = {"diff", "log", "show", "status", "blame", "ls-files", "grep", "cat-file"}
# git's own options that take a value, before the subcommand.
GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True).stdout.strip()


def repo_path(file_path: Path) -> str | None:
    """`file_path` relative to the root of the git repository holding it, or None outside one."""
    existing = next(directory for directory in file_path.parents if directory.is_dir())
    root = git(existing, "rev-parse", "--show-toplevel")
    return file_path.relative_to(Path(root).resolve()).as_posix() if root else None


def is_config(path: str) -> bool:
    return path.startswith(tuple(CONFIG_DIRS)) or Path(path).name in CONFIG_NAMES


def tool_sections(text: str) -> dict[str, Any]:
    try:
        tools: dict[str, Any] = tomllib.loads(text).get("tool", {})
    except tomllib.TOMLDecodeError:
        return {}
    return {tool: tools.get(tool) for tool in CONFIG_TOOLS}


def edited(file_path: Path, tool: str, tool_input: dict[str, Any]) -> str:
    """The file's text once a Write, Edit or MultiEdit has run."""
    if tool == "Write":
        return str(tool_input.get("content", ""))
    text = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    for edit in tool_input.get("edits") or [tool_input]:
        old, new = str(edit.get("old_string", "")), str(edit.get("new_string", ""))
        text = text.replace(old, new) if edit.get("replace_all") else text.replace(old, new, 1)
    return text


def file_denial(tool: str, tool_input: dict[str, Any], cwd: Path) -> str | None:
    file_path = (cwd / str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")).resolve()
    path = repo_path(file_path)
    if path is None:
        return None
    if is_config(path):
        return f"{path} is CI, coverage, lint or Lanes config; a lane may not change it"
    if path == "pyproject.toml" and tool != "NotebookEdit":
        before = tool_sections(file_path.read_text(encoding="utf-8")) if file_path.exists() else tool_sections("")
        after = tool_sections(edited(file_path, tool, tool_input))
        changed = [section for section in CONFIG_TOOLS if before[section] != after[section]]
        if changed:
            return f"pyproject.toml [tool.{changed[0]}] is lint, test or coverage config; a lane may not change it"
    return None


def segments(command: str) -> list[list[str]]:
    """Each simple command in `command` as its words, redirection operators kept as words."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"  # a newline separates commands, so it must come back as a word
    lexer.whitespace_split = True
    found: list[list[str]] = [[]]
    for word in lexer:
        if word in CONTROL:
            found.append([])
        else:
            found[-1].append(word)
    return [words for words in found if words]


def unwrapped(words: list[str]) -> list[str]:
    """The command `words` runs, without `NAME=value` assignments, wrappers such as `env`, or a path to it."""
    while words and (re.match(r"^\w+=", words[0]) or Path(words[0]).name in WRAPPERS):
        words = words[1:]
    return [Path(words[0]).name, *words[1:]] if words else []


def inner_command(words: list[str]) -> str | None:
    """The command a `bash -c` or `eval` runs, if `words` is one."""
    if words[:1] == ["eval"]:
        return shlex.join(words[1:])
    if words[:1] and words[0] in SHELLS and "-c" in words[:-1]:
        return words[words.index("-c") + 1]
    return None


def without_redirects(words: list[str]) -> list[str]:
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word in REDIRECTS:
            skip = True
            if kept and kept[-1].isdigit():
                kept.pop()  # the descriptor in `2>`
        else:
            kept.append(word)
    return kept


def git_subcommand(words: list[str]) -> list[str] | None:
    """The git subcommand and its arguments when `words` runs git, else None."""
    if words[:1] != ["git"]:
        return None
    rest = words[1:]
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in GIT_VALUE_OPTIONS else rest[1:]
    return rest


def default_branch(cwd: Path) -> str:
    """The branch `origin/HEAD` points at, or main when there is no origin."""
    return git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/") or "main"


def pushed_branches(arguments: list[str], cwd: Path) -> list[str] | None:
    """The branches a push updates on the remote, or None when it updates every branch."""
    if {"--all", "--mirror"} & set(arguments):
        return None
    positional = [word for word in arguments if not word.startswith("-")]
    current = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if len(positional) < 2:
        return [current]  # no refspec: the current branch goes to its namesake
    branches = []
    for refspec in positional[1:]:
        destination = refspec.lstrip("+").split(":")[-1]
        branches.append(current if destination == "HEAD" else destination.removeprefix("refs/heads/"))
    return branches


def only_reads(words: list[str]) -> bool:
    if any(word in REDIRECTS and word not in ("<", "<<", "<&") for word in words):
        return False
    words = without_redirects(words)
    if not words:
        return True
    subcommand = git_subcommand(words)
    if subcommand is not None:
        return subcommand[:1] != [] and subcommand[0] in READING_GIT
    return words[0] in READERS


def shell_denial(command: str, cwd: Path) -> str | None:
    try:
        found = segments(command)
    except ValueError:
        return f"`{command}` could not be parsed, so the guard cannot tell what it changes"
    default = default_branch(cwd)
    for words in found:
        words = unwrapped(words)
        if not words:
            continue
        inner = inner_command(words)
        if inner is not None:
            reason = shell_denial(inner, cwd)
            if reason is not None:
                return reason
            continue
        shown = shlex.join(words)
        subcommand = git_subcommand(without_redirects(words))
        if subcommand is not None and subcommand[:1] == ["push"]:
            branches = pushed_branches(subcommand[1:], cwd)
            if branches is None or default in branches:
                return f"`{shown}` pushes to {default}; a lane pushes only its own branch"
        if any(MENTIONS_CONFIG.search(word) for word in words) and not only_reads(words):
            return f"`{shown}` may change CI, coverage, lint or Lanes config; a lane may not change it"
    return None


def denial(tool: str, tool_input: dict[str, Any], cwd: Path) -> str | None:
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return file_denial(tool, tool_input, cwd)
    if tool == "Bash":
        return shell_denial(str(tool_input.get("command", "")), cwd)
    return None


def main() -> int:
    given = json.load(sys.stdin)
    reason = denial(given["tool_name"], given.get("tool_input") or {}, Path(given.get("cwd") or "."))
    if reason is None:
        return 0
    print(f"Lanes guard: {reason}. Only a human changes it, under allow-config; say in the pull request body what you needed.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
