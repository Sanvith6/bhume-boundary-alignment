"""Confidence estimator module for the cadastral boundary correction pipeline.

This module estimates the reliability of optimized and spatially regularized transformations,
producing normalized confidence signals, weighted aggregation, and diagnostic plots.

Key behaviors:
- estimate() returns RAW confidence only. The coordinator applies the dynamic
  village-wide sigmoid stretch exactly once, after all plots are processed.
- Unavailable signals fall back to a NEUTRAL value (never to the total score,
  never to 1.0), or are excluded entirely with weight renormalization.
- A spatial ambiguity signal (distant-rival hypothesis gap) detects when the
  evidence supports two different fields, the main wrong-field-snapping guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw

from bhume.candidate_generator import CandidateTransformation
from bhume.config import Config
from bhume.optimizer import OptimizationResult, OptimizationStatistics
from bhume.regularizer import RegularizedOptimizationResult

logger = logging.getLogger(__name__)


@dataclass
class ConfidenceResult:
    """Dataclass holding confidence metrics and independent signals.

    Note: `confidence` holds the RAW aggregated value when produced by
    estimate(); the coordinator overwrites it with the dynamically stretched
    value for corrected plots at the end of the pipeline.
    """

    confidence: float
    optimization_quality: float
    neighbor_agreement: float
    area_consistency: float
    edge_strength: float
    boundary_coverage: float
    peak_gap: float
    entropy: float
    score_improvement: float
    support_signals: Dict[str, float]
    debug_metadata: Dict
    raw_confidence: float = 0.0


def stretch_confidence(
    raw_confidence: float,
    center: float,
    k: float,
    out_min: float = 0.10,
    out_max: float = 0.95,
) -> float:
    """Apply a dynamic, village-wide sigmoid stretch to raw confidence.

    `center` is the village's median raw confidence (sigmoid midpoint);
    `k` is a steepness derived from the village's IQR, so that the
    middle 50% of raw confidences spans roughly the middle of the
    [out_min, out_max] output range. No fixed bounds from any
    fixed sample are used -- this adapts per village.
    """
    sigmoid_val = 1.0 / (1.0 + np.exp(-k * (raw_confidence - center)))
    stretched = out_min + (out_max - out_min) * sigmoid_val
    return float(np.clip(stretched, out_min, out_max))



class ConfidenceEstimator:
    """Computes and aggregates spatial and geometric confidence signals."""

    def __init__(self, config: Config) -> None:
        """Initialize ConfidenceEstimator.

        Args:
            config: Pipeline configuration settings.
        """
        self.config = config

    def estimate(
        self,
        opt_res: OptimizationResult,
        reg_res: RegularizedOptimizationResult,
        final_score: float | None = None,
        final_feature_scores: Dict[str, float] | None = None,
    ) -> ConfidenceResult:
        """Estimate the alignment confidence for a single plot.

        Args:
            opt_res: Local OptimizationResult.
            reg_res: RegularizedOptimizationResult.
            final_score: Optional score at the regularized candidate position.
            final_feature_scores: Optional feature scores at the regularized candidate position.

        Returns:
            A populated ConfidenceResult object (confidence == raw, unstretched).
        """
        stats = opt_res.statistics
        best_cand = opt_res.best_candidate
        neutral = getattr(self.config, "confidence_neutral_value", 0.5)

        # Optimization quality with respect to actual regularized score
        actual_score = final_score if final_score is not None else opt_res.best_score

        # 1. Optimization quality (Q_local)
        quality_weights = getattr(self.config, "regularization_quality_weights", {
            "best_score": 0.5,
            "score_gap": 0.3,
            "entropy": 0.2
        })
        max_entropy = np.log(max(stats.candidate_count, 2))
        norm_entropy = stats.score_entropy / max_entropy if max_entropy > 0 else 0.0
        S_opt_quality = float(np.clip(
            quality_weights.get("best_score", 0.5) * actual_score +
            quality_weights.get("score_gap", 0.3) * stats.score_gap +
            quality_weights.get("entropy", 0.2) * (1.0 - norm_entropy),
            0.0, 1.0
        ))

        # 2. Peak gap S_peak_gap (adjacent-candidate gap; weak signal, low weight)
        sigma_gap = getattr(self.config, "confidence_sigma_gap", 0.1)
        S_peak_gap = float(np.clip(stats.score_gap / sigma_gap, 0.0, 1.0))

        # 3. Optimization entropy S_entropy
        S_entropy = float(np.clip(1.0 - norm_entropy, 0.0, 1.0))

        if final_feature_scores is not None:
            feature_scores = final_feature_scores
        elif opt_res.top_k_candidates:
            feature_scores = opt_res.top_k_candidates[0].feature_scores
        else:
            # Lookup ScoredCandidate in history matching best candidate
            best_scored = None
            min_dist = float('inf')
            for sc in opt_res.optimization_history:
                dist = np.hypot(sc.candidate.dx - best_cand.dx, sc.candidate.dy - best_cand.dy)
                if dist < min_dist:
                    min_dist = dist
                    best_scored = sc

            feature_scores = best_scored.feature_scores if best_scored else {}

        # 4. Area consistency S_area
        # Missing data must never default to "perfect"; use neutral.
        S_area = feature_scores.get("area_consistency", neutral)

        # 5. Neighbor agreement & 6. Consensus strength
        # No neighbors = no corroborating evidence: EXCLUDE these signals from
        # aggregation (None) instead of granting a free high value.
        contribs = reg_res.neighbor_statistics.get("neighbor_contributions", [])
        neighbor_count = len(contribs)

        if neighbor_count > 0:
            # Neighbor agreement
            local_dx, local_dy = reg_res.local_shift
            neigh_dx, neigh_dy = reg_res.neighbor_shift
            neigh_dist = np.hypot(local_dx - neigh_dx, local_dy - neigh_dy)
            sigma_neigh = getattr(self.config, "confidence_sigma_neighbor_dist", 2.0)
            S_neighbor_agreement = float(np.exp(-neigh_dist / sigma_neigh))

            # Consensus strength
            q_avg = np.mean([c["quality"] for c in contribs])
            if neighbor_count > 1:
                shifts_dx = [c["shift"][0] for c in contribs]
                shifts_dy = [c["shift"][1] for c in contribs]
                var_x = np.var(shifts_dx)
                var_y = np.var(shifts_dy)
                variance = var_x + var_y
            else:
                variance = 0.0
            sigma_var = getattr(self.config, "confidence_sigma_neighbor_variance", 4.0)
            f_var = float(np.exp(-variance / sigma_var))
            S_consensus_strength = float(q_avg * f_var)
        else:
            S_neighbor_agreement = None  # unavailable: excluded, weights renormalized
            S_consensus_strength = None

        # 7. Edge strength S_edge_strength (distance transform)
        # Fallback must be NEUTRAL, never actual_score: defaulting to the total
        # score makes "independent" signals echo the match score, which is
        # exactly what cannot distinguish a confident wrong match.
        S_edge_strength = feature_scores.get("distance_transform", neutral)

        # 8. Boundary coverage S_boundary_coverage (contour similarity)
        S_boundary_coverage = feature_scores.get("contour_similarity", neutral)

        # 9. Gradient agreement S_gradient_agreement
        S_gradient_agreement = feature_scores.get("gradient_agreement", neutral)

        # Official baseline score. None = genuinely unknown; never fall back to
        # min(scores) (maximally flattering) or treat a real 0.0 as missing.
        official_score = getattr(opt_res, "official_score", None)
        if official_score is None:
            official_candidates = [sc for sc in opt_res.optimization_history
                                   if abs(sc.candidate.dx) < 1e-5 and abs(sc.candidate.dy) < 1e-5]
            if official_candidates:
                official_score = official_candidates[0].total_score

        # 10. Score improvement S_score_improvement
        if official_score is None:
            S_score_improvement = neutral  # baseline unknown -> neutral, never flattering
        else:
            score_improv = max(actual_score - official_score, 0.0)
            sigma_improv = getattr(self.config, "confidence_sigma_improvement", 0.05)
            S_score_improvement = float(np.clip(score_improv / sigma_improv, 0.0, 1.0))

        # 11. Basin stability S_basin_stability
        S_basin_stability = None
        if hasattr(stats, 'basin_stability') and stats.basin_stability is not None:
            S_basin_stability = float(stats.basin_stability.stability_score)

        # 12. Spatial ambiguity S_ambiguity: does a DISTANT hypothesis score
        # nearly as well as the best? Catches "two plausible fields" in crowded
        # villages, the wrong-field snapping failure mode.
        S_ambiguity = None
        clusters = getattr(opt_res, "hypothesis_clusters", None) or []
        if len(clusters) >= 2:
            bx, by, bs = clusters[0]
            amb_dist = getattr(self.config, "ambiguity_distance_m", 5.0)
            distant_scores = [
                cs for (cx, cy, cs) in clusters[1:]
                if np.hypot(cx - bx, cy - by) > amb_dist
            ]
            if distant_scores:
                rival = max(distant_scores)
                if bs > 1e-9:
                    S_ambiguity = float(np.clip((bs - rival) / (bs * 
                        getattr(self.config, "ambiguity_score_ratio", 0.15)), 0.0, 1.0))
                else:
                    S_ambiguity = 0.0
            else:
                S_ambiguity = 1.0  # no distant rival -> unambiguous
        elif len(clusters) == 1:
            S_ambiguity = 1.0  # single hypothesis -> unambiguous

        # 13. Optimizer stability S_stability
        S_stability = 1.0
        converged = getattr(stats, "converged", True)
        opt_residual = getattr(stats, "optimization_residual", 0.0)
        final_step = getattr(stats, "final_step_size", 0.05)
        init_agreement = getattr(stats, "init_agreement", 1.0)
        if not converged:
            S_stability *= 0.6
        if opt_residual > 0.0:
            step = final_step if final_step > 0 else 0.05
            S_stability *= float(np.exp(-max(0.0, opt_residual - step) / 0.2))
        S_stability *= init_agreement
        S_stability = float(np.clip(S_stability, 0.0, 1.0))

        # 14. Multi-scale agreement S_scale_agreement
        S_scale_agreement = None
        c_shift = getattr(stats, "coarse_best_shift", None)
        m_shift = getattr(stats, "medium_best_shift", None)
        f_shift = getattr(stats, "fine_best_shift", None)

        if c_shift is not None and m_shift is not None and f_shift is not None:
            d_cm = np.hypot(c_shift[0] - m_shift[0], c_shift[1] - m_shift[1])
            d_mf = np.hypot(m_shift[0] - f_shift[0], m_shift[1] - f_shift[1])
            d_cf = np.hypot(c_shift[0] - f_shift[0], c_shift[1] - f_shift[1])
            max_dist = max(d_cm, d_mf, d_cf)
            S_scale_agreement = float(np.exp(-max_dist / 1.0))
        elif m_shift is not None and f_shift is not None:
            d_mf = np.hypot(m_shift[0] - f_shift[0], m_shift[1] - f_shift[1])
            S_scale_agreement = float(np.exp(-d_mf / 1.0))
        elif c_shift is not None and f_shift is not None:
            d_cf = np.hypot(c_shift[0] - f_shift[0], c_shift[1] - f_shift[1])
            S_scale_agreement = float(np.exp(-d_cf / 1.0))

        if S_scale_agreement is not None:
            S_scale_agreement = float(np.clip(S_scale_agreement, 0.0, 1.0))
            if S_scale_agreement < 0.20:
                S_ambiguity = 0.0

        # Signals map (None = unavailable, excluded from aggregation)
        signals = {
            "opt_quality": S_opt_quality,
            "peak_gap": S_peak_gap,
            "ambiguity": S_ambiguity,
            "entropy": S_entropy,
            "area_consistency": S_area,
            "neighbor_agreement": S_neighbor_agreement,
            "consensus_strength": S_consensus_strength,
            "edge_strength": S_edge_strength,
            "boundary_coverage": S_boundary_coverage,
            "gradient_agreement": S_gradient_agreement,
            "score_improvement": S_score_improvement,
            "basin_stability": S_basin_stability,
            "stability": S_stability,
            "scale_agreement": S_scale_agreement,
        }

        # Compute E: Image evidence combining DT (edge strength), boundary coverage, gradient agreement,
        # and local opt quality.
        S_edge_strength = feature_scores.get("distance_transform", neutral)
        S_boundary_coverage = feature_scores.get("contour_similarity", neutral)
        S_gradient_agreement = feature_scores.get("gradient_agreement", neutral)

        # Edge Contrast Re-Evaluation: Boost edge weighting if texture is low but borders are strong
        edge_weight = 0.4
        grad_weight = 0.1
        opt_weight = 0.2
        if S_edge_strength > 0.5 and S_gradient_agreement < 0.4:
            edge_weight = 0.6
            grad_weight = 0.0
            opt_weight = 0.1

        E = float(np.clip(
            edge_weight * S_edge_strength +
            0.3 * S_boundary_coverage +
            grad_weight * S_gradient_agreement +
            opt_weight * S_opt_quality,
            0.0, 1.0
        ))

        # Neighbor consistency computation (formerly part of G_neigh)
        if neighbor_count > 0:
            local_dx, local_dy = reg_res.local_shift
            neigh_dx, neigh_dy = reg_res.neighbor_shift
            neigh_dist = np.hypot(local_dx - neigh_dx, local_dy - neigh_dy)
            
            # Smart Neighbor-Agreement: Do not penalize if local signal was garbage
            if actual_score < 0.15:
                neigh_dist = 0.0

            sigma_neigh = getattr(self.config, "confidence_sigma_neighbor_dist", 2.0)
            sigma_neigh_eff = sigma_neigh * (1.0 + 2.0 * E)
            S_neighbor_agreement_val = float(np.exp(-0.5 * (neigh_dist / sigma_neigh_eff) ** 2))
            S_neighbor_agreement = S_neighbor_agreement_val

        # Translation magnitude consistency computation (formerly part of G_trans)
        local_dx, local_dy = reg_res.applied_shift
        applied_dist = float(np.hypot(local_dx, local_dy))
        sigma_trans = getattr(self.config, "confidence_sigma_translation", 5.0)
        sigma_trans_eff = sigma_trans * (1.0 + 2.0 * E)
        S_translation = float(np.exp(-0.5 * (applied_dist / sigma_trans_eff) ** 2))

        # Drift consistency computation (formerly G_drift)
        if neighbor_count > 0:
            pred_dx, pred_dy = reg_res.neighbor_shift
            applied_dx, applied_dy = reg_res.applied_shift
            d_drift = float(np.hypot(applied_dx - pred_dx, applied_dy - pred_dy))
            sigma_drift = getattr(self.config, "confidence_sigma_drift", 4.0)
            S_drift_agreement = float(np.exp(-0.5 * (d_drift / sigma_drift) ** 2))
            G_drift = 0.6 + 0.4 * S_drift_agreement
        else:
            G_drift = 1.0

        # Repeatability consistency computation (formerly G_rep)
        repeatability_score = init_agreement
        G_rep = 0.6 + 0.4 * repeatability_score

        # Add G_drift and G_rep to signals mapping (needed for visualization/diagnostics)
        signals["drift"] = G_drift
        signals["repeatability"] = G_rep

        # Compute raw confidence using 12 Attenuated Multiplicative Gates (Change A)
        alpha = 0.15
        signals_list = [
            E,                          # 1. image_evidence
            S_score_improvement,        # 2. score_improvement
            S_peak_gap,                 # 3. score_gap
            S_ambiguity if S_ambiguity is not None else 1.0,  # 4. ambiguity_index
            float(np.clip(opt_res.best_candidate.metadata.get("peak_sharpness", 0.05) / 0.1, 0.0, 1.0)), # 5. peak_sharpness
            S_basin_stability if S_basin_stability is not None else 0.5, # 6. basin_stability
            init_agreement,             # 7. repeatability
            S_stability,                # 8. optimizer_residual
            S_boundary_coverage,        # 9. edge_continuity
            S_area,                     # 10. area_consistency
            (float(np.clip(0.4 * S_neighbor_agreement + 0.3 * S_consensus_strength + 0.3 * S_drift_agreement, 0.0, 1.0))
             if neighbor_count > 0 else 1.0), # 11. neighbor_consistency
            S_translation               # 12. translation_magnitude
        ]

        raw_confidence = 1.0
        for s in signals_list:
            gate = 1.0 - alpha * (1.0 - s)
            raw_confidence *= gate

        # Zero suppression: only kill confidence for genuinely pathological conditions
        # (e.g. zero edge strength/image evidence)
        if S_edge_strength < 1e-9:
            confidence = 0.0
            raw_confidence = 0.0
        else:
            # Boost confidence slightly if scale agreement is high:
            if S_scale_agreement is not None and S_scale_agreement > 0.90:
                confidence = min(1.0, raw_confidence + 0.03)
            else:
                confidence = raw_confidence

        # For the visualization-friendly fields, substitute neutral for None
        viz_neighbor = S_neighbor_agreement if S_neighbor_agreement is not None else neutral


        res = ConfidenceResult(
            confidence=confidence,
            optimization_quality=S_opt_quality,
            neighbor_agreement=viz_neighbor,
            area_consistency=S_area,
            edge_strength=S_edge_strength,
            boundary_coverage=S_boundary_coverage,
            peak_gap=S_peak_gap,
            entropy=S_entropy,
            score_improvement=S_score_improvement,
            support_signals=signals,
            debug_metadata={"alpha": alpha},
            raw_confidence=raw_confidence,
        )

        if self.config.debug_visualize:
            self.visualize_confidence_profile(opt_res.best_candidate.metadata.get("plot_number", "unknown"), res)

        return res

    def visualize_confidence_profile(self, plot_number: str, result: ConfidenceResult) -> Path:
        """Save a diagnostics bar chart for a single plot's confidence profile."""
        out_dir = Path(self.config.debug_out_dir) / "confidence"
        out_dir.mkdir(parents=True, exist_ok=True)

        img_w, img_h = 600, 700
        pil_img = Image.new("RGB", (img_w, img_h), color=(20, 20, 20))
        draw = ImageDraw.Draw(pil_img)

        # Title
        draw.text((20, 15), f"Plot {plot_number} Confidence Profile Diagnostics", fill=(255, 255, 255))

        sig = result.support_signals

        def _val(key: str, fallback: float = 0.0) -> float:
            v = sig.get(key)
            return float(v) if v is not None else fallback

        # Display signals + final confidence
        items = [
            ("Optimization Quality", result.optimization_quality, False),
            ("Peak Gap", result.peak_gap, False),
            ("Ambiguity (rival gap)", _val("ambiguity"), False),
            ("Search Entropy", result.entropy, False),
            ("Area Consistency", result.area_consistency, False),
            ("Neighbor Agreement", result.neighbor_agreement, False),
            ("Consensus Strength", _val("consensus_strength"), False),
            ("Edge Strength", result.edge_strength, False),
            ("Boundary Coverage", result.boundary_coverage, False),
            ("Gradient Agreement", _val("gradient_agreement"), False),
            ("Score Improvement", result.score_improvement, False),
            ("Optimizer Stability", _val("stability"), False),
            ("Scale Agreement", _val("scale_agreement"), False),
            ("Drift Agreement Gate", _val("drift"), False),
            ("Repeatability Gate", _val("repeatability"), False),
            ("RAW CONFIDENCE", result.raw_confidence, True)
        ]

        bar_y_start = 50
        bar_height = 18
        bar_spacing = 38
        bar_w_max = 300

        for idx, (label, val, is_final) in enumerate(items):
            y = bar_y_start + idx * bar_spacing

            # Label
            draw.text((20, y + 3), label, fill=(200, 200, 200) if not is_final else (0, 255, 255))

            # Background bar
            bx1 = 200
            by1 = y
            bx2 = bx1 + bar_w_max
            by2 = y + bar_height
            draw.rectangle([bx1, by1, bx2, by2], fill=(40, 40, 40), outline=(60, 60, 60))

            # Foreground bar
            if val > 0:
                fg_w = int(val * bar_w_max)
                fx2 = bx1 + fg_w

                if is_final:
                    fill_color = (0, 180, 255)
                else:
                    r = int((1.0 - val) * 255)
                    g = int(val * 255)
                    fill_color = (r, g, 50)

                draw.rectangle([bx1, by1, fx2, by2], fill=fill_color)

            # Value label
            draw.text((bx2 + 15, y + 3), f"{val:.4f}", fill=(255, 255, 255) if not is_final else (0, 255, 255))

        # Border
        draw.rectangle([5, 5, img_w - 5, img_h - 5], outline=(100, 100, 100), width=1)

        sanitized_plot_number = str(plot_number).replace("/", "_")
        out_path = out_dir / f"plot_{sanitized_plot_number}_confidence_profile.png"
        pil_img.save(out_path)
        return out_path

    def visualize_village_histogram(self, village_slug: str, results: Dict[str, ConfidenceResult]) -> Path:
        """Save a vertical bar histogram of confidence values across the village."""
        out_dir = Path(self.config.debug_out_dir) / "confidence"
        out_dir.mkdir(parents=True, exist_ok=True)

        img_w, img_h = 600, 400
        pil_img = Image.new("RGB", (img_w, img_h), color=(20, 20, 20))
        draw = ImageDraw.Draw(pil_img)

        # Title
        draw.text((20, 15), f"Village {village_slug} Confidence Distribution Histogram", fill=(255, 255, 255))

        # Count values in 10 bins
        conf_vals = [res.confidence for res in results.values()]
        bins = np.linspace(0.0, 1.0, 11)
        counts, _ = np.histogram(conf_vals, bins=bins)

        max_count = max(counts) if len(counts) > 0 and max(counts) > 0 else 1

        # Histogram dimensions
        graph_x1 = 60
        graph_y1 = 60
        graph_x2 = img_w - 40
        graph_y2 = img_h - 60
        graph_w = graph_x2 - graph_x1
        graph_h = graph_y2 - graph_y1

        # Draw axes
        draw.line([(graph_x1, graph_y1), (graph_x1, graph_y2)], fill=(120, 120, 120), width=1)
        draw.line([(graph_x1, graph_y2), (graph_x2, graph_y2)], fill=(120, 120, 120), width=1)

        # Draw Y-axis ticks and grid lines
        y_ticks = 4
        for ticks in range(y_ticks + 1):
            f_tick = ticks / y_ticks
            y_val = int(graph_y2 - f_tick * graph_h)
            tick_count = int(f_tick * max_count)
            draw.line([(graph_x1, y_val), (graph_x2, y_val)], fill=(40, 40, 40), width=1)
            draw.text((graph_x1 - 35, y_val - 5), str(tick_count), fill=(150, 150, 150))

        # Draw bars
        bin_width = graph_w / 10
        for i in range(10):
            count = counts[i]
            bar_h = int((count / max_count) * graph_h)

            bx1 = int(graph_x1 + i * bin_width + 4)
            bx2 = int(graph_x1 + (i + 1) * bin_width - 4)
            by1 = graph_y2 - bar_h
            by2 = graph_y2

            # Bar color: Red (low conf) to Green (high conf) gradient
            c_val = (i + 0.5) / 10.0
            r = int((1.0 - c_val) * 255)
            g = int(c_val * 255)
            fill_color = (r, g, 50)

            if bar_h > 0:
                draw.rectangle([bx1, by1, bx2, by2], fill=fill_color, outline=(100, 100, 100))

            bin_center_x = (bx1 + bx2) // 2
            label_x = f"{bins[i]:.1f}"
            draw.text((bin_center_x - 10, graph_y2 + 8), label_x, fill=(150, 150, 150))

        draw.text((graph_x2 - 10, graph_y2 + 8), "1.0", fill=(150, 150, 150))

        # Legend/Stats text
        num_plots = len(conf_vals)
        mean_conf = np.mean(conf_vals) if num_plots > 0 else 0.0
        std_conf = np.std(conf_vals) if num_plots > 0 else 0.0
        draw.text((graph_x2 - 200, 30), f"Plots count: {num_plots}", fill=(180, 180, 180))
        draw.text((graph_x2 - 200, 45), f"Mean Conf: {mean_conf:.4f} | Std: {std_conf:.4f}", fill=(180, 180, 180))

        out_path = out_dir / f"village_{village_slug}_confidence_histogram.png"
        pil_img.save(out_path)
        return out_path
