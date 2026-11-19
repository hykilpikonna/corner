from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
from scipy.optimize import linear_sum_assignment

from app.media.fingerprint import compute_image_fingerprint, phash_hamming_distance

# Default weighting and confidence thresholds for Workflow B matching
DEFAULT_PHASH_WEIGHT = 0.55
DEFAULT_RGB_WEIGHT = 0.35
DEFAULT_ASPECT_WEIGHT = 0.10

DEFAULT_MAX_DISTANCE = 0.22
DEFAULT_MIN_MARGIN = 0.06


@dataclass(slots=True)
class CandidatePhoto:
    file_key: str
    filename: str
    path: Path
    phash: str
    rgb_sig: bytes
    width: int
    height: int
    size: int


@dataclass(slots=True)
class TargetPostImage:
    image_id: int
    sort_order: int
    url: str
    thumb_url: str | None
    phash: str | None
    rgb_sig: bytes | None
    width: int
    height: int


@dataclass(slots=True)
class PairMatch:
    file_key: str
    filename: str
    image_id: int
    sort_order: int
    distance: float
    phash_dist: float
    rgb_dist: float
    aspect_penalty: float
    runner_up_distance: float | None
    margin: float | None
    confident: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_key": self.file_key,
            "filename": self.filename,
            "image_id": self.image_id,
            "sort_order": self.sort_order,
            "distance": round(self.distance, 4),
            "phash_dist": round(self.phash_dist, 4),
            "rgb_dist": round(self.rgb_dist, 4),
            "aspect_penalty": round(self.aspect_penalty, 4),
            "runner_up_distance": (
                round(self.runner_up_distance, 4) if self.runner_up_distance is not None else None
            ),
            "margin": round(self.margin, 4) if self.margin is not None else None,
            "confident": self.confident,
            "reason": self.reason,
        }


@dataclass(slots=True)
class MatchResult:
    status: Literal["confident", "ambiguous"]
    matches: list[PairMatch]
    unmatched_files: list[str]
    cost_matrix: list[list[float]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "matches": [m.to_dict() for m in self.matches],
            "unmatched_files": list(self.unmatched_files),
        }


def build_candidate_photo(
    file_key: str,
    filename: str,
    path: Path,
) -> CandidatePhoto:
    """Compute perceptual fingerprint + dimensions for an uploaded original candidate."""
    p = Path(path)
    phash, rgb_sig, width, height = compute_image_fingerprint(p)
    return CandidatePhoto(
        file_key=file_key,
        filename=filename,
        path=p,
        phash=phash,
        rgb_sig=rgb_sig,
        width=width,
        height=height,
        size=p.stat().st_size,
    )


def compute_rgb_distance(sig1: bytes | None, sig2: bytes | None) -> float:
    """Compute blended normalized L1 + cosine distance in [0, 1] between 16x16x3 RGB vectors."""
    if not sig1 or not sig2:
        return 1.0
    a = np.frombuffer(sig1, dtype=np.uint8).astype(np.float32) / 255.0
    b = np.frombuffer(sig2, dtype=np.uint8).astype(np.float32) / 255.0
    if a.shape != b.shape or a.size == 0:
        return 1.0

    l1_dist = float(np.mean(np.abs(a - b)))

    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a == 0.0 and norm_b == 0.0:
        cos_dist = 0.0
    elif norm_a == 0.0 or norm_b == 0.0:
        cos_dist = 1.0
    else:
        cos_sim = float(np.dot(a, b) / (norm_a * norm_b))
        cos_dist = max(0.0, min(1.0, 1.0 - cos_sim))

    return float(max(0.0, min(1.0, 0.65 * l1_dist + 0.35 * cos_dist)))


def compute_aspect_penalty(w1: int, h1: int, w2: int, h2: int) -> float:
    """Compute aspect-ratio incompatibility penalty in [0, 1]."""
    if w1 <= 0 or h1 <= 0 or w2 <= 0 or h2 <= 0:
        return 0.0
    r1 = w1 / h1
    r2 = w2 / h2
    rel_diff = abs(r1 - r2) / max(r1, r2)
    # Allow minor rounding drift (~3%) from Telegram resizing
    if rel_diff <= 0.03:
        return 0.0
    return float(min(1.0, (rel_diff - 0.03) * 1.5))


def compute_pair_distance(
    candidate: CandidatePhoto,
    target: TargetPostImage,
    *,
    phash_weight: float = DEFAULT_PHASH_WEIGHT,
    rgb_weight: float = DEFAULT_RGB_WEIGHT,
    aspect_weight: float = DEFAULT_ASPECT_WEIGHT,
) -> tuple[float, float, float, float]:
    """Return (combined_distance, phash_dist, rgb_dist, aspect_penalty) in [0, 1]."""
    if candidate.phash and target.phash:
        phash_dist = phash_hamming_distance(candidate.phash, target.phash) / 64.0
    else:
        phash_dist = 1.0

    rgb_dist = compute_rgb_distance(candidate.rgb_sig, target.rgb_sig)
    aspect_penalty = compute_aspect_penalty(
        candidate.width,
        candidate.height,
        target.width,
        target.height,
    )

    total_weight = phash_weight + rgb_weight + aspect_weight
    if total_weight <= 0:
        combined = phash_dist
    else:
        combined = (
            phash_weight * phash_dist + rgb_weight * rgb_dist + aspect_weight * aspect_penalty
        ) / total_weight

    return (
        float(max(0.0, min(1.0, combined))),
        float(phash_dist),
        float(rgb_dist),
        float(aspect_penalty),
    )


