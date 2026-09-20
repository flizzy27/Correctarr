"""Rate indexers by what they actually deliver.

The idea: Prowlarr knows how often an indexer was queried and how often that
turned into a grab. Radarr and Sonarr know **how good** the grabbed release was,
through the custom format score. Put together, that is enough to tell which
indexer is carrying its weight and which one only burns queries.

Deliberately a suggestion, never a change: a wrongly set indexer priority is
easy to make and hard to notice later.

The score per indexer is made of four parts:

  yield        (40%)  grabs per query. The most honest number: an indexer that
                      never delivers anything useful only costs time.
  quality      (35%)  mean custom format score of the releases that came from
                      it, measured against the best source in the field.
  reliability  (15%)  share of queries and grabs without errors.
  speed        (10%)  response time relative to the fastest source.

All parts land between 0 and 1, the score between 0 and 100. An indexer with
too little data gets no score at all — better to say nothing than to derive a
ranking from three grabs.

One thing that matters for quality: the history of the Arr services only goes
back a few hundred entries. A rarely used indexer may have no sample left in
there at all. Missing data is not a bad report card, so in that case quality
drops out of the calculation entirely and the remaining weights are scaled back
up to 100 percent. The first draft treated missing data as zero — which handed
the indexer with the best score in the field (540,000) a grade of 0.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

WEIGHTS = {"yield": 0.40, "quality": 0.35, "reliability": 0.15, "speed": 0.10}
MIN_QUERIES = 50          # below this any statement is chance
MIN_SAMPLES = 3           # history samples needed for a quality statement

UNKNOWN_TO_PROWLARR = "unknown to Prowlarr"


@dataclass
class IndexerView:
    name: str
    id: int | None = None
    priority: int | None = None
    enabled: bool = True
    queries: int = 0
    grabs: int = 0
    failed_queries: int = 0
    failed_grabs: int = 0
    response_ms: float = 0.0
    mean_score: float = 0.0
    mean_gb: float = 0.0
    samples: int = 0                # history entries the quality rests on
    rating: float | None = None
    parts: dict = field(default_factory=dict)
    solid: bool = False
    notes: list[str] = field(default_factory=list)


def _normalise(value: float, best: float, lower_is_better: bool = False) -> float:
    """Map onto 0..1, measured against the best value in the field."""
    if best <= 0:
        return 0.0
    if lower_is_better:
        return max(0.0, min(1.0, best / value)) if value > 0 else 1.0
    return max(0.0, min(1.0, value / best))


def build_views(prowlarr_indexers: list[dict], stats: list[dict],
                statuses: list[dict],
                history_by_indexer: dict[str, list]) -> list[IndexerView]:
    """Merge Prowlarr's numbers with the history of the Arr services."""
    by_name: dict[str, IndexerView] = {}

    for entry in prowlarr_indexers:
        view = IndexerView(name=entry.get("name", "?"), id=entry.get("id"),
                           priority=entry.get("priority"),
                           enabled=bool(entry.get("enable", True)))
        by_name[view.name] = view

    for row in stats:
        name = row.get("indexerName") or "?"
        view = by_name.setdefault(name, IndexerView(name=name))
        view.queries = int(row.get("numberOfQueries") or 0)
        view.grabs = int(row.get("numberOfGrabs") or 0)
        view.failed_queries = int(row.get("numberOfFailedQueries") or 0)
        view.failed_grabs = int(row.get("numberOfFailedGrabs") or 0)
        view.response_ms = float(row.get("averageResponseTime") or 0)

    # Radarr and Sonarr name the indexer with a " (Prowlarr)" suffix.
    for raw_name, samples in history_by_indexer.items():
        name = raw_name.replace(" (Prowlarr)", "").strip()
        view = by_name.get(name)
        if view is None:
            view = by_name.setdefault(name, IndexerView(name=name))
            view.notes.append(UNKNOWN_TO_PROWLARR)
        scores = [s for s, _ in samples if s]
        sizes = [g for _, g in samples if g]
        view.samples = len(scores)
        if scores:
            view.mean_score = statistics.mean(scores)
        if sizes:
            view.mean_gb = statistics.mean(sizes)

    disabled = {s.get("indexerId") for s in statuses if s.get("disabledTill")}
    for view in by_name.values():
        if view.id in disabled:
            view.notes.append("disabled by Prowlarr")
    return list(by_name.values())


