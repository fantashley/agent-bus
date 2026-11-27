"""Review prompt text from the head under review, for a repository that opts in (#138).

A review of a change to agent-bus's own prompts otherwise shows the reviewers the installed
prompts, so they can judge the new text only from the diff.  With the review's repo listed in
``self_hosted_prompts``, a review pass takes the text of the constants in ``NAMES`` from the
head's ``src/agent_bus/prompts.py`` instead, read from the pinned head's read-only snapshot.

The head's code is never imported or run: the file is parsed with ``ast`` and only top-level
assignments of string literals (and ``+`` of literals and names resolved earlier) are read.
Everything else stays installed: the worker, the adapters (and their headless preambles), the
verdict format the worker parses, and the database.  If any name can't be read that way, the
pass uses the installed prompts, all of them, and says why.
"""

from __future__ import annotations

import ast
import os
import stat
import string
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from . import prompts

PATH = "src/agent_bus/prompts.py"
MAX_BYTES = 256 * 1024
MAX_VALUE = 128 * 1024  # one constant's text, so `A = B + B` chains can't blow up
MAX_TOTAL = 1024 * 1024  # all the constants' text together

# The review turn's prompt text (worker.review_round and its helpers).  Not here, so always
# installed: the verdict format (VERDICT_FORMAT, VERDICT_FOLLOWUP, FORMAT_LABEL and its
# version: the installed worker parses the verdict), the adapters' headless preambles, the
# discussion, debrief and repair prompts, and the functions.
NAMES = (
    "REVIEW", "REVIEW_FOLLOWUP", "REVIEW_PUSHBACK",
    "SEVERITY", "FIRST_PASS", "CONVERGE", "ALREADY_APPROVED", "APPROVED_OTHERS", "APPROVED_AUTHOR",
    "CONFIRM", "CONFIRM_LEAD", "EARLIER_FINDINGS", "BUDGET_SPENT", "PUSHBACK_NOTE", "REBASE_NOTE",
    "PURE_REBASE", "RANGE_DIFF", "TIME_LIMIT", "REVIEW_ONLY",
    "READ_HEAD_SHELL", "READ_HEAD_GIT", "READ_HEAD_JJ", "SNAPSHOT", "SNAPSHOT_SHELL", "READ_TIP_SHELL",
    "SNAPSHOT_BRIEF", "HISTORY_BRIEF", "SNAPSHOT_SPARSE", "SNAPSHOT_LIST", "SNAPSHOT_DELTA",
    "SNAPSHOT_HISTORY", "SNAPSHOT_NONE", "READ_HEAD_FILES", "USAGES", "USAGES_CUT", "USAGES_OUTSIDE",
)


class NotLoadable(Exception):
    """Why the head's prompts can't be used: a short fixed reason, never the head's text."""


def fields(text: str) -> Counter:
    """The str.format fields of ``text``, with how often each appears.  A field with a
    conversion or a format spec, or text str.format can't parse, is refused: the installed
    templates use neither."""
    out: Counter = Counter()
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as e:
        raise NotLoadable("unbalanced braces") from e
    for _literal, name, spec, conversion in parsed:
        if name is None:
            continue
        if spec or conversion:
            raise NotLoadable("a field with a conversion or format spec")
        out[name] += 1
    return out


# The top-level statements a loadable prompts.py may have: a statement of any other kind
# (an if, try, loop, with, match, del, augmented assignment, expression...) could rebind a
# name in ways a plain read of the assignments would miss, so the file isn't loadable.
TOP_LEVEL = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.AsyncFunctionDef,
             ast.ClassDef)
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _bindings(node: ast.AST, names: list[str]) -> None:
    """The module-scope names ``node`` binds: assignment and walrus targets, imports, def
    and class names (their decorators, defaults and bases too, but not their bodies)."""
    if isinstance(node, DEFS):
        names.append(node.name)
    elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        names.append(node.id)
    elif isinstance(node, ast.alias):
        names.append((node.asname or node.name).split(".")[0])
    for name, child in ast.iter_fields(node):
        if isinstance(node, DEFS) and name == "body":
            continue
        for c in child if isinstance(child, list) else [child]:
            if isinstance(c, ast.AST):
                _bindings(c, names)


