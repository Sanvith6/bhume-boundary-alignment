"""Spatial regularizer module for the cadastral boundary correction pipeline.

This module refines local optimized transformations using topological relationships
between neighboring plots to improve spatial consistency while preserving local evidence.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw

from bhume.candidate_generator import CandidateTransformation
from bhume.config import Config
from bhume.io import Village
from bhume.optimizer import OptimizationResult, OptimizationStatistics

logger = logging.getLogger(__name__)


@dataclass
class RegularizedOptimizationResult:
    """Dataclass holding the regularized optimization results for a plot."""

    original_candidate: CandidateTransformation
    regularized_candidate: CandidateTransformation
    applied_shift: Tuple[float, float]
    local_shift: Tuple[float, float]
    neighbor_shift: Tuple[float, float]
    blending_factor: float
    neighbor_statistics: Dict
    debug_metadata: Dict


class SpatialRegularizer:
    """Refines local optimized transformations using topological relationships and neighbor consensus."""

    def __init__(self, config: Config) -> None:
        """Initialize SpatialRegularizer with pipeline configuration.

        Args:
            config: Pipeline configuration object.
        """
        self.config = config

    def compute_quality(self, stats: OptimizationStatistics) -> float:
        """Compute local optimization quality metric Q in [0, 1] based on config weights.

        Args:
            stats: The local OptimizationStatistics.

        Returns:
            Quality score in [0.0, 1.0].
        """
        weights = getattr(self.config, "regularization_quality_weights", {
            "best_score": 0.5,
            "score_gap": 0.3,
            "entropy": 0.2
        })

        # Base e Shannon entropy, max possible is log(candidate_count)
        max_entropy = np.log(max(stats.candidate_count, 2))
        norm_entropy = stats.score_entropy / max_entropy if max_entropy > 0 else 0.0

        quality = (
            weights.get("best_score", 0.5) * stats.best_score +
            weights.get("score_gap", 0.3) * stats.score_gap +
            weights.get("entropy", 0.2) * (1.0 - norm_entropy)
        )
        return float(np.clip(quality, 0.0, 1.0))

    def regularize(
        self,
        results: Dict[str, OptimizationResult],
        village: Village,
        graph: Dict[str, Dict[str, float]] | None = None
    ) -> Dict[str, RegularizedOptimizationResult]:
        """Smooth local optimized shifts based on neighborhood consensus.

        Args:
            results: Dict mapping plot_number to OptimizationResult.
            village: The loaded Village dataclass.
            graph: Dict mapping plot_number to Dict of {neighbor: shared_boundary_length}.
                   If None, it is constructed using build_neighbor_graph.

        Returns:
            Dict mapping plot_number to RegularizedOptimizationResult.
        """
        if graph is None:
            from bhume.loader import build_neighbor_graph
            graph = build_neighbor_graph(village.plots)

        # 1. Compute qualities for all plots
        qualities = {}
        for pn, res in results.items():
            qualities[pn] = self.compute_quality(res.statistics)

        # Precompute centroids in EPSG:3857 for distance decay weighting
        plots_projected = village.plots.to_crs("EPSG:3857")
        centroids_m = {}
        for pn, row in plots_projected.iterrows():
            geom = row.geometry
            if geom is not None and not geom.is_empty:
                centroid = geom.centroid
                centroids_m[str(pn)] = (centroid.x, centroid.y)

        # 2. Iteratively smooth shifts
        current_shifts = {pn: (res.best_candidate.dx, res.best_candidate.dy) for pn, res in results.items()}
        shift_history = [copy.deepcopy(current_shifts)]

        iterations = self.config.regularization_iterations
        outlier_thresh = getattr(self.config, "regularization_outlier_threshold_m", 5.0)
        min_neighbor_q = getattr(self.config, "regularization_min_neighbor_quality", 0.2)
        high_q_thresh = getattr(self.config, "regularization_high_quality_threshold", 0.7)
        low_q_thresh = getattr(self.config, "regularization_low_quality_threshold", 0.3)
        min_alpha = getattr(self.config, "regularization_min_alpha", 0.2)

        blending_factors = {}
        neighbor_stats = {}
        neighbor_shifts = {}

        for step in range(iterations):
            new_shifts = {}
            for pn, res in results.items():
                local_dx, local_dy = res.best_candidate.dx, res.best_candidate.dy
                q_local = qualities[pn]

                # Determine adaptive blending factor alpha
                if q_local >= high_q_thresh:
                    alpha = 1.0
                elif q_local <= low_q_thresh:
                    alpha = min_alpha
                else:
                    span = high_q_thresh - low_q_thresh
                    alpha = min_alpha + (1.0 - min_alpha) * (q_local - low_q_thresh) / span

                blending_factors[pn] = alpha

                # Get neighbors from the graph
                neighbors_dict = graph.get(pn, {})

                # Filter valid neighbors that have optimization results and meet quality requirements
                valid_neighbors = []
                for n_pn, length in neighbors_dict.items():
                    if n_pn in results and qualities[n_pn] >= min_neighbor_q:
                        ndx, ndy = current_shifts[n_pn]
                        valid_neighbors.append((n_pn, ndx, ndy, length, qualities[n_pn], results[n_pn].best_score))

                # Outlier detection
                if valid_neighbors:
                    neighbor_dxs = [item[1] for item in valid_neighbors]
                    neighbor_dys = [item[2] for item in valid_neighbors]

                    median_dx = np.median(neighbor_dxs)
                    median_dy = np.median(neighbor_dys)

                    filtered_neighbors = []
                    ignored_neighbors = []
                    for item in valid_neighbors:
                        n_pn, ndx, ndy, length, q_n, s_n = item
                        dist = np.sqrt((ndx - median_dx)**2 + (ndy - median_dy)**2)
                        if dist <= outlier_thresh:
                            filtered_neighbors.append(item)
                        else:
                            ignored_neighbors.append(item)
                else:
                    filtered_neighbors = []
                    ignored_neighbors = []

                # Compute weighted consensus shift
                if filtered_neighbors:
                    total_weight = 0.0
                    weighted_sum_dx = 0.0
                    weighted_sum_dy = 0.0

                    contribs = []
                    c_local = centroids_m.get(str(pn))
                    for item in filtered_neighbors:
                        n_pn, ndx, ndy, length, q_n, s_n = item
                        
                        # Distance decay weighting (Gaussian decay)
                        distance_weight = 1.0
                        if c_local is not None:
                            c_neighbor = centroids_m.get(str(n_pn))
                            if c_neighbor is not None:
                                centroid_dist = np.hypot(c_local[0] - c_neighbor[0], c_local[1] - c_neighbor[1])
                                decay_scale = getattr(self.config, "neighbor_max_distance_m", 50.0)
                                if decay_scale > 0:
                                    distance_weight = float(np.exp(-0.5 * (centroid_dist / decay_scale)**2))

                        # Weight neighbor by shared boundary length * quality * score * distance_weight
                        w = length * q_n * s_n * distance_weight
                        weighted_sum_dx += w * ndx
                        weighted_sum_dy += w * ndy
                        total_weight += w

                        contribs.append({
                            "plot_number": n_pn,
                            "weight": float(w),
                            "shift": (float(ndx), float(ndy)),
                            "shared_boundary": float(length),
                            "quality": float(q_n),
                            "score": float(s_n),
                            "distance_weight": distance_weight
                        })

                    if total_weight > 1e-9:
                        neigh_dx = weighted_sum_dx / total_weight
                        neigh_dy = weighted_sum_dy / total_weight
                    else:
                        neigh_dx, neigh_dy = local_dx, local_dy

                    neighbor_stats[pn] = {
                        "neighbor_count": len(filtered_neighbors),
                        "ignored_neighbor_count": len(ignored_neighbors),
                        "neighbor_contributions": contribs,
                        "topological_neighbor_count": len(neighbors_dict)
                    }
                else:
                    neigh_dx, neigh_dy = local_dx, local_dy
                    neighbor_stats[pn] = {
                        "neighbor_count": 0,
                        "ignored_neighbor_count": len(ignored_neighbors),
                        "neighbor_contributions": [],
                        "topological_neighbor_count": len(neighbors_dict)
                    }

                neighbor_shifts[pn] = (neigh_dx, neigh_dy)

                # Blend local shift with neighborhood consensus
                reg_dx = alpha * local_dx + (1.0 - alpha) * neigh_dx
                reg_dy = alpha * local_dy + (1.0 - alpha) * neigh_dy

                new_shifts[pn] = (reg_dx, reg_dy)

            current_shifts = new_shifts
            shift_history.append(copy.deepcopy(current_shifts))

        # 3. Build final output results
        reg_results = {}
        for pn, res in results.items():
            local_dx, local_dy = res.best_candidate.dx, res.best_candidate.dy
            reg_dx, reg_dy = current_shifts[pn]
            neigh_dx, neigh_dy = neighbor_shifts[pn]
            alpha = blending_factors[pn]

            orig_cand = res.best_candidate
            reg_cand = copy.deepcopy(orig_cand)
            
            # STRICT PHYSICAL BOUNDARY ENFORCEMENT:
            # Prevent neighbor consensus from dragging plots beyond their physical limits
            max_allowed_radius = orig_cand.metadata.get("max_allowed_radius")
            print(f"DEBUG {pn}: local_dx={local_dx:.2f}, neigh_dx={neigh_dx:.2f}, alpha={alpha:.2f}, reg_dx={reg_dx:.2f}, max_allowed_radius={max_allowed_radius}")
            if max_allowed_radius is not None:
                dist = np.hypot(reg_dx, reg_dy)
                if dist > max_allowed_radius:
                    scale = max_allowed_radius / dist
                    reg_dx *= scale
                    reg_dy *= scale
            
            reg_cand.dx = reg_dx
            reg_cand.dy = reg_dy
            reg_cand.metadata["regularized"] = True
            reg_cand.metadata["blending_factor"] = alpha

            reg_results[pn] = RegularizedOptimizationResult(
                original_candidate=orig_cand,
                regularized_candidate=reg_cand,
                applied_shift=(reg_dx, reg_dy),
                local_shift=(local_dx, local_dy),
                neighbor_shift=(neigh_dx, neigh_dy),
                blending_factor=alpha,
                neighbor_statistics=neighbor_stats[pn],
                debug_metadata={
                    "quality": qualities[pn],
                    "shift_history": [hist[pn] for hist in shift_history]
                }
            )

        # 4. Generate debug visualization if enabled
        if self.config.debug_visualize:
            self.visualize_regularization(village, results, reg_results, graph)

        return reg_results

    def visualize_regularization(
        self,
        village: Village,
        results: Dict[str, OptimizationResult],
        reg_results: Dict[str, RegularizedOptimizationResult],
        graph: Dict[str, Dict[str, float]],
    ) -> Path | None:
        """Save a premium PIL-based debug visualization of the neighbor graph,

        displacement vectors, and smoothing influence.
        """
        out_dir = Path(self.config.debug_out_dir) / "regularizer"
        out_dir.mkdir(parents=True, exist_ok=True)

        plots_projected = village.plots.to_crs("EPSG:3857")
        bounds = plots_projected.total_bounds
        if bounds is None or len(bounds) != 4:
            return None
        min_x, min_y, max_x, max_y = bounds
        span_x = max(max_x - min_x, 10.0)
        span_y = max(max_y - min_y, 10.0)

        img_size = 1000
        pad = 50

        def to_pixel(x, y):
            col = int((x - min_x) / span_x * (img_size - 2 * pad)) + pad
            row = img_size - pad - int((y - min_y) / span_y * (img_size - 2 * pad))
            return col, row

        pil_img = Image.new("RGB", (img_size, img_size), color=(20, 20, 20))
        draw = ImageDraw.Draw(pil_img)

        # 1. Draw plot polygons, highlighting those influenced by neighbors
        for pn, row in plots_projected.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue

            reg_res = reg_results.get(str(pn))
            fill_color = (25, 25, 25)
            if reg_res and reg_res.blending_factor < 0.99:
                alpha = reg_res.blending_factor
                # Use HSL tailored color: blue glow for smoothed plots
                fill_color = (20, int(40 + (1.0 - alpha) * 40), int(60 + (1.0 - alpha) * 100))

            if geom.geom_type == "Polygon":
                coords = list(geom.exterior.coords)
                px_coords = [to_pixel(x, y) for x, y in coords]
                draw.polygon(px_coords, fill=fill_color, outline=(60, 60, 60))
            elif geom.geom_type == "MultiPolygon":
                for poly in geom.geoms:
                    coords = list(poly.exterior.coords)
                    px_coords = [to_pixel(x, y) for x, y in coords]
                    draw.polygon(px_coords, fill=fill_color, outline=(60, 60, 60))

        # 2. Draw neighbor graph adjacency connections
        for pn_i, neighbors in graph.items():
            if pn_i not in plots_projected.index:
                continue
            geom_i = plots_projected.loc[pn_i].geometry
            if geom_i is None or geom_i.is_empty:
                continue
            c_i = geom_i.centroid
            px_i = to_pixel(c_i.x, c_i.y)

            for pn_j in neighbors.keys():
                if pn_j not in plots_projected.index:
                    continue
                geom_j = plots_projected.loc[pn_j].geometry
                if geom_j is None or geom_j.is_empty:
                    continue
                c_j = geom_j.centroid
                px_j = to_pixel(c_j.x, c_j.y)

                draw.line([px_i, px_j], fill=(45, 55, 75), width=1)

        # 3. Draw displacement vectors (amplified 10x for visibility)
        vector_scale = 10.0
        for pn, reg_res in reg_results.items():
            if pn not in plots_projected.index:
                continue
            geom = plots_projected.loc[pn].geometry
            if geom is None or geom.is_empty:
                continue
            c = geom.centroid
            cx_px, cy_px = to_pixel(c.x, c.y)

            # Local shift vector (red)
            local_dx, local_dy = reg_res.local_shift
            lx_m = c.x + local_dx * vector_scale
            ly_m = c.y + local_dy * vector_scale
            lx_px, ly_px = to_pixel(lx_m, ly_m)

            # Regularized shift vector (green)
            reg_dx, reg_dy = reg_res.applied_shift
            rx_m = c.x + reg_dx * vector_scale
            ry_m = c.y + reg_dy * vector_scale
            rx_px, ry_px = to_pixel(rx_m, ry_m)

            # Draw original shift line and point
            draw.line([(cx_px, cy_px), (lx_px, ly_px)], fill=(220, 50, 50), width=1)
            draw.ellipse([lx_px - 2, ly_px - 2, lx_px + 2, ly_px + 2], fill=(220, 50, 50))

            # Draw regularized shift line and point
            draw.line([(cx_px, cy_px), (rx_px, ry_px)], fill=(50, 220, 50), width=2)
            draw.ellipse([rx_px - 3, ry_px - 3, rx_px + 3, ry_px + 3], fill=(50, 220, 50))

            # Draw correction link line if they shifted due to neighbors
            if np.sqrt((local_dx - reg_dx)**2 + (local_dy - reg_dy)**2) > 0.05:
                draw.line([(lx_px, ly_px), (rx_px, ry_px)], fill=(255, 200, 50), width=1)

        # 4. Draw legend panel
        draw.rectangle([10, 10, 320, 190], fill=(20, 20, 20), outline=(100, 100, 100))
        draw.text((20, 15), "Village Spatial Regularization Map", fill=(255, 255, 255))

        draw.rectangle([25, 45, 45, 55], fill=(25, 25, 25), outline=(60, 60, 60))
        draw.text((55, 43), "Standard Plot Polygon", fill=(200, 200, 200))

        draw.line([(20, 75), (45, 75)], fill=(45, 55, 75), width=1)
        draw.text((55, 68), "Adjacency Connection", fill=(200, 200, 200))

        draw.line([(20, 100), (45, 100)], fill=(220, 50, 50), width=1)
        draw.ellipse([43, 98, 47, 102], fill=(220, 50, 50))
        draw.text((55, 93), "Original Optimized Shift (10x)", fill=(200, 200, 200))

        draw.line([(20, 125), (45, 125)], fill=(50, 220, 50), width=2)
        draw.ellipse([42, 122, 48, 128], fill=(50, 220, 50))
        draw.text((55, 118), "Regularized Shift (10x)", fill=(200, 200, 200))

        draw.rectangle([25, 153, 45, 163], fill=(20, 80, 160), outline=(60, 60, 60))
        draw.text((55, 150), "Neighbor Smoothed (alpha < 1.0)", fill=(200, 200, 200))

        iterations = self.config.regularization_iterations
        outlier_thresh = getattr(self.config, "regularization_outlier_threshold_m", 5.0)
        draw.text((20, 175), f"Passes: {iterations} | Outlier Thresh: {outlier_thresh}m", fill=(130, 130, 130))

        out_path = out_dir / f"{village.slug}_regularizer_grid.png"
        pil_img.save(out_path)
        logger.debug(f"Saved SpatialRegularizer debug grid plot to: {out_path}")
        return out_path