def rate(views: list[IndexerView]) -> list[IndexerView]:
    """Assign the score. The benchmark is always the best in the field."""
    usable = [v for v in views if v.queries >= MIN_QUERIES]
    if not usable:
        return views

    best_yield = max(v.grabs / v.queries for v in usable) or 1e-9
    # The quality benchmark only comes from indexers with enough samples,
    # otherwise a single outlier skews the whole field.
    best_quality = max((v.mean_score for v in usable if v.samples >= MIN_SAMPLES),
                       default=0) or 1e-9
    best_speed = min((v.response_ms for v in usable if v.response_ms > 0),
                     default=0) or 1e-9

    for view in views:
        if view.queries < MIN_QUERIES:
            view.notes.append(f"too little data ({view.queries} queries)")
            continue

        parts = {
            "yield": _normalise(view.grabs / view.queries, best_yield),
            "reliability": max(0.0, 1.0 - (view.failed_queries + view.failed_grabs)
                               / max(1, view.queries)),
            "speed": (_normalise(view.response_ms, best_speed, lower_is_better=True)
                      if view.response_ms else 0.5),
        }
        if view.samples >= MIN_SAMPLES:
            parts["quality"] = _normalise(view.mean_score, best_quality)
        else:
            view.notes.append(f"quality not rated ({view.samples} samples in history)")

        # Scale the weights up to the parts that are actually present, so a
        # missing value does not enter the grade as a zero.
        total_weight = sum(WEIGHTS[k] for k in parts)
        view.parts = {k: round(v, 3) for k, v in parts.items()}
        view.rating = round(100 * sum(WEIGHTS[k] * v for k, v in parts.items())
                            / total_weight, 1)
        view.solid = view.samples >= MIN_SAMPLES
    return views


def ranked(views: list[IndexerView]) -> list[IndexerView]:
    """Sorted by score, best first. Unrated ones go last."""
    return sorted(views, key=lambda v: (v.rating is None, -(v.rating or 0)))


def rank_deviation(views: list[IndexerView], tolerance: int = 2) -> list[dict]:
    """Compare the configured order against the measured one.

    What matters is the RANK, not the absolute number: if the best indexer sits
    at priority 1 there is nothing to say — whether that number reads 1, 5 or
    10. A deviation is only reported when an indexer is more than ``tolerance``
    places out of position. Small differences are noise and a reason to say
    nothing at all.

    In Prowlarr a smaller number means higher precedence.
    """
    rated = [v for v in views
             if v.rating is not None and v.enabled and v.priority is not None]
    if len(rated) < 3:
        return []

    by_setting = sorted(rated, key=lambda v: v.priority)
    by_measurement = sorted(rated, key=lambda v: -v.rating)
    actual_rank = {v.name: i + 1 for i, v in enumerate(by_setting)}
    target_rank = {v.name: i + 1 for i, v in enumerate(by_measurement)}

    out = []
    for view in rated:
        difference = actual_rank[view.name] - target_rank[view.name]
        if abs(difference) < tolerance:
            continue
        # Which number would put it in the right place? Taken from the priority
        # that already sits there, so the suggestion stays inside the range the
        # operator chose.
        slot = target_rank[view.name] - 1
        neighbours = [v.priority for v in by_setting]
        suggested = neighbours[slot] if slot < len(neighbours) else view.priority
        out.append({
            "view": view,
            "actual_rank": actual_rank[view.name],
            "target_rank": target_rank[view.name],
            "actual_priority": view.priority,
            "suggested_priority": suggested,
            "direction": "higher" if difference > 0 else "lower",
            "places": abs(difference),
        })
    return out
