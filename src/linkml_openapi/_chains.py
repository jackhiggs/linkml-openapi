"""Shared helpers for parent-chain detection and path-template parsing.

Used by both the OpenAPI generator and the Spring emitter to compute the
deep-nested URL chain for a leaf class. Pure: SchemaView in, dataclasses
out. Path-style and `path_id` rendering stay in the calling generator
(injected via callbacks) because they need access to that generator's
already-implemented path-style state.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from linkml_runtime.linkml_model import ClassDefinition, SlotDefinition
from linkml_runtime.utils.schemaview import SchemaView

PATH_TEMPLATE_PLACEHOLDER_RE: re.Pattern[str] = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class ChainHop:
    """One hop of a deep-nested URL chain."""

    parent_class: str
    parent_id_param_name: str  # honors openapi.path_id
    parent_path_segment: str  # honors openapi.path / path_style
    slot_segment: str  # honors openapi.path_segment / path_style
    parent_id_slot: SlotDefinition  # for typing the path param


def _parse_csv(raw: str) -> list[str]:
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def build_parent_chains_index(
    sv: SchemaView,
    *,
    resource_classes: set[str],
    excluded_classes: set[str],
    is_slot_excluded: Callable[[SlotDefinition], bool],
    get_slot_annotation: Callable[..., str | None],
    induced_slots: Callable[[str], list[SlotDefinition]],
) -> dict[str, list[list[tuple[str, str]]]]:
    """Index every chain of `(parent_class, slot_name)` leading to each class.

    Result is keyed by leaf class name. Each value is a list of chains;
    each chain is a list of `(parent_class, slot_name)` tuples ordered
    root -> direct parent. A class with no parents in the relationship
    graph is absent from the dict.

    Cycles are detected (the recursion tracks visited classes on the
    current path) so a graph with `A.bs: list[B]` and `B.as: list[A]`
    doesn't blow up.
    """
    direct_parents: dict[str, list[tuple[str, str]]] = {}
    for parent_name in sv.all_classes():
        if parent_name in excluded_classes:
            continue
        if parent_name not in resource_classes:
            continue
        parent_cls = sv.get_class(parent_name)
        for slot in induced_slots(parent_name):
            if not slot.multivalued:
                continue
            if is_slot_excluded(slot):
                continue
            target = slot.range
            if not target or sv.get_class(target) is None:
                continue
            if target == parent_name:
                continue  # self-loop
            nested_ann = get_slot_annotation(parent_cls, slot.name, "openapi.nested")
            if nested_ann is not None and str(nested_ann).lower() != "true":
                continue
            direct_parents.setdefault(target, []).append((parent_name, slot.name))

    index: dict[str, list[list[tuple[str, str]]]] = {}

    def walk(leaf: str, on_path: tuple[str, ...]) -> list[list[tuple[str, str]]]:
        chains: list[list[tuple[str, str]]] = []
        for parent_name, slot_name in direct_parents.get(leaf, []):
            if parent_name in on_path:
                continue
            upper = walk(parent_name, on_path + (parent_name,))
            if not upper:
                chains.append([(parent_name, slot_name)])
            else:
                for u in upper:
                    chains.append(u + [(parent_name, slot_name)])
        return chains

    for cls_name in list(direct_parents.keys()):
        chains = walk(cls_name, (cls_name,))
        if chains:
            index[cls_name] = chains
    return index


def parent_path_segments(annotation: str) -> list[tuple[str | None, str]]:
    """Parse an `openapi.parent_path` annotation into per-hop matchers.

    Each `/`-separated segment is either `slot_name` (parent class
    implied, must be unambiguous) or `ClassName.slot_name`
    (class-qualified). Returns `[(class_or_none, slot_name), ...]`.
    """
    segments: list[tuple[str | None, str]] = []
    for raw in annotation.strip().split("/"):
        raw = raw.strip()
        if not raw:
            continue
        if "." in raw:
            cls_name, slot_name = raw.split(".", 1)
            segments.append((cls_name.strip() or None, slot_name.strip()))
        else:
            segments.append((None, raw))
    return segments


def canonical_parent_chain(
    class_name: str,
    index: dict[str, list[list[tuple[str, str]]]],
    parent_path_annotation: str | None,
) -> list[tuple[str, str]]:
    """Pick the canonical chain for `class_name`.

    * 0 chains in index -> returns `[]`.
    * 1 chain -> returns it.
    * >1 chains -> reads `openapi.parent_path` and matches.
    """
    chains = index.get(class_name, [])
    if not chains:
        return []
    if len(chains) == 1:
        return chains[0]
    candidates_qualified = ["/".join(f"{p}.{s}" for p, s in chain) for chain in chains]
    if parent_path_annotation:
        wanted = parent_path_segments(parent_path_annotation)
        for chain in chains:
            if len(chain) != len(wanted):
                continue
            if all(
                (cls_q is None or cls_q == p) and slot_q == s
                for (cls_q, slot_q), (p, s) in zip(wanted, chain)
            ):
                return chain
        raise ValueError(
            f"Class {class_name!r} declares "
            f"`openapi.parent_path: {parent_path_annotation!r}` but no "
            f"matching chain exists. Candidates: {candidates_qualified}."
        )
    raise ValueError(
        f"Class {class_name!r} is reachable via multiple parent chains. "
        f"Pick one with the `openapi.parent_path` class annotation, "
        f"e.g. `openapi.parent_path: {candidates_qualified[0]!r}`. "
        f"Candidates: {candidates_qualified}."
    )


def parse_path_param_sources(class_name: str, raw: str) -> dict[str, tuple[str, str]]:
    """Parse `openapi.path_param_sources` into `{name: (Class, slot)}`."""
    sources: dict[str, tuple[str, str]] = {}
    for raw_entry in _parse_csv(raw):
        if ":" not in raw_entry:
            raise ValueError(
                f"Class {class_name!r} has malformed "
                f"`openapi.path_param_sources` entry {raw_entry!r}: "
                "expected `name:Class.slot`."
            )
        name, source = (s.strip() for s in raw_entry.split(":", 1))
        if "." not in source:
            raise ValueError(
                f"Class {class_name!r} has malformed source "
                f"{source!r} for parameter {name!r}: expected "
                "`Class.slot`."
            )
        src_class, src_slot = (s.strip() for s in source.split(".", 1))
        if not name or not src_class or not src_slot:
            raise ValueError(
                f"Class {class_name!r} has empty token in "
                f"`openapi.path_param_sources` entry {raw_entry!r}."
            )
        if name in sources:
            raise ValueError(
                f"Class {class_name!r} declares duplicate path "
                f"parameter {name!r} in `openapi.path_param_sources`."
            )
        sources[name] = (src_class, src_slot)
    return sources


def render_chain_hops(
    sv: SchemaView,
    chain: list[tuple[str, str]],
    *,
    class_path_id_name: Callable[[str], str],
    get_path_segment: Callable[[ClassDefinition], str],
    render_slot_segment: Callable[[ClassDefinition | None, SlotDefinition], str],
    identifier_slot: Callable[[str], SlotDefinition | None],
    induced_slots_by_name: Callable[[str], dict[str, SlotDefinition]],
) -> list[ChainHop]:
    """Translate a `[(parent_class, slot_name), ...]` chain into ChainHops.

    Each hop carries the rendered URL segments and the typed identifier
    slot for the parent. The calling generator decides how to format the
    final URL string and parameter list — this helper just supplies the
    typed pieces.
    """
    hops: list[ChainHop] = []
    for parent_name, slot_name in chain:
        id_slot = identifier_slot(parent_name)
        if id_slot is None:
            raise ValueError(
                f"Parent class {parent_name!r} in a deep nested chain "
                "has no identifier slot — can't synthesise its path "
                "parameter."
            )
        parent_cls = sv.get_class(parent_name)
        slot_def = induced_slots_by_name(parent_name).get(slot_name)
        slot_seg = render_slot_segment(parent_cls, slot_def) if slot_def is not None else slot_name
        hops.append(
            ChainHop(
                parent_class=parent_name,
                parent_id_param_name=class_path_id_name(parent_name),
                parent_path_segment=get_path_segment(parent_cls),
                slot_segment=slot_seg,
                parent_id_slot=id_slot,
            )
        )
    return hops