def _bound(tree: ast.Module) -> Counter:
    """How many times each module-scope name is bound, counting a function's ``global``
    declaration of it as one more."""
    for i, stmt in enumerate(tree.body):
        docstring = i == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
        if not docstring and not isinstance(stmt, TOP_LEVEL):
            raise NotLoadable(f"a top-level {type(stmt).__name__} statement (only imports, assignments "
                              "and definitions are read)")
        if isinstance(stmt, ast.ImportFrom) and any(a.name == "*" for a in stmt.names):
            raise NotLoadable("a wildcard import")
    names: list[str] = []
    _bindings(tree, names)
    names += [n for node in ast.walk(tree) if isinstance(node, ast.Global) for n in node.names]
    return Counter(names)


def _value(node: ast.expr, env: dict[str, str]) -> str | None:
    """A string literal, a name resolved earlier, or ``+`` of those; None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        out = node.value
    elif isinstance(node, ast.Name):
        out = env.get(node.id)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _value(node.left, env), _value(node.right, env)
        out = left + right if left is not None and right is not None else None
    else:
        return None
    return out if out is not None and len(out) <= MAX_VALUE else None


def parse(source: str) -> dict[str, str]:
    """The NAMES' text from a prompts.py source, without running it.  Raises NotLoadable."""
    try:
        return _parse(source)
    except RecursionError as e:  # a deeply nested expression or a long + chain
        raise NotLoadable("an expression is nested too deeply") from e


def _parse(source: str) -> dict[str, str]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        raise NotLoadable("it is not valid Python") from e
    counts = _bound(tree)
    env: dict[str, str] = {}
    total = 0
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if isinstance(target, ast.Name) and counts[target.id] == 1:
            text = _value(value, env)
            if text is not None:
                total += len(text)
                if total > MAX_TOTAL:
                    raise NotLoadable(f"its strings add up to over {MAX_TOTAL // 1024} KiB")
                env[target.id] = text
    out = {}
    for name in NAMES:
        text = env.get(name)
        if text is None:
            raise NotLoadable(f"{name} is missing or not a string literal")
        if not text.strip() or "\0" in text or not _utf8(text):
            raise NotLoadable(f"{name} is empty, or has a NUL or a character UTF-8 can't encode")
        if fields(text) != fields(getattr(prompts, name)):
            raise NotLoadable(f"{name} takes other fields than the installed worker fills")
        out[name] = text
    return out


def _utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def load(snap) -> dict[str, str]:
    """The NAMES' text from the head's prompts.py in its read-only snapshot ``snap``
    (snapshot.Snapshot).  Raises NotLoadable."""
    if snap is None or not snap.path:
        raise NotLoadable("there is no read-only copy of the head")
    if PATH not in snap.files:
        raise NotLoadable(f"the head's read-only copy has no {PATH}")
    try:
        fd = os.open(os.path.join(snap.path, PATH), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise NotLoadable(f"{PATH} is not a regular file")
            data = f.read(MAX_BYTES + 1)
    except OSError as e:
        raise NotLoadable(f"{PATH} could not be read") from e
    if len(data) > MAX_BYTES:
        raise NotLoadable(f"{PATH} is over {MAX_BYTES // 1024} KiB")
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise NotLoadable(f"{PATH} is not UTF-8") from e
    return parse(source)


@dataclass
class Active:
    """The prompt set of a review pass in an opted-in repository: the head's texts (empty
    when it fell back to the installed ones) and the line saying which it uses."""
    texts: dict[str, str] = field(default_factory=dict)
    note: str = ""


_ACTIVE: ContextVar[Active | None] = ContextVar("agent_bus_head_prompts", default=None)


class _View:
    """The prompts module as the worker sees it: a NAMES constant comes from the active
    pass's head prompts when it has them, everything else from the installed module."""

    def __getattr__(self, name: str):
        active = _ACTIVE.get()
        if active is not None and name in active.texts:
            return active.texts[name]
        return getattr(prompts, name)


view = _View()


def note() -> str:
    """The active pass's line on which prompts it uses; "" when its repo doesn't opt in."""
    active = _ACTIVE.get()
    return active.note if active is not None else ""


@contextmanager
def using(active: Active | None) -> Iterator[None]:
    token = _ACTIVE.set(active)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
