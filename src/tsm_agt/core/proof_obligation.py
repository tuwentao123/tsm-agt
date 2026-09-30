"""Proof Obligation: one atomic requirement a Task Contract must prove.

Design constraints (see 《任务证明闭环实施SPEC.md》 §3.2):

* **Pure** -- no IO, no ``await``, no mutation. Independently unit-testable.
* **Deterministic, content-sensitive identity** (AG-10). The id is derived from
  the stable contract reference, the predicate, a digest of the *normalized*
  requirement text, and a **version-free** selector. A requirement whose meaning
  changes must get a new id; a file whose content changes must **not** -- the
  former would let stale evidence cover a changed requirement, the latter would
  churn the whole obligation list on every edit.
* **Bounded**. A Task carries at most ``MAX_PROOF_OBLIGATIONS`` obligations and
  one contract source compiles to at most ``MAX_OBLIGATIONS_PER_SOURCE``, so the
  projection and the checkpoint stay bounded.
* **No business dispatch**. Composition depends only on structured contract
  fields -- never on project type, directory name, framework or tool name
  (AG-15, Neutrality).
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .configuration import canonical_hash


class ObligationPredicate(StrEnum):
    """What a single obligation asserts about the world."""

    ARTIFACT_EXISTS = "ARTIFACT_EXISTS"
    ARTIFACT_CONTENT = "ARTIFACT_CONTENT"
    RELATION_EXISTS = "RELATION_EXISTS"
    EFFECT_OBSERVED = "EFFECT_OBSERVED"
    COMMAND_VERIFIED = "COMMAND_VERIFIED"
    #: "The declared scope contains exactly N matching objects" (N may be 0).
    #: The only predicate able to certify absence, hence the strictest
    #: conditions -- see SPEC AG-13 / §4.4.
    QUERY_CARDINALITY = "QUERY_CARDINALITY"
    #: Cannot be settled by deterministic evidence. Only compilable from
    #: ``TaskCriterionKind.RUBRIC`` / ``GOAL_ALIGNMENT``.
    SEMANTIC_REVIEW = "SEMANTIC_REVIEW"


#: Total obligations a single Task may carry. The worst case is
#: ``MAX_AUTHORED_ACCEPTANCE_CRITERIA (28) x MAX_OBLIGATIONS_PER_SOURCE (4)``,
#: which exceeds this bound -- truncation is therefore a *normal* path, not an
#: exceptional one, and it must never be able to drop a required obligation
#: silently (SPEC D1).
MAX_PROOF_OBLIGATIONS = 64

#: Obligations one criterion / outcome may compile to.
MAX_OBLIGATIONS_PER_SOURCE = 4


def normalize_text(value: str) -> str:
    """Normalize requirement text so equal requirements hash equally.

    Deliberately conservative: it folds compatibility forms, case, quote
    variants, whitespace runs and trailing sentence punctuation. It does **not**
    strip meaningful punctuation from the middle of a sentence, because that
    could make two genuinely different requirements collide.
    """

    folded = unicodedata.normalize("NFKC", value).casefold()
    for source, target in _QUOTE_VARIANTS:
        folded = folded.replace(source, target)
    folded = " ".join(folded.split())
    # One layer of *surrounding* quotes is cosmetic: `Show it` and `"Show it."`
    # state the same requirement and must not produce two identities. Inner
    # quotes are left alone -- dropping those could merge different requirements.
    for quote in ('"', "'"):
        if len(folded) >= 2 and folded.startswith(quote) and folded.endswith(quote):
            folded = folded[1:-1].strip()
            break
    return folded.rstrip(_TRAILING_PUNCTUATION)


_QUOTE_VARIANTS: tuple[tuple[str, str], ...] = (
    ("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'),
)

_TRAILING_PUNCTUATION = ".\u3002\uff0e!?\uff01\uff1f;,\uff0c\u3001:\uff1a"


def requirement_digest(requirement_text: str) -> str:
    """Content digest that makes a changed requirement change the obligation id."""

    return canonical_hash(normalize_text(requirement_text))


#: Selector keys that carry a version or a content-derived identity. They are
#: stripped before hashing: including them would change the obligation id every
#: time a file is edited (AG-10 rule 2).
_VERSION_BEARING_KEYS: frozenset[str] = frozenset({
    "version", "artifact_id", "content_hash", "hash", "revision", "etag",
    "sha256", "digest", "mtime", "updated_at", "generated_at",
})


def version_free_selector(selector: Mapping[str, Any]) -> dict[str, Any]:
    """Return the selector with every version-bearing key removed, recursively.

    Only stable locators survive: a path, a symbol qualified name, a command
    signature, an external resource identifier.
    """

    cleaned: dict[str, Any] = {}
    for key in sorted(selector):
        if key.casefold() in _VERSION_BEARING_KEYS:
            continue
        value = selector[key]
        if isinstance(value, Mapping):
            cleaned[key] = version_free_selector(value)
        elif isinstance(value, (list, tuple)):
            cleaned[key] = [
                version_free_selector(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            cleaned[key] = value
    return cleaned


@dataclass(frozen=True, slots=True)
class ProofObligation:
    """One atomic requirement, derived from the Task Contract."""

    obligation_id: str
    contract_ref: str
    required: bool
    predicate: ObligationPredicate
    subject_selector: Mapping[str, Any] = field(default_factory=dict)
    expected: Mapping[str, Any] = field(default_factory=dict)
    scope: Mapping[str, Any] = field(default_factory=dict)
    required_capabilities: frozenset[str] = frozenset()
    depends_on: tuple[str, ...] = ()
    #: A TaskSpec revision may retire a requirement. Retired obligations keep
    #: their id and their events; they are never reused for a different
    #: requirement (AG-10 "只增不换").
    retired: bool = False

    @property
    def is_semantic(self) -> bool:
        return self.predicate is ObligationPredicate.SEMANTIC_REVIEW

    def to_data(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "contract_ref": self.contract_ref,
            "required": self.required,
            "predicate": self.predicate.value,
            "subject_selector": dict(self.subject_selector),
            "expected": dict(self.expected),
            "scope": dict(self.scope),
            "required_capabilities": sorted(self.required_capabilities),
            "depends_on": list(self.depends_on),
            "retired": self.retired,
        }

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> ProofObligation:
        return cls(
            obligation_id=str(data["obligation_id"]),
            contract_ref=str(data["contract_ref"]),
            required=bool(data["required"]),
            predicate=ObligationPredicate(data["predicate"]),
            subject_selector=dict(data.get("subject_selector") or {}),
            expected=dict(data.get("expected") or {}),
            scope=dict(data.get("scope") or {}),
            required_capabilities=frozenset(
                data.get("required_capabilities") or ()
            ),
            depends_on=tuple(data.get("depends_on") or ()),
            retired=bool(data.get("retired", False)),
        )


def derive_obligation_id(
    *,
    contract_ref: str,
    predicate: ObligationPredicate,
    requirement_text: str,
    selector: Mapping[str, Any],
) -> str:
    """Derive the stable, content-sensitive obligation id (AG-10)."""

    fingerprint = canonical_hash({
        "contract_ref": contract_ref,
        "predicate": predicate.value,
        "requirement_digest": requirement_digest(requirement_text),
        "selector": version_free_selector(selector),
    })
    return f"obg-{fingerprint[:16]}"


def make_obligation(
    *,
    contract_ref: str,
    predicate: ObligationPredicate,
    requirement_text: str,
    selector: Mapping[str, Any],
    required: bool,
    expected: Mapping[str, Any] | None = None,
    scope: Mapping[str, Any] | None = None,
    required_capabilities: Iterable[str] = (),
    depends_on: Sequence[str] = (),
) -> ProofObligation:
    """Build an obligation with a derived id -- the only supported constructor."""

    return ProofObligation(
        obligation_id=derive_obligation_id(
            contract_ref=contract_ref, predicate=predicate,
            requirement_text=requirement_text, selector=selector,
        ),
        contract_ref=contract_ref,
        required=required,
        predicate=predicate,
        subject_selector=version_free_selector(selector),
        expected=dict(expected or {}),
        scope=dict(scope or {}),
        required_capabilities=frozenset(required_capabilities),
        depends_on=tuple(depends_on),
    )


@dataclass(frozen=True, slots=True)
class ObligationTruncation:
    """Outcome of applying the obligation bound (SPEC D1)."""

    kept: tuple[ProofObligation, ...]
    dropped: tuple[ProofObligation, ...]
    #: True when the bound would have dropped a **required** obligation. The
    #: caller must then record ``proof.plan_truncated{REQUIRED_OVERFLOW}`` and
    #: route the Task to human review: silently keeping the first N required
    #: obligations would let the Task succeed while a real requirement was
    #: never proven (a new false-success channel).
    required_overflow: bool

    @property
    def truncated(self) -> bool:
        return bool(self.dropped)


def truncate_obligations(
    ordered: Sequence[ProofObligation],
    *,
    limit: int = MAX_PROOF_OBLIGATIONS,
) -> ObligationTruncation:
    """Apply the obligation bound to an already priority-ordered sequence.

    Ordering is the compiler's responsibility (SPEC §4.1): deterministic
    priority -- ``REQUIRED_EFFECT`` -> ``POST_MUTATION_COMMAND`` -> the rest --
    so the same TaskSpec always truncates at the same boundary.
    """

    if limit < 1:
        raise ValueError("obligation limit must be positive")
    if len(ordered) <= limit:
        return ObligationTruncation(tuple(ordered), (), False)
    kept = tuple(ordered[:limit])
    dropped = tuple(ordered[limit:])
    return ObligationTruncation(kept, dropped, any(item.required for item in dropped))


def retire_obligation(obligation: ProofObligation) -> ProofObligation:
    """Mark an obligation retired without changing its identity (AG-10)."""

    return replace(obligation, retired=True)