def ensure_target_fingerprints(
    targets: list[TargetPostImage],
    media_root: Path | None = None,
) -> list[TargetPostImage]:
    """Lazily compute missing pHash/RGB signatures from disk when `media_root` is provided."""
    if media_root is None:
        return targets

    root_resolved = media_root.resolve()
    hydrated: list[TargetPostImage] = []
    for t in targets:
        if t.phash and t.rgb_sig:
            hydrated.append(t)
            continue

        phash, rgb_sig, width, height = t.phash, t.rgb_sig, t.width, t.height
        raw_rel = t.url.lstrip("./").lstrip("/")
        if raw_rel and not raw_rel.startswith(("http://", "https://")):
            cand = (root_resolved / raw_rel).resolve()
            if cand.is_relative_to(root_resolved) and cand.is_file():
                try:
                    phash, rgb_sig, det_w, det_h = compute_image_fingerprint(cand)
                    if width <= 0:
                        width = det_w
                    if height <= 0:
                        height = det_h
                except Exception:
                    pass

        hydrated.append(
            TargetPostImage(
                image_id=t.image_id,
                sort_order=t.sort_order,
                url=t.url,
                thumb_url=t.thumb_url,
                phash=phash,
                rgb_sig=rgb_sig,
                width=width,
                height=height,
            )
        )
    return hydrated


def match_originals_to_post_images(
    candidates: list[CandidatePhoto],
    targets: list[TargetPostImage],
    *,
    media_root: Path | None = None,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    min_margin: float = DEFAULT_MIN_MARGIN,
    phash_weight: float = DEFAULT_PHASH_WEIGHT,
    rgb_weight: float = DEFAULT_RGB_WEIGHT,
    aspect_weight: float = DEFAULT_ASPECT_WEIGHT,
) -> MatchResult:
    """Solve optimal 1-to-1 assignment between uploaded originals and PostImages via Hungarian algorithm.

    Supports both partial uploads (`M < N`) and full album uploads (`M == N`).
    If `M > N`, matches the best `N` pairs, reports extra files in `unmatched_files`,
    and marks the result as `ambiguous` so the user can confirm.
    """
    if not candidates or not targets:
        return MatchResult(
            status="ambiguous",
            matches=[],
            unmatched_files=[c.file_key for c in candidates],
            cost_matrix=[],
        )

    hydrated_targets = ensure_target_fingerprints(targets, media_root=media_root)
    m_count = len(candidates)
    n_count = len(hydrated_targets)

    cost_np = np.zeros((m_count, n_count), dtype=np.float64)
    breakdown: list[list[tuple[float, float, float, float]]] = []

    for i, cand in enumerate(candidates):
        row_b: list[tuple[float, float, float, float]] = []
        for j, tgt in enumerate(hydrated_targets):
            dist, p_dist, r_dist, a_pen = compute_pair_distance(
                cand,
                tgt,
                phash_weight=phash_weight,
                rgb_weight=rgb_weight,
                aspect_weight=aspect_weight,
            )
            cost_np[i, j] = dist
            row_b.append((dist, p_dist, r_dist, a_pen))
        breakdown.append(row_b)

    row_ind, col_ind = linear_sum_assignment(cost_np)
    matched_rows = set(int(r) for r in row_ind)
    unmatched_files = [
        candidates[i].file_key for i in range(m_count) if i not in matched_rows
    ]

    matches: list[PairMatch] = []
    all_confident = len(unmatched_files) == 0

    for r_raw, c_raw in zip(row_ind, col_ind, strict=True):
        i = int(r_raw)
        j = int(c_raw)
        cand = candidates[i]
        tgt = hydrated_targets[j]
        dist, p_dist, r_dist, a_pen = breakdown[i][j]

        # Compute closest alternative (runner-up) across both row (other targets) and column (other candidates)
        runner_ups: list[float] = []
        if n_count > 1:
            row_alts = [float(cost_np[i, col]) for col in range(n_count) if col != j]
            if row_alts:
                runner_ups.append(min(row_alts))
        if m_count > 1:
            col_alts = [float(cost_np[row, j]) for row in range(m_count) if row != i]
            if col_alts:
                runner_ups.append(min(col_alts))

        runner_up = min(runner_ups) if runner_ups else None
        margin = (runner_up - dist) if runner_up is not None else None

        reasons: list[str] = []
        if tgt.phash is None or tgt.rgb_sig is None:
            reasons.append("target_missing_fingerprint")
        if dist > max_distance:
            reasons.append(f"distance_above_threshold({dist:.3f}>{max_distance:.3f})")
        if margin is not None and margin < min_margin:
            reasons.append(f"low_margin({margin:.3f}<{min_margin:.3f})")

        pair_confident = len(reasons) == 0
        if not pair_confident:
            all_confident = False

        matches.append(
            PairMatch(
                file_key=cand.file_key,
                filename=cand.filename,
                image_id=tgt.image_id,
                sort_order=tgt.sort_order,
                distance=dist,
                phash_dist=p_dist,
                rgb_dist=r_dist,
                aspect_penalty=a_pen,
                runner_up_distance=runner_up,
                margin=margin,
                confident=pair_confident,
                reason="; ".join(reasons) if reasons else None,
            )
        )

    matches.sort(key=lambda pm: pm.sort_order)
    status: Literal["confident", "ambiguous"] = "confident" if all_confident else "ambiguous"

    return MatchResult(
        status=status,
        matches=matches,
        unmatched_files=unmatched_files,
        cost_matrix=[[round(float(val), 4) for val in row] for row in cost_np.tolist()],
    )
