"""Local optimizer module for the cadastral boundary correction pipeline.

This module implements the hierarchical coarse-to-fine optimization strategy
to find the candidate transformation that maximizes the alignment score,
preserving top-k candidates, detecting local maxima peaks, and calculating search statistics.

Enhanced with:
- Multi-start initialization (official, global, neighbor)
- Adaptive search expansion (entropy/peak-gap driven)
- Multi-hypothesis refinement (cluster + refine top N)
- Basin stability analysis
- Hypothesis cluster extraction for spatial ambiguity estimation
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw
from scipy.interpolate import griddata

from bhume.alignment_scorer import AlignmentScorer, ScoredCandidate
from bhume.candidate_generator import CandidateGenerator, CandidateTransformation
from bhume.config import Config
from bhume.contour_detector import BoundaryRepresentation
from bhume.edge_detector import EdgeLevelResult
from bhume.loader import CoordinateTransformer

logger = logging.getLogger(__name__)


@dataclass
class OptimizedCandidate:
    """Dataclass holding information for a ranked candidate."""

    candidate: CandidateTransformation
    total_score: float
    feature_scores: Dict[str, float]
    search_stage: str
    rank: int


@dataclass
class BasinStability:
    """Basin stability metrics around an optimum."""

    stability_score: float  # Average score at offsets / best score
    basin_width: float  # Effective width where score > 0.9 * best
    neighborhood_avg: float  # Mean score at all offset positions


@dataclass
class OptimizationStatistics:
    """Dataclass holding statistics about the search space and convergence."""

    best_score: float
    second_best_score: float
    score_gap: float
    score_mean: float
    score_std: float
    score_entropy: float
    number_of_local_maxima: int
    candidate_count: int
    optimum_on_boundary: bool
    convergence_path: List[Tuple[float, float]]
    basin_stability: BasinStability | None = None
    optimizer_iterations: int = 0
    final_step_size: float = 0.0
    optimization_residual: float = 0.0
    converged: bool = True
    coarse_best_shift: Tuple[float, float] | None = None
    medium_best_shift: Tuple[float, float] | None = None
    fine_best_shift: Tuple[float, float] | None = None
    init_agreement: float = 1.0


@dataclass
class OptimizationResult:
    """Dataclass holding results of the hierarchical local optimization."""

    best_candidate: CandidateTransformation
    best_score: float
    evaluated_candidate_count: int
    optimization_history: List[ScoredCandidate]
    refinement_levels: Dict[str, float]  # Best score at each level (coarse, refinement, sub_meter)
    top_k_candidates: List[OptimizedCandidate]
    statistics: OptimizationStatistics
    peaks: List[OptimizedCandidate]
    official_score: float | None = None  # None = unknown; never confuse with a real 0.0 score
    # Distinct spatially-separated alignment hypotheses as (dx, dy, score), best
    # first. Consumed by the confidence ambiguity signal and decision ambiguity veto.
    hypothesis_clusters: List[Tuple[float, float, float]] = field(default_factory=list)


def detect_local_maxima(
    history: List[ScoredCandidate],
    exclusion_distance: float
) -> List[ScoredCandidate]:
    """Find local maxima (peaks) from evaluated candidates using spatial distance suppression.

    A candidate is a local maximum if it has the highest score within its exclusion radius.
    """
    if not history:
        return []

    # Sort all candidates by score descending
    sorted_candidates = sorted(history, key=lambda sc: sc.total_score, reverse=True)

    local_maxima: List[ScoredCandidate] = []

    for sc in sorted_candidates:
        is_suppressed = False
        dx1, dy1 = sc.candidate.dx, sc.candidate.dy
        for lm in local_maxima:
            dx2, dy2 = lm.candidate.dx, lm.candidate.dy
            dist = np.sqrt((dx1 - dx2) ** 2 + (dy1 - dy2) ** 2)
            if dist < exclusion_distance:
                is_suppressed = True
                break
        if not is_suppressed:
            local_maxima.append(sc)

    return local_maxima


def cluster_candidates(
    candidates: List[ScoredCandidate],
    cluster_radius: float,
    max_clusters: int = 50,
) -> List[List[ScoredCandidate]]:
    """Cluster scored candidates in (dx, dy) space using greedy distance-based clustering.

    Returns list of clusters, each cluster is a list of ScoredCandidate,
    sorted by total_score descending within each cluster.
    """
    if not candidates:
        return []

    sorted_cands = sorted(candidates, key=lambda sc: sc.total_score, reverse=True)
    clusters: List[List[ScoredCandidate]] = []
    assigned = set()

    for i, sc in enumerate(sorted_cands):
        if i in assigned:
            continue
        if len(clusters) >= max_clusters:
            break

        # Start a new cluster with this candidate as the center
        cluster = [sc]
        assigned.add(i)
        cx, cy = sc.candidate.dx, sc.candidate.dy

        for j in range(i + 1, len(sorted_cands)):
            if j in assigned:
                continue
            dx2, dy2 = sorted_cands[j].candidate.dx, sorted_cands[j].candidate.dy
            dist = np.sqrt((cx - dx2) ** 2 + (cy - dy2) ** 2)
            if dist <= cluster_radius:
                cluster.append(sorted_cands[j])
                assigned.add(j)

        clusters.append(cluster)

    return clusters


def save_optimizer_debug(
    plot_number: str,
    history: List[ScoredCandidate],
    path_points: List[Tuple[float, float]],
    local_maxima: List[ScoredCandidate],
    top_k: List[OptimizedCandidate],
    config: Config,
) -> Path:
    """Save debug visualization plotting evaluated candidate points, score surface,

    trajectory, and local maxima.
    """
    d = Path(config.debug_out_dir) / "optimizer"
    d.mkdir(parents=True, exist_ok=True)

    # Convert coordinates to arrays
    points = np.array([[sc.candidate.dx, sc.candidate.dy] for sc in history])
    values = np.array([sc.total_score for sc in history])

    # 1. Define grid bounds
    min_x, max_x = np.min(points[:, 0]) - 1.0, np.max(points[:, 0]) + 1.0
    min_y, max_y = np.min(points[:, 1]) - 1.0, np.max(points[:, 1]) + 1.0
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)

    # 2. Create the score surface using interpolation
    grid_size = 500
    cols = np.linspace(min_x, max_x, grid_size)
    rows = np.linspace(max_y, min_y, grid_size)  # Inverted Y coordinate for PIL mapping
    grid_x, grid_y = np.meshgrid(cols, rows)

    # Linear interpolation onto a dense grid
    grid_z = griddata(points, values, (grid_x, grid_y), method="linear", fill_value=np.min(values))

    # Normalize grid_z to [0, 1] for visual colormapping
    min_s = np.min(values)
    max_s = np.max(values)
    span_s = max_s - min_s if max_s - min_s > 1e-5 else 1.0
    norm_grid = (grid_z - min_s) / span_s
    norm_grid = np.clip(norm_grid, 0.0, 1.0)

    # Map normalized scores to a custom Viridis-like RGB colormap
    r_map = np.zeros_like(norm_grid)
    g_map = np.zeros_like(norm_grid)
    b_map = np.zeros_like(norm_grid)

    # Category 1: t < 0.3
    mask1 = norm_grid < 0.3
    f1 = norm_grid[mask1] / 0.3
    r_map[mask1] = 15 + f1 * 15
    g_map[mask1] = 15 + f1 * 15
    b_map[mask1] = 40 + f1 * 60

    # Category 2: 0.3 <= t < 0.7
    mask2 = (norm_grid >= 0.3) & (norm_grid < 0.7)
    f2 = (norm_grid[mask2] - 0.3) / 0.4
    r_map[mask2] = 30 + f2 * 10
    g_map[mask2] = 30 + f2 * 120
    b_map[mask2] = 100 - f2 * 40

    # Category 3: t >= 0.7
    mask3 = norm_grid >= 0.7
    f3 = (norm_grid[mask3] - 0.7) / 0.3
    r_map[mask3] = 40 + f3 * 215
    g_map[mask3] = 150 + f3 * 100
    b_map[mask3] = 60 - f3 * 40

    rgb = np.stack([r_map, g_map, b_map], axis=-1).astype(np.uint8)
    pil_img = Image.fromarray(rgb)
    draw = ImageDraw.Draw(pil_img)

    # Coordinate mapping function
    def to_pixel(x, y):
        col = int((x - min_x) / span_x * grid_size)
        row = int((max_y - y) / span_y * grid_size)
        return col, row

    # Draw grid axes (dx = 0, dy = 0)
    ax_col, ax_row = to_pixel(0.0, 0.0)
    if 0 <= ax_col < grid_size:
        draw.line([(ax_col, 0), (ax_col, grid_size)], fill=(70, 70, 70), width=1)
    if 0 <= ax_row < grid_size:
        draw.line([(0, ax_row), (grid_size, ax_row)], fill=(70, 70, 70), width=1)

    # 3. Draw all evaluated candidates as background grey dots
    for sc in history:
        px, py = to_pixel(sc.candidate.dx, sc.candidate.dy)
        draw.ellipse([px - 1, py - 1, px + 1, py + 1], fill=(120, 120, 120), outline=(50, 50, 50))

    # 4. Draw Top-K candidates as white rings
    for rank, sc in enumerate(top_k, 1):
        px, py = to_pixel(sc.candidate.dx, sc.candidate.dy)
        draw.ellipse([px - 4, py - 4, px + 4, py + 4], outline=(255, 255, 255), width=1)

    # 5. Draw detected local maxima as yellow crosses
    for lm in local_maxima:
        px, py = to_pixel(lm.candidate.dx, lm.candidate.dy)
        draw.line([(px - 6, py), (px + 6, py)], fill=(255, 215, 0), width=2)
        draw.line([(px, py - 6), (px, py + 6)], fill=(255, 215, 0), width=2)

    # 6. Draw the optimization path (trajectory) in cyan
    path_pixels = [to_pixel(pt[0], pt[1]) for pt in path_points]
    if len(path_pixels) > 1:
        draw.line(path_pixels, fill=(0, 255, 255), width=2)
        # Draw path keypoints
        for px, py in path_pixels:
            draw.ellipse([px - 4, py - 4, px + 4, py + 4], fill=(0, 255, 255), outline=(0, 120, 255), width=1)

    # 7. Highlight the global optimum (best candidate)
    best_cand = top_k[0].candidate if top_k else history[0].candidate
    px, py = to_pixel(best_cand.dx, best_cand.dy)
    draw.ellipse([px - 7, py - 7, px + 7, py + 7], outline=(255, 0, 0), fill=(255, 255, 0), width=2)

    # 8. Draw a solid dark legend box in the top-left
    draw.rectangle([10, 10, 220, 135], fill=(20, 20, 20), outline=(100, 100, 100))
    draw.text((20, 15), f"Plot {plot_number} Search Space", fill=(255, 255, 255))

    # Legend items
    # Local Maxima
    draw.line([(22, 45), (32, 45)], fill=(255, 215, 0), width=2)
    draw.line([(27, 40), (27, 50)], fill=(255, 215, 0), width=2)
    draw.text((40, 40), "Local Maxima", fill=(200, 200, 200))

    # Top-K Candidates
    draw.ellipse([23, 61, 31, 69], outline=(255, 255, 255), width=1)
    draw.text((40, 60), f"Top-{len(top_k)} Candidates", fill=(200, 200, 200))

    # Optimization Trajectory
    draw.line([(20, 85), (34, 85)], fill=(0, 255, 255), width=2)
    draw.ellipse([25, 82, 29, 88], fill=(0, 255, 255))
    draw.text((40, 80), "Search Trajectory", fill=(200, 200, 200))

    # Global Optimum
    draw.ellipse([23, 101, 31, 109], outline=(255, 0, 0), fill=(255, 255, 0), width=2)
    draw.text((40, 100), "Global Optimum", fill=(200, 200, 200))

    # Candidate Count
    draw.text((20, 120), f"Total evaluated: {len(history)}", fill=(150, 150, 150))

    out_path = d / f"plot_{plot_number}_optim_path.png"
    pil_img.save(out_path)
    logger.debug(f"Saved revised optimizer debug image to: {out_path}")
    return out_path


class LocalOptimizer:
    """Orchestrates hierarchical coarse-to-fine searches to locate alignment optima."""

    def __init__(
        self, config: Config, generator: CandidateGenerator, scorer: AlignmentScorer
    ) -> None:
        """Initialize LocalOptimizer.

        Args:
            config: Pipeline configuration.
            generator: CandidateGenerator helper.
            scorer: AlignmentScorer helper.
        """
        self.config = config
        self.generator = generator
        self.scorer = scorer

    def _score_candidates(
        self,
        candidates: List[CandidateTransformation],
        boundary: BoundaryRepresentation,
        edge_level: EdgeLevelResult,
        transformer: CoordinateTransformer,
        patch_transform: object,
        recorded_area_m2: float | None,
        map_area_m2: float | None,
        neighbor_shift: Tuple[float, float] | None,
        expected_drift: Tuple[float, float] | None = None,
    ) -> List[ScoredCandidate]:
        """Score a batch of candidates against imagery evidence."""
        return self.scorer.score_candidates(
            candidates,
            boundary,
            edge_level,
            transformer,
            patch_transform=patch_transform,
            recorded_area_m2=recorded_area_m2,
            map_area_m2=map_area_m2,
            neighbor_shift=neighbor_shift,
            expected_drift=expected_drift,
        )

    def _compute_entropy(self, scores: np.ndarray) -> float:
        """Compute softmax entropy of score distribution."""
        if len(scores) == 0:
            return 0.0
        max_score = np.max(scores)
        temp = 0.02
        exp_scores = np.exp((scores - max_score) / temp)
        probs = exp_scores / np.sum(exp_scores)
        return -float(np.sum(probs * np.log(probs + 1e-12)))

    def _compute_basin_stability(
        self,
        best_candidate: CandidateTransformation,
        best_score: float,
        boundary: BoundaryRepresentation,
        edge_level: EdgeLevelResult,
        transformer: CoordinateTransformer,
        patch_transform: object,
        recorded_area_m2: float | None,
        map_area_m2: float | None,
        neighbor_shift: Tuple[float, float] | None,
        expected_drift: Tuple[float, float] | None = None,
    ) -> BasinStability:
        """Evaluate score at offsets around the optimum to measure basin stability."""
        offsets = self.config.basin_offsets_m
        all_offset_scores = []

        for offset in offsets:
            for ddx, ddy in [(offset, 0), (-offset, 0), (0, offset), (0, -offset)]:
                cand = CandidateTransformation(
                    dx=best_candidate.dx + ddx,
                    dy=best_candidate.dy + ddy,
                    search_level="stability",
                )
                sc = self.scorer.score_candidate(
                    cand, boundary, edge_level, transformer,
                    patch_transform=patch_transform,
                    recorded_area_m2=recorded_area_m2,
                    map_area_m2=map_area_m2,
                    neighbor_shift=neighbor_shift,
                    expected_drift=expected_drift,
                )
                all_offset_scores.append(sc.total_score)

        neighborhood_avg = float(np.mean(all_offset_scores)) if all_offset_scores else best_score
        stability_score = neighborhood_avg / best_score if best_score > 1e-9 else 0.0

        # Basin width: count offsets where score > 0.9 * best
        threshold = 0.9 * best_score
        n_above = sum(1 for s in all_offset_scores if s >= threshold)
        basin_width = float(n_above) / max(len(all_offset_scores), 1)

        return BasinStability(
            stability_score=float(np.clip(stability_score, 0.0, 1.0)),
            basin_width=basin_width,
            neighborhood_avg=neighborhood_avg,
        )

    def optimize(
        self,
        plot_number: str,
        boundary: BoundaryRepresentation,
        edge_level: EdgeLevelResult,
        transformer: CoordinateTransformer,
        patch_transform: object,
        patch_gray: np.ndarray,
        center_dx: float = 0.0,
        center_dy: float = 0.0,
        recorded_area_m2: float | None = None,
        map_area_m2: float | None = None,
        perimeter_m: float | None = None,
        neighbor_shift: Tuple[float, float] | None = None,
        expected_drift: Tuple[float, float] | None = None,
        coarse_only: bool = False,
    ) -> OptimizationResult:
        """Run the hierarchical optimization search for a single plot."""
        # Temporarily disable debug visualization for bulk search iterations
        orig_vis = self.scorer.config.debug_visualize
        self.scorer.config.debug_visualize = False

        scored_cache: Dict[str, ScoredCandidate] = {}
        history: List[ScoredCandidate] = []
        refinement_levels: Dict[str, float] = {}

        # Compute equivalent diameter to bound search radius dynamically
        area_m2 = map_area_m2 if map_area_m2 is not None else (recorded_area_m2 if recorded_area_m2 is not None else 1000.0)
        d_eq = 2.0 * np.sqrt(area_m2 / np.pi)
        
        # Compute physical span based on Area/Perimeter ratio.
        # For a long rectangle L x W (L >> W), Area=L*W, Perimeter~2L -> 2*Area/Perimeter ~ W.
        # For a square of side S, Area=S^2, Perimeter=4S -> 2*Area/Perimeter = S/2.
        # For a circle of radius R, Area=pi*R^2, Perimeter=2*pi*R -> 2*Area/Perimeter = R.
        # This perfectly bounds the search radius to the plot's shortest dimension!
        if perimeter_m is not None and perimeter_m > 0:
            physical_span = 2.0 * area_m2 / perimeter_m
        else:
            physical_span = d_eq
        

            
        if self.config.enable_adaptive_search:
            max_allowed_radius = max(self.config.search_radius_m * self.config.adaptive_radius_multiplier, physical_span)
        else:
            max_allowed_radius = max(self.config.search_radius_m, physical_span)

        t_start = time.perf_counter()
        print(f"DEBUG: optimize() start, max_allowed_radius={max_allowed_radius:.2f}, coarse_radius={self.config.search_radius_m:.2f}")

        score_kwargs = dict(
            boundary=boundary, edge_level=edge_level, transformer=transformer,
            patch_transform=patch_transform, recorded_area_m2=recorded_area_m2,
            map_area_m2=map_area_m2, neighbor_shift=neighbor_shift,
            expected_drift=expected_drift,
        )

        def score_batch(cands: List[CandidateTransformation]) -> List[ScoredCandidate]:
            novel = []
            for c in cands:
                # STRICT PHYSICAL BOUNDARY ENFORCEMENT:
                # Never evaluate candidates whose absolute shift exceeds the plot's physical scale limit.
                # This prevents extreme neighbor consensus from pulling small/narrow plots into hallucinated locations.
                if np.hypot(c.dx, c.dy) > max_allowed_radius * 1.05:
                    continue
                
                if c.candidate_id not in scored_cache:
                    novel.append(c)
            if novel:
                scored = self._score_candidates(novel, **score_kwargs)
                for sc in scored:
                    scored_cache[sc.candidate.candidate_id] = sc
                    history.append(sc)
            return [scored_cache[c.candidate_id] for c in cands if c.candidate_id in scored_cache]

        official_cand = CandidateTransformation(dx=0.0, dy=0.0, search_level="official")
        official_scored = score_batch([official_cand])[0]
        official_score = official_scored.total_score

        # ==========================================
        # STAGE 1: Multi-start Coarse Translation Search
        # ==========================================
        start_centers = [(center_dx, center_dy)]

        if self.config.enable_multi_start:
            # Always include official position (0, 0)
            if abs(center_dx) > 0.5 or abs(center_dy) > 0.5:
                start_centers.append((0.0, 0.0))
            # Include neighbor consensus if available
            if neighbor_shift is not None:
                ndx, ndy = neighbor_shift
                # Only add if sufficiently different from existing starts
                is_novel = all(
                    np.hypot(ndx - sx, ndy - sy) > 2.0
                    for sx, sy in start_centers
                )
                if is_novel:
                    start_centers.append((ndx, ndy))

        converged_locations = []
        initial_search_radius_m = min(self.config.search_radius_m, max_allowed_radius)
        self.config.search_radius_m = initial_search_radius_m

        # Optimization trajectory loop with boundary expansion (up to MAX_EXPANSIONS = 2)
        MAX_EXPANSIONS = 2
        
        for expansion_idx in range(MAX_EXPANSIONS + 1):
            converged_locations.clear()
            start_coarse_cands = {}
            for sx, sy in start_centers:
                coarse_cands = self.generator.generate_coarse(sx, sy)
                start_coarse_cands[(sx, sy)] = coarse_cands
                score_batch(coarse_cands)

            if coarse_only:
                # Early return for stage 0 consensus estimation
                coarse_list = [sc for sc in scored_cache.values() if sc.candidate.search_level in ("coarse", "adaptive")]
                best_coarse = max(coarse_list, key=lambda s: s.total_score) if coarse_list else official_scored
                stats = OptimizationStatistics(
                    best_score=best_coarse.total_score,
                    second_best_score=0.0,
                    score_gap=0.0,
                    score_mean=0.0,
                    score_std=0.0,
                    score_entropy=0.0,
                    number_of_local_maxima=0,
                    candidate_count=len(scored_cache),
                    optimum_on_boundary=False,
                    convergence_path=[]
                )
                # Restore original search radius to config before returning
                self.config.search_radius_m = initial_search_radius_m
                return OptimizationResult(
                    best_candidate=best_coarse.candidate,
                    best_score=best_coarse.total_score,
                    evaluated_candidate_count=len(scored_cache),
                    optimization_history=[],
                    refinement_levels={},
                    top_k_candidates=[],
                    statistics=stats,
                    peaks=[],
                    official_score=official_score,
                )

            # Run search path (coarse -> fine -> submeter) for each start center separately
            for sx, sy in start_centers:
                coarse_cands = start_coarse_cands[(sx, sy)]
                scored_coarse = [scored_cache[c.candidate_id] for c in coarse_cands if c.candidate_id in scored_cache]
                if not scored_coarse:
                    cand = CandidateTransformation(dx=sx, dy=sy, search_level="coarse")
                    scored_coarse = score_batch([cand])
                best_coarse_for_start = max(scored_coarse, key=lambda s: s.total_score)

                # Adaptive Search Expansion for this start center if needed
                if self.config.enable_adaptive_search and len(scored_coarse) > 1:
                    coarse_scores = np.array([s.total_score for s in scored_coarse])
                    entropy = self._compute_entropy(coarse_scores)
                    sorted_scores = np.sort(coarse_scores)[::-1]
                    peak_gap = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) > 1 else 1.0

                    if entropy > self.config.entropy_high_threshold or peak_gap < self.config.peak_gap_low_threshold:
                        expanded_radius = self.config.search_radius_m * self.config.adaptive_radius_multiplier
                        expanded_step = self.config.search_step_m * self.config.adaptive_step_multiplier

                        bx, by = best_coarse_for_start.candidate.dx, best_coarse_for_start.candidate.dy
                        x_offsets = np.arange(-expanded_radius, expanded_radius + 1e-5, expanded_step) + bx
                        y_offsets = np.arange(-expanded_radius, expanded_radius + 1e-5, expanded_step) + by

                        expanded_cands = [
                            CandidateTransformation(dx=float(dx), dy=float(dy), search_level="adaptive")
                            for dx in x_offsets for dy in y_offsets
                        ]
                        scored_expanded = score_batch(expanded_cands)
                        if scored_expanded:
                            new_best = max(scored_expanded, key=lambda s: s.total_score)
                            if new_best.total_score > best_coarse_for_start.total_score:
                                best_coarse_for_start = new_best

                # Dynamic/Adaptive Refinement Radius using Score Gap AND Peak Sharpness
                scored_coarse_for_gap = [scored_cache[c.candidate_id] for c in coarse_cands if c.candidate_id in scored_cache]
                if len(scored_coarse_for_gap) > 1:
                    coarse_scores = np.array([s.total_score for s in scored_coarse_for_gap])
                    sorted_scores = np.sort(coarse_scores)[::-1]
                    score_gap = float(sorted_scores[0] - sorted_scores[1])
                else:
                    score_gap = 1.0

                # Compute peak sharpness P for best_coarse_for_start (P = max(0.0, best - mean of neighbors) / max(best, 10^-6)) (normalized)
                step_size = self.config.search_step_m
                bx, by = best_coarse_for_start.candidate.dx, best_coarse_for_start.candidate.dy
                neighbors_scored = []
                for sc in scored_coarse_for_gap:
                    d = np.hypot(sc.candidate.dx - bx, sc.candidate.dy - by)
                    if 0.01 < d <= 1.5 * step_size:
                        neighbors_scored.append(sc.total_score)
                peak_sharpness = float(max(0.0, best_coarse_for_start.total_score - np.mean(neighbors_scored)) / max(best_coarse_for_start.total_score, 1e-6)) if neighbors_scored else 0.0

                # R = R_min + (R_max - R_min) * exp(-alpha * g) * exp(-beta * P)
                fine_radius = 2.0 + 3.5 * np.exp(-self.config.alpha_gap * score_gap) * np.exp(-self.config.beta_peak * peak_sharpness)
                fine_radius = float(np.clip(fine_radius, 2.0, 5.5))

                # Stage 2: Refinement
                fine_cands = self.generator.refine_around(
                    best_coarse_for_start.candidate,
                    radius=fine_radius,
                    step=self.config.fine_search_step_m,
                    level="refinement",
                )
                scored_fine = score_batch(fine_cands)
                best_fine_for_start = best_coarse_for_start
                if scored_fine:
                    best_fine_temp = max(scored_fine, key=lambda s: s.total_score)
                    if best_fine_temp.total_score > best_coarse_for_start.total_score:
                        best_fine_for_start = best_fine_temp

                # Stage 3: Sub-meter Refinement Search
                sub_cands = self.generator.refine_around(
                    best_fine_for_start.candidate,
                    radius=self.config.submeter_search_radius_m,
                    step=self.config.submeter_search_step_m,
                    level="sub_meter",
                )
                scored_sub = score_batch(sub_cands)
                best_sub_for_start = best_fine_for_start
                if scored_sub:
                    best_sub_temp = max(scored_sub, key=lambda s: s.total_score)
                    if best_sub_temp.total_score > best_fine_for_start.total_score:
                        best_sub_for_start = best_sub_temp

                converged_locations.append((best_sub_for_start.candidate.dx, best_sub_for_start.candidate.dy))

            # Multi-hypothesis refinement (if enabled, run over all evaluated coarse candidates)
            if self.config.enable_multi_hypothesis:
                all_coarse_scored = [sc for sc in scored_cache.values() if sc.candidate.search_level in ("coarse", "adaptive")]
                if len(all_coarse_scored) > 1:
                    top_coarse = sorted(all_coarse_scored, key=lambda s: s.total_score, reverse=True)[:50]
                    clusters = cluster_candidates(
                        top_coarse,
                        cluster_radius=self.config.cluster_radius_m,
                    )
                    n_refine = min(self.config.num_hypotheses_to_refine, len(clusters))
                    for cluster in clusters[:n_refine]:
                        cluster_center = cluster[0]
                        
                        if len(top_coarse) > 1:
                            coarse_scores_hyp = np.array([s.total_score for s in top_coarse])
                            sorted_scores_hyp = np.sort(coarse_scores_hyp)[::-1]
                            overall_score_gap = float(sorted_scores_hyp[0] - sorted_scores_hyp[1])
                        else:
                            overall_score_gap = 1.0

                        # Calculate peak sharpness for cluster center (normalized)
                        cbx, cby = cluster_center.candidate.dx, cluster_center.candidate.dy
                        c_neighbors = []
                        for sc in top_coarse:
                            d = np.hypot(sc.candidate.dx - cbx, sc.candidate.dy - cby)
                            if 0.01 < d <= 1.5 * self.config.search_step_m:
                                c_neighbors.append(sc.total_score)
                        c_sharpness = float(max(0.0, cluster_center.total_score - np.mean(c_neighbors)) / max(cluster_center.total_score, 1e-6)) if c_neighbors else 0.0

                        fine_radius_hyp = 2.0 + 3.5 * np.exp(-self.config.alpha_gap * overall_score_gap) * np.exp(-self.config.beta_peak * c_sharpness)
                        fine_radius_hyp = float(np.clip(fine_radius_hyp, 2.0, 5.5))

                        fine_cands = self.generator.refine_around(
                            cluster_center.candidate,
                            radius=fine_radius_hyp,
                            step=self.config.fine_search_step_m,
                            level="refinement",
                        )
                        score_batch(fine_cands)
                        scored_fine = [scored_cache[c.candidate_id] for c in fine_cands if c.candidate_id in scored_cache]
                        best_fine_cluster = cluster_center
                        if scored_fine:
                            best_fine_temp = max(scored_fine, key=lambda s: s.total_score)
                            if best_fine_temp.total_score > cluster_center.total_score:
                                best_fine_cluster = best_fine_temp

                        sub_cands = self.generator.refine_around(
                            best_fine_cluster.candidate,
                            radius=self.config.submeter_search_radius_m,
                            step=self.config.submeter_search_step_m,
                            level="sub_meter",
                        )
                        score_batch(sub_cands)

            # Get overall best candidate strictly based on raw total_score
            best_sub = max(scored_cache.values(), key=lambda s: s.total_score)

            # Boundary clipping check: is best_sub near the search boundary of its closest start center?
            closest_start = start_centers[0]
            min_start_dist = float('inf')
            for sx, sy in start_centers:
                d = np.hypot(best_sub.candidate.dx - sx, best_sub.candidate.dy - sy)
                if d < min_start_dist:
                    min_start_dist = d
                    closest_start = (sx, sy)

            coarse_radius = self.config.search_radius_m
            dist_x = abs(best_sub.candidate.dx - closest_start[0])
            dist_y = abs(best_sub.candidate.dy - closest_start[1])

            # Check if clipping boundary (allowing 1.0m tolerance)
            is_clipping = (dist_x >= coarse_radius - 1.0 or dist_y >= coarse_radius - 1.0)
            
            # If not clipping, we converged!
            if not is_clipping or expansion_idx >= MAX_EXPANSIONS:
                break
                
            # Expand search radius in config for the next loop pass (drift-based boundary expansion)
            observed_drift = min_start_dist
            expanded_r = float(max(coarse_radius * 1.4, observed_drift + 2.0))
            self.config.search_radius_m = min(expanded_r, max_allowed_radius)
            logger.info(
                f"Boundary clipping detected at (dx={best_sub.candidate.dx:.2f}, dy={best_sub.candidate.dy:.2f}). "
                f"Expanding search radius to {self.config.search_radius_m:.2f}m and re-optimizing (expansion pass {expansion_idx + 1}/{MAX_EXPANSIONS})."
            )

        # ==========================================
        # STAGE 4: Gradient-Directed Local Polishing
        # ==========================================
        current = best_sub
        step_sizes = [0.25, self.config.submeter_search_step_m]
        MAX_POLISH = 10
        for step_size in step_sizes:
            for _ in range(MAX_POLISH):
                dx, dy = current.candidate.dx, current.candidate.dy
                neighbors = []
                for idx_x in [-1, 0, 1]:
                    for idx_y in [-1, 0, 1]:
                        if idx_x == 0 and idx_y == 0:
                            continue
                        ndx = dx + idx_x * step_size
                        ndy = dy + idx_y * step_size
                        neighbors.append(CandidateTransformation(
                            dx=float(ndx), dy=float(ndy),
                            rotation=current.candidate.rotation,
                            scale=current.candidate.scale,
                            search_level="polish"
                        ))
                scored_neighbors = score_batch(neighbors)
                best_neighbor = max(scored_neighbors, key=lambda x: x.total_score)
                if best_neighbor.total_score <= current.total_score:
                    break
                current = best_neighbor
        best_sub = current

        # Find best coarse and fine that led to best_sub
        coarse_list = [sc for sc in scored_cache.values() if sc.candidate.search_level in ("coarse", "adaptive")]
        best_coarse = max(coarse_list, key=lambda s: s.total_score) if coarse_list else best_sub
        refinement_levels["coarse"] = best_coarse.total_score

        ref_list = [sc for sc in scored_cache.values() if sc.candidate.search_level in ("coarse", "adaptive", "refinement")]
        best_fine = max(ref_list, key=lambda s: s.total_score) if ref_list else best_sub
        refinement_levels["refinement"] = best_fine.total_score

        refinement_levels["sub_meter"] = best_sub.total_score

        # Determine path points from closest start center
        closest_start = start_centers[0]
        min_start_dist = float('inf')
        for sx, sy in start_centers:
            d = np.hypot(best_sub.candidate.dx - sx, best_sub.candidate.dy - sy)
            if d < min_start_dist:
                min_start_dist = d
                closest_start = (sx, sy)

        path_points = [
            closest_start,
            (best_coarse.candidate.dx, best_coarse.candidate.dy),
            (best_fine.candidate.dx, best_fine.candidate.dy),
            (best_sub.candidate.dx, best_sub.candidate.dy)
        ]

        t_elapsed = time.perf_counter() - t_start

        # ------------------------------------------------------------
        # Save physical span limits for downstream regularizer
        # ------------------------------------------------------------
        best_sub.candidate.metadata["max_allowed_radius"] = float(max_allowed_radius)

        # Restore config search radius to initial value
        self.config.search_radius_m = initial_search_radius_m

        # ==========================================
        # Basin Stability Analysis
        # ==========================================
        basin = None
        if self.config.enable_basin_stability:
            basin = self._compute_basin_stability(
                best_sub.candidate, best_sub.total_score,
                **score_kwargs,
            )

        # ------------------------------------------------------------
        # Compute Optimization Statistics and Ambiguity Indicators
        # ------------------------------------------------------------
        # 1. Sort history to construct Top-K lists
        sorted_history = sorted(history, key=lambda sc: sc.total_score, reverse=True)
        top_k_candidates: List[OptimizedCandidate] = []
        for rank, sc in enumerate(sorted_history[:self.config.top_k_candidates], 1):
            top_k_candidates.append(
                OptimizedCandidate(
                    candidate=sc.candidate,
                    total_score=sc.total_score,
                    feature_scores=sc.feature_scores,
                    search_stage=sc.candidate.search_level,
                    rank=rank,
                )
            )

        # 2. Local Maxima detection
        local_maxima_scored = detect_local_maxima(
            history,
            exclusion_distance=self.config.multi_peak_distance_m
        )

        # 3. Peaks extraction within tolerance of best_score
        best_score = best_sub.total_score
        peaks: List[OptimizedCandidate] = []
        for rank, lm in enumerate(local_maxima_scored, 1):
            if lm.total_score >= best_score - self.config.multi_peak_threshold:
                peaks.append(
                    OptimizedCandidate(
                        candidate=lm.candidate,
                        total_score=lm.total_score,
                        feature_scores=lm.feature_scores,
                        search_stage=lm.candidate.search_level,
                        rank=rank
                    )
                )

        # 3b. Hypothesis clusters for ambiguity estimation.
        ambiguity_dist = getattr(self.config, "ambiguity_distance_m", 5.0)
        ambiguity_maxima = detect_local_maxima(
            history,
            exclusion_distance=ambiguity_dist,
        )
        hypothesis_clusters: List[Tuple[float, float, float]] = [
            (float(lm.candidate.dx), float(lm.candidate.dy), float(lm.total_score))
            for lm in ambiguity_maxima[:5]
        ]

        # 4. Statistics values
        if len(local_maxima_scored) > 1:
            second_best_score = local_maxima_scored[1].total_score
        elif len(history) > 1:
            second_best_score = sorted_history[1].total_score
        else:
            second_best_score = best_score

        score_gap = best_score - second_best_score

        scores = np.array([sc.total_score for sc in history])
        score_mean = float(np.mean(scores))
        score_std = float(np.std(scores))
        score_entropy = self._compute_entropy(scores)

        # Boundary checks
        optimum_on_boundary = bool(
            best_sub.candidate.dx <= (closest_start[0] - initial_search_radius_m + 1e-3) or
            best_sub.candidate.dx >= (closest_start[0] + initial_search_radius_m - 1e-3) or
            best_sub.candidate.dy <= (closest_start[1] - initial_search_radius_m + 1e-3) or
            best_sub.candidate.dy >= (closest_start[1] + initial_search_radius_m - 1e-3)
        )

        # Optimization stability metrics
        optimizer_iterations = len(path_points)
        final_step_size = self.config.submeter_search_step_m
        optimization_residual = float(np.hypot(best_sub.candidate.dx - best_fine.candidate.dx, best_sub.candidate.dy - best_fine.candidate.dy))
        converged = bool(not optimum_on_boundary and (optimization_residual < 1.5 * self.config.submeter_search_step_m))

        # Calculate repeatability score between initializations using median pairwise distance
        if len(converged_locations) > 1:
            distances = []
            n_locs = len(converged_locations)
            for i in range(n_locs):
                for j in range(i + 1, n_locs):
                    dist = np.hypot(
                        converged_locations[i][0] - converged_locations[j][0],
                        converged_locations[i][1] - converged_locations[j][1]
                    )
                    distances.append(dist)
            
            median_dist = float(np.median(distances))
            scale = getattr(self.config, "repeatability_scale_m", 3.0)
            init_agreement = float(np.clip(1.0 - (median_dist / scale), 0.0, 1.0))
        else:
            init_agreement = 1.0
        
        repeatability_score = init_agreement

        # Expose all diagnostics to best_candidate.metadata
        best_sub.candidate.metadata["optimizer_iterations"] = optimizer_iterations
        best_sub.candidate.metadata["optimization_residual"] = optimization_residual
        best_sub.candidate.metadata["init_agreement"] = init_agreement
        best_sub.candidate.metadata["repeatability_score"] = repeatability_score
        
        best_sub.candidate.metadata["score_gap"] = score_gap
        best_sub.candidate.metadata["ambiguity_index"] = float(second_best_score / (best_score + 1e-9))
        best_sub.candidate.metadata["basin_width"] = float(basin.basin_width if basin is not None else 0.5)

        # Peak sharpness for best_sub (using neighbors within 1.5 * step_size of submeter) (normalized)
        sub_step_size = self.config.submeter_search_step_m
        sub_bx, sub_by = best_sub.candidate.dx, best_sub.candidate.dy
        sub_neighbors_scored = []
        for sc in history:
            d = np.hypot(sc.candidate.dx - sub_bx, sc.candidate.dy - sub_by)
            if 0.001 < d <= 1.5 * sub_step_size:
                sub_neighbors_scored.append(sc.total_score)
        peak_sharpness = float(max(0.0, best_sub.total_score - np.mean(sub_neighbors_scored)) / max(best_sub.total_score, 1e-6)) if sub_neighbors_scored else 0.0
        best_sub.candidate.metadata["peak_sharpness"] = peak_sharpness

        stats = OptimizationStatistics(
            best_score=best_score,
            second_best_score=second_best_score,
            score_gap=score_gap,
            score_mean=score_mean,
            score_std=score_std,
            score_entropy=score_entropy,
            number_of_local_maxima=len(local_maxima_scored),
            candidate_count=len(history),
            optimum_on_boundary=optimum_on_boundary,
            convergence_path=path_points,
            basin_stability=basin,
            optimizer_iterations=optimizer_iterations,
            final_step_size=final_step_size,
            optimization_residual=optimization_residual,
            converged=converged,
            init_agreement=init_agreement,
        )

        logger.debug(
            f"Plot {plot_number} optimized: best score {best_score:.4f} "
            f"at dx={best_sub.candidate.dx:.2f}, dy={best_sub.candidate.dy:.2f} "
            f"({len(history)} evaluated, {t_elapsed * 1000:.1f}ms)"
        )

        self.scorer.config.debug_visualize = orig_vis
        if orig_vis:
            self.scorer.score_candidate(
                best_sub.candidate,
                boundary,
                edge_level,
                transformer,
                patch_transform=patch_transform,
                recorded_area_m2=recorded_area_m2,
                map_area_m2=map_area_m2,
                neighbor_shift=neighbor_shift,
                expected_drift=expected_drift,
            )
            save_optimizer_debug(
                plot_number,
                history,
                path_points,
                local_maxima_scored,
                top_k_candidates,
                self.config,
            )

        best_sub.candidate.metadata["optimizer_iterations"] = stats.optimizer_iterations
        best_sub.candidate.metadata["final_step_size"] = stats.final_step_size
        best_sub.candidate.metadata["optimization_residual"] = stats.optimization_residual
        best_sub.candidate.metadata["converged"] = stats.converged
        best_sub.candidate.metadata["init_agreement"] = stats.init_agreement

        return OptimizationResult(
            best_candidate=best_sub.candidate,
            best_score=best_score,
            evaluated_candidate_count=len(history),
            optimization_history=history,
            refinement_levels=refinement_levels,
            top_k_candidates=top_k_candidates,
            statistics=stats,
            peaks=peaks,
            official_score=official_score,
            hypothesis_clusters=hypothesis_clusters,
        )
