"""``parentUuid`` DAG construction over a session's records.

Critical finding from the corpus: ``parentUuid`` describes a *DAG with real
branches*, not a flat list. Sampled sessions had 75, 256, and 11 distinct
parent uuids that each had ≥2 children — that's rewinds, edits, and
TaskCreate sidechain forks. A naive ``for line in file`` linear walk misses
all of them.

This module:

* Builds a parent→children index keyed by ``uuid``.
* Identifies roots (``parent_uuid is None`` or parent not in the set) and
  leaves (no children).
* Linearizes the *main* branch from a chosen leaf by walking parents up to a
  root, then reversing.
* Reports branch points so callers can render or fold them.

Records without a ``uuid`` (``permission-mode``, ``ai-title``,
``last-prompt``, some ``attachment``s) are intentionally excluded from the
DAG — they're session-scoped metadata, not part of the conversation graph.
``isSidechain`` records are also excluded because their parent lives in a
separate sidechain transcript; use :mod:`lib.sidechain` for those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lib.schema import Record

LinearizePolicy = Literal["deepest", "latest_ts", "specified_leaf"]


@dataclass
class Dag:
    """Parent-uuid DAG over a single session's main-thread records.

    Attributes:
        nodes:    ``uuid -> Record``.
        children: ``parent_uuid -> [child_uuid, ...]`` (preserves file order).
        roots:    uuids whose ``parent_uuid`` is ``None`` or unresolved.
        leaves:   uuids with no children.
    """

    nodes: dict[str, Record] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    leaves: list[str] = field(default_factory=list)


def _eligible(r: Record) -> bool:
    """Records that participate in the conversation DAG."""
    if r.uuid is None:
        return False
    if r.is_sidechain:
        return False
    # The DAG is the conversation thread; metadata-only types don't belong.
    return r.type in {"assistant", "user", "system"}


def build_dag(records: list[Record]) -> Dag:
    """Build a :class:`Dag` from an in-memory record list.

    Input is the *full* record stream for one session file — we filter
    internally rather than pushing that responsibility to callers. Order of
    ``records`` is preserved within each parent's child list, which matters
    for ``linearize_main_branch``.
    """

    dag = Dag()
    for r in records:
        if not _eligible(r):
            continue
        # Last-write-wins on collisions (shouldn't happen, but be safe).
        dag.nodes[r.uuid] = r  # type: ignore[index]

    for r in records:
        if not _eligible(r):
            continue
        uuid = r.uuid  # type: ignore[assignment]
        if r.parent_uuid is None or r.parent_uuid not in dag.nodes:
            dag.roots.append(uuid)  # type: ignore[arg-type]
            continue
        dag.children.setdefault(r.parent_uuid, []).append(uuid)  # type: ignore[arg-type]

    dag.leaves = [u for u in dag.nodes if u not in dag.children]
    return dag


def find_branches(dag: Dag) -> list[tuple[str, list[str]]]:
    """Parents that have more than one child — the rewind/edit/branch points.

    Returns ``[(parent_uuid, [child_uuid, ...]), ...]`` in insertion order.
    """

    return [(p, kids) for p, kids in dag.children.items() if len(kids) > 1]


def _walk_up(dag: Dag, leaf_uuid: str) -> list[Record]:
    """Walk parents from ``leaf_uuid`` up to a root, return chronological path."""
    path: list[Record] = []
    cur: str | None = leaf_uuid
    seen: set[str] = set()  # cycle guard, defensive
    while cur is not None:
        if cur in seen:
            break
        seen.add(cur)
        rec = dag.nodes.get(cur)
        if rec is None:
            break
        path.append(rec)
        cur = rec.parent_uuid if rec.parent_uuid in dag.nodes else None
    path.reverse()
    return path


def _leaf_depths(dag: Dag) -> dict[str, int]:
    """Compute depth (== root-to-leaf hop count) for each leaf.

    Memoizes per uuid; safe against the (defensive) cycle case.
    """
    depth: dict[str, int] = {}

    def _d(u: str, stack: set[str]) -> int:
        if u in depth:
            return depth[u]
        if u in stack:
            return 0  # cycle guard
        rec = dag.nodes.get(u)
        if rec is None:
            return 0
        parent = rec.parent_uuid
        if parent is None or parent not in dag.nodes:
            depth[u] = 1
            return 1
        stack.add(u)
        d = _d(parent, stack) + 1
        stack.discard(u)
        depth[u] = d
        return d

    for leaf in dag.leaves or list(dag.nodes.keys()):
        _d(leaf, set())
    return depth


def linearize_main_branch(
    dag: Dag,
    leaf_uuid: str | None = None,
    policy: LinearizePolicy = "deepest",
) -> list[Record]:
    """Walk parents from a chosen leaf up to its root, return chronological path.

    The corpus reality: ``--resume`` cycles produce multiple roots and many
    leaves per session. The latest-ts leaf often lives in a shallow side
    sub-tree (depth=3) while the *actual* long-running conversation thread is
    150 records deep in a different sub-tree. Picking by ``ts`` alone hides
    the main conversation.

    Policies:

    * ``"deepest"`` *(default)* — pick the leaf with the longest root-to-leaf
      path. Best for "what was the main conversation in this session?".
      Ties broken by latest ``ts``, then by ``byte_offset`` (file order).
    * ``"latest_ts"`` — pick the leaf with the latest ``ts``. Best for
      "what is the user looking at right now?" (the live rewind tip).
    * ``"specified_leaf"`` — caller must pass ``leaf_uuid`` and we walk up
      from there. Implicitly selected whenever ``leaf_uuid`` is non-None,
      regardless of ``policy``.

    Trade-off summary: ``deepest`` favors completeness of narrative;
    ``latest_ts`` favors recency of activity. They diverge specifically when
    the user has rewound (``--resume`` from a mid-session message), creating
    a short fresh branch that is more recent than the original deep branch.
    """

    if not dag.nodes:
        return []

    if leaf_uuid is not None:
        if leaf_uuid not in dag.nodes:
            return []
        return _walk_up(dag, leaf_uuid)

    leaves = dag.leaves or list(dag.nodes.keys())
    if not leaves:
        return []

    if policy == "latest_ts":
        chosen = max(
            leaves,
            key=lambda u: (
                dag.nodes[u].ts.timestamp() if dag.nodes[u].ts else 0.0,
                dag.nodes[u].byte_offset,
            ),
        )
    elif policy == "specified_leaf":
        # leaf_uuid was None — caller asked for explicit-leaf semantics but
        # supplied no leaf. Empty path is the safest answer.
        return []
    else:  # "deepest" (default) — and any unknown value falls through here.
        depths = _leaf_depths(dag)
        chosen = max(
            leaves,
            key=lambda u: (
                depths.get(u, 0),
                dag.nodes[u].ts.timestamp() if dag.nodes[u].ts else 0.0,
                dag.nodes[u].byte_offset,
            ),
        )

    return _walk_up(dag, chosen)
