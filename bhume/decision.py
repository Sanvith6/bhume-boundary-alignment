"""Decision engine module for the cadastral boundary correction pipeline.

Determines whether to accept a proposed geometric shift or flag it for human review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry.base import BaseGeometry
from shapely.affinity import translate
from shapely.ops import transform as shp_transform
from pyproj import Transformer

from bhume.config import Config
from bhume.optimizer import OptimizationResult
from bhume.regularizer import RegularizedOptimizationResult
from bhume.confidence import ConfidenceResult

logger = logging.getLogger(__name__)


@dataclass
class DecisionResult:
    """Dataclass holding the decision outcome for a plot."""

    plot_number: str
    status: str  # "corrected" or "flagged"
    confidence: float
    final_geometry: BaseGeometry
    applied_shift: Tuple[float, float]
    decision_reason: List[str]
    decision_signals: Dict[str, float]
    score_improvement: float
    correctability_score: float
    decision_trace: Dict
    debug_metadata: Dict
    method_note: str | None = None
    decision_score: float = 0.0


class DecisionEngine:
    """Consumes optimization, regularization, and confidence outputs to make the final decision."""

    def __init__(self, config: Config) -> None:
        """Initialize DecisionEngine with a Config instance."""
        self.config = config

    def decide(
        self,
        opt_res: OptimizationResult,
        reg_res: RegularizedOptimizationResult,
        conf_res: ConfidenceResult,
        original_geom: BaseGeometry,
        transformer: object = None,
        final_score: float | None = None,
        village_shift_mad: float | None = None,
    ) -> DecisionResult:
        """Make the final corrected vs flagged decision for a plot.

        Args:
            opt_res: Local OptimizationResult.
            reg_res: RegularizedOptimizationResult.
            conf_res: ConfidenceResult.
            original_geom: The official input boundary geometry (EPSG:4326).
            transformer: Optional CoordinateTransformer mapping to raster projection.
            final_score: Optional score at the final (regularized) position.
            village_shift_mad: Optional MAD of accepted shifts across the village,
                used to scale the neighbor-disagreement veto.

        Returns:
            A populated DecisionResult.
        """
        plot_number = opt_res.best_candidate.metadata.get("plot_number", "unknown")
        vetoes = []
        applied_shift = (0.0, 0.0)
        final_geom = original_geom

        # 1. Topological validity check of the original geometry
        if not original_geom.is_valid:
            vetoes.append("invalid_original_geometry")

        # Official baseline score. None means genuinely unknown; never fall back
        # to min(scores) or 0.0, which would inflate the apparent improvement.
        official_score = getattr(opt_res, "official_score", None)
        if official_score is None:
            zero_cands = [sc for sc in opt_res.optimization_history
                          if abs(sc.candidate.dx) < 1e-5 and abs(sc.candidate.dy) < 1e-5]
            if zero_cands:
                official_score = zero_cands[0].total_score

        actual_score = final_score if final_score is not None else opt_res.best_score

        if official_score is None:
            # Cannot measure improvement against the official position: be conservative.
            raw_score_improvement = 0.0
            vetoes.append("missing_official_baseline")
        else:
            raw_score_improvement = max(actual_score - official_score, 0.0)

        # 2. Correctability score calculation
        # Note: correctability_score is computed for diagnostic/telemetry purposes (e.g. tracking
        # in decision_trace and plotting in the diagnostic scatter panel) but does not feed into
        # the safety vetoes or the final decision_score.
        signals = {
            "confidence": conf_res.raw_confidence,
            "score_improvement": conf_res.score_improvement,
            "optimization_quality": conf_res.optimization_quality,
            "area_consistency": conf_res.area_consistency,
            "neighbor_agreement": conf_res.neighbor_agreement,
        }

        weights = getattr(
            self.config,
            "correctability_signal_weights",
            {
                "confidence": 0.3,
                "score_improvement": 0.2,
                "optimization_quality": 0.2,
                "area_consistency": 0.15,
                "neighbor_agreement": 0.15,
            },
        )

        total_w = sum(weights.values())
        if total_w > 0:
            norm_weights = {k: v / total_w for k, v in weights.items()}
        else:
            norm_weights = {k: 1.0 / len(weights) for k in weights}

        # Compute weighted geometric mean (None-valued signals treated as neutral)
        eps = 1e-12
        neutral = getattr(self.config, "confidence_neutral_value", 0.5)
        safe_signals = {k: (v if v is not None else neutral) for k, v in signals.items()}
        log_sum = sum(
            norm_weights.get(k, 0.0) * np.log(max(safe_signals[k], eps)) for k in safe_signals
        )
        correctability_score = float(np.exp(log_sum))

        # Check if any weight-bearing signal is zero
        for k, w in norm_weights.items():
            if w > 0.0 and safe_signals[k] < 1e-9:
                correctability_score = 0.0
                break

        # 3. Apply safety vetoes
        dx, dy = reg_res.applied_shift
        shift_magnitude = float(np.hypot(dx, dy))

        # Check hard physical vetoes first
        if not original_geom.is_valid:
            vetoes.append("invalid_original_geometry")
            
        # Calculate d_eq for required_improv calculation later
        map_area_m2 = opt_res.best_candidate.metadata.get("map_area_m2")
        if map_area_m2 is not None and map_area_m2 > 0:
            d_eq = 2.0 * np.sqrt(map_area_m2 / np.pi)
        else:
            d_eq = 2.0 * np.sqrt(original_geom.area / np.pi)

        # Calculate dynamic limit for extreme translation based on D_eq and perimeter
        dynamic_search_max = opt_res.best_candidate.metadata.get("max_allowed_radius", 15.0)

        if official_score is None:
            raw_score_improvement = 0.0
            vetoes.append("missing_official_baseline")
        else:
            raw_score_improvement = max(actual_score - official_score, 0.0)
            if actual_score <= official_score:
                vetoes.append("worse_than_official")

        if opt_res.best_candidate.metadata.get("area_ratio_pre_filter_flagged", False):
            vetoes.append("area_mismatch")

        if shift_magnitude < self.config.threshold_already_correct:
            vetoes.append("already_correct")

        if shift_magnitude > dynamic_search_max + 1e-4:
            vetoes.append("extreme_translation")

        # Soft evidence accumulation (Part 2)
        dt_score = conf_res.edge_strength
        edge_continuity = conf_res.boundary_coverage
        basin_width = opt_res.statistics.basin_stability.basin_width if opt_res.statistics.basin_stability is not None else 0.5
        ambiguity_signal = conf_res.support_signals.get("ambiguity")
        
        contribs = reg_res.neighbor_statistics.get("neighbor_contributions", [])
        disagreement = 0.0
        if contribs:
            ldx, ldy = reg_res.local_shift
            ndx, ndy = reg_res.neighbor_shift
            disagreement = float(np.hypot(ldx - ndx, ldy - ndy))

        # Soft evidence accumulation (Part 2)
        dt_score = conf_res.edge_strength
        if opt_res.best_score < 0.02:
            vetoes.append("weak_image_evidence_hard_veto")
            
        # Distance-penalized score improvement
        required_improv = 0.03 + 0.05 * (shift_magnitude / max(1.0, d_eq))
        p_improv = float(np.clip(raw_score_improvement / required_improv, 0.0, 1.0))
        p_image = float(np.clip(dt_score, 0.0, 1.0))
        p_gap = float(np.clip(opt_res.statistics.score_gap / 0.1, 0.0, 1.0))
        p_rep = float(np.clip(opt_res.statistics.init_agreement, 0.0, 1.0))
        p_sharp = float(np.clip(opt_res.best_candidate.metadata.get("peak_sharpness", 0.05) / 0.1, 0.0, 1.0))
        p_basin = float(np.clip(basin_width, 0.0, 1.0))

        # Negative risk signals (all normalized to [0, 1])
        n_amb = float(np.clip(1.0 - (ambiguity_signal if ambiguity_signal is not None else 1.0), 0.0, 1.0))
        n_edges = float(np.clip(1.0 - edge_continuity, 0.0, 1.0))
        n_trans = float(np.clip(shift_magnitude / dynamic_search_max, 0.0, 1.0))
        
        mad_val = village_shift_mad if (village_shift_mad is not None and village_shift_mad > 1e-6) else 2.5
        n_neigh = float(np.clip(disagreement / (3.0 * mad_val), 0.0, 1.0)) if contribs else 0.0
        n_resid = float(np.clip(opt_res.statistics.optimization_residual / 0.5, 0.0, 1.0))
        n_area = float(np.clip(1.0 - conf_res.area_consistency, 0.0, 1.0))
        
        pass_instability = opt_res.best_candidate.metadata.get("pass_instability", False)
        n_instab = 1.0 if pass_instability else 0.0

        # Weighted log-odds combination
        log_odds = (
            3.0
            + 4.0 * p_improv
            + 2.0 * p_image
            + 1.5 * p_gap
            + 1.5 * p_rep
            + 1.5 * p_sharp
            + 1.0 * p_basin
            - (
                4.0 * n_amb
                + 1.5 * n_edges
                + 1.5 * n_trans
                + 3.0 * n_neigh
                + 1.5 * n_resid
                + 1.0 * n_area
                + 3.0 * n_instab
            )
        )
        decision_score = float(1.0 / (1.0 + np.exp(-log_odds)))

        # Flag if overall decision score is low (unless already flagged by hard vetoes)
        has_hard_veto = len(vetoes) > 0
        if decision_score < 0.35 and not has_hard_veto:
            # We flag due to low overall evidence / high risks.
            # To preserve dynamic diagnostic reasons, append component veto reasons:
            if p_image < 0.25:
                vetoes.append("weak_image_evidence")
            if p_improv < 0.20:
                vetoes.append("insufficient_improvement")
            if n_amb > 0.40:
                vetoes.append("ambiguous_alignment")
            if n_neigh > 0.40:
                vetoes.append("neighbor_disagreement")
            if n_edges > 0.60 or p_basin < 0.30:
                vetoes.append("flat_basin_weak_edges")
            if n_instab > 0.5:
                vetoes.append("pass_instability")
            if n_resid > 0.6:
                vetoes.append("large_optimization_residual")
            if n_area > 0.5:
                vetoes.append("low_area_consistency")
            
            # If none of the specific reasons triggered but score is still < 0.5
            if not vetoes or all(v in ("invalid_original_geometry", "missing_official_baseline") for v in vetoes):
                vetoes.append("low_correctability")

        # If no vetoes, construct translated geometry and validate
        if not vetoes:
            try:
                if transformer is not None:
                    geom_crs = transformer.geom_to_crs(original_geom)
                    translated_crs = translate(geom_crs, xoff=dx, yoff=dy)
                    candidate_geom = transformer.geom_to_lonlat(translated_crs)
                else:
                    # Fallback UTM transformer
                    lon = original_geom.centroid.x
                    utm_zone = f"EPSG:{32600 + int((lon + 180) // 6) + 1}"
                    to_utm = Transformer.from_crs("EPSG:4326", utm_zone, always_xy=True)
                    to_4326 = Transformer.from_crs(utm_zone, "EPSG:4326", always_xy=True)
                    geom_utm = shp_transform(to_utm.transform, original_geom)
                    translated_utm = translate(geom_utm, xoff=dx, yoff=dy)
                    candidate_geom = shp_transform(to_4326.transform, translated_utm)

                if not candidate_geom.is_valid:
                    vetoes.append("invalid_final_geometry")
                else:
                    final_geom = candidate_geom
                    applied_shift = (dx, dy)
            except Exception as e:
                logger.error(f"Error during geometry translation for plot {plot_number}: {e}")
                vetoes.append("translation_error")

        status = "corrected" if not vetoes else "flagged"
        confidence = conf_res.confidence
        method_note = None

        if vetoes:
            # For AUC calibration metrics, confidence represents the confidence that the shift
            # is a "good fix". Since flagged plots are 0-meter shifts, they must be ranked
            # at the absolute bottom of the confidence ladder to prevent inverted AUC.
            confidence = 0.0
            
            # Setup method note for flagged cases
            if "area_mismatch" in vetoes:
                area_ratio = opt_res.best_candidate.metadata.get("area_ratio", 0.0)
                method_note = f"Area mismatch: drawn/recorded ratio = {area_ratio:.2f}"
            elif "ambiguous_alignment" in vetoes:
                # Resolve ambiguous_rival if possible
                ambiguous_rival = None
                clusters = getattr(opt_res, "hypothesis_clusters", []) or []
                if len(clusters) >= 2:
                    bx, by, bs = clusters[0]
                    amb_dist = getattr(self.config, "ambiguity_distance_m", 5.0)
                    amb_ratio = getattr(self.config, "ambiguity_score_ratio", 0.15)
                    for (cx, cy, cs) in clusters[1:]:
                        d = float(np.hypot(cx - bx, cy - by))
                        if d > amb_dist and bs > 1e-9 and (bs - cs) / bs < amb_ratio:
                            ambiguous_rival = (d, (bs - cs) / bs * 100.0)
                            break
                if ambiguous_rival is not None:
                    method_note = (
                        f"Ambiguous: competing alignment {ambiguous_rival[0]:.1f}m away "
                        f"scores within {ambiguous_rival[1]:.0f}% of best"
                    )
                else:
                    method_note = "Ambiguous: multiple distant alignment hypotheses score similarly"
            elif "neighbor_disagreement" in vetoes:
                method_note = (
                    f"Local shift disagrees with neighborhood consensus by "
                    f"{disagreement:.1f}m without a dominant peak"
                )
            elif "pass_instability" in vetoes:
                method_note = f"Multi-pass instability: Pass-2 shift is large ({shift_magnitude:.2f}m) while Pass-1 shift was near-zero"
            elif "already_correct" in vetoes:
                method_note = f"Plot appears already correctly positioned (shift={shift_magnitude:.2f}m)"
            elif "insufficient_improvement" in vetoes:
                method_note = "Correction did not meaningfully improve alignment over official position"
            elif "weak_image_evidence" in vetoes:
                method_note = f"Weak image evidence: alignment score {dt_score:.3f} is too low"
            elif "flat_basin_weak_edges" in vetoes:
                method_note = "Weak edge continuity and flat optimization basin: geometry shift is unreliable"
            elif "extreme_translation" in vetoes:
                method_note = f"Shift distance ({shift_magnitude:.2f}m) exceeds maximum village search radius"
            elif "worse_than_official" in vetoes:
                method_note = "Best candidate scores no better than the official position"
            elif "missing_official_baseline" in vetoes:
                method_note = "Could not score the official position; improvement unverifiable"
            elif "low_correctability" in vetoes or "low_confidence" in vetoes:
                method_note = "Ambiguous alignment landscape: low confidence in local optimization match"
            elif "invalid_original_geometry" in vetoes:
                method_note = "Original geometry is invalid (self-intersecting or malformed)"
            elif "invalid_final_geometry" in vetoes or "translation_error" in vetoes:
                method_note = "Translated geometry failed validity checks"

        # Record decision trace
        decision_trace = {
            "plot_number": plot_number,
            "correctability_score": correctability_score,
            "raw_score_improvement": raw_score_improvement,
            "confidence": confidence,
            "optimized_score": opt_res.best_score,
            "official_score": official_score,
            "applied_shift": applied_shift,
            "shift_magnitude": shift_magnitude,
            "neighbor_disagreement_m": disagreement,
            "vetoes": vetoes.copy(),
            "weights": norm_weights,
            "signals": signals.copy(),
            # Diagnostic variables:
            "score_gap": opt_res.statistics.score_gap,
            "ambiguity_index": 1.0 - (conf_res.support_signals.get("ambiguity") if conf_res.support_signals.get("ambiguity") is not None else 1.0),
            "peak_sharpness": opt_res.best_candidate.metadata.get("peak_sharpness", 0.0),
            "basin_width": opt_res.statistics.basin_stability.basin_width if opt_res.statistics.basin_stability is not None else 0.5,
            "scale_agreement": conf_res.support_signals.get("scale_agreement"),
            "repeatability_score": opt_res.statistics.init_agreement,
            "predicted_drift": reg_res.neighbor_shift,
            "decision_score": decision_score,
            "neighbor_score": conf_res.neighbor_agreement,
            "edge_score": dt_score,
            "area_score": conf_res.area_consistency,
        }

        return DecisionResult(
            plot_number=plot_number,
            status=status,
            confidence=confidence,
            final_geometry=final_geom,
            applied_shift=applied_shift,
            decision_reason=vetoes,
            decision_signals=signals,
            score_improvement=raw_score_improvement,
            correctability_score=correctability_score,
            decision_trace=decision_trace,
            debug_metadata={},
            method_note=method_note,
            decision_score=decision_score,
        )

    def generate_diagnostics(
        self, village_slug: str, results: Dict[str, DecisionResult]
    ) -> List[Path]:
        """Generate 6 PIL-based diagnostic plots and save them in the debug output folder.

        Plots:
        1. Pie chart: Corrected vs Flagged
        2. Histogram: Confidence values
        3. Histogram: Raw score improvements
        4. Scatter plot: Confidence vs Raw Score Improvement
        5. Scatter plot: Correctability vs Confidence
        6. Horizontal bar chart: Decision failure reasons

        Args:
            village_slug: Identifier for the village.
            results: Dict mapping plot numbers to DecisionResults.

        Returns:
            List of paths to the generated plots.
        """
        out_dir = Path(self.config.debug_out_dir) / "decision"
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []

        total_plots = len(results)
        if total_plots == 0:
            return paths

        corrected_count = sum(1 for r in results.values() if r.status == "corrected")
        flagged_count = total_plots - corrected_count

        # ----------------------------------------------------
        # Chart 1: Pie chart of corrected vs flagged
        # ----------------------------------------------------
        img_w, img_h = 500, 400
        img = Image.new("RGB", (img_w, img_h), color=(20, 20, 20))
        draw = ImageDraw.Draw(img)
        draw.text((20, 15), f"Village {village_slug}: Corrected vs Flagged Decisions", fill=(255, 255, 255))

        # Draw pie circle inside bounding box
        cx, cy, r = 200, 200, 120
        bbox = [cx - r, cy - r, cx + r, cy + r]

        if corrected_count == 0:
            draw.ellipse(bbox, fill=(220, 70, 70), outline=(255, 255, 255))
        elif flagged_count == 0:
            draw.ellipse(bbox, fill=(50, 200, 100), outline=(255, 255, 255))
        else:
            angle_corrected = (corrected_count / total_plots) * 360
            draw.pieslice(bbox, start=0, end=angle_corrected, fill=(50, 200, 100), outline=(255, 255, 255))
            draw.pieslice(bbox, start=angle_corrected, end=360, fill=(220, 70, 70), outline=(255, 255, 255))

        # Legend
        draw.rectangle([350, 150, 365, 165], fill=(50, 200, 100))
        draw.text((375, 152), f"Corrected: {corrected_count} ({corrected_count / total_plots * 100:.1f}%)", fill=(200, 200, 200))
        draw.rectangle([350, 185, 365, 200], fill=(220, 70, 70))
        draw.text((375, 187), f"Flagged: {flagged_count} ({flagged_count / total_plots * 100:.1f}%)", fill=(200, 200, 200))
        draw.text((20, 360), f"Total evaluated plots: {total_plots}", fill=(180, 180, 180))

        draw.rectangle([5, 5, img_w - 5, img_h - 5], outline=(100, 100, 100), width=1)
        pie_path = out_dir / f"{village_slug}_decision_pie.png"
        img.save(pie_path)
        paths.append(pie_path)

        # ----------------------------------------------------
        # Chart 2: Histogram of confidence values
        # ----------------------------------------------------
        conf_vals = [r.confidence for r in results.values()]
        paths.append(self._draw_histogram(
            village_slug,
            conf_vals,
            "Confidence Distribution",
            out_dir / f"{village_slug}_confidence_hist.png",
            (0, 180, 255),
            (0.0, 1.0)
        ))

        # ----------------------------------------------------
        # Chart 3: Histogram of raw score improvements
        # ----------------------------------------------------
        improv_vals = [r.score_improvement for r in results.values()]
        max_val = max(max(improv_vals), 0.1) if improv_vals else 0.1
        paths.append(self._draw_histogram(
            village_slug,
            improv_vals,
            "Score Improvement Distribution",
            out_dir / f"{village_slug}_score_improvement_hist.png",
            (200, 100, 255),
            (0.0, max_val)
        ))

        # ----------------------------------------------------
        # Chart 4: Scatter plot of Confidence vs Score Improvement
        # ----------------------------------------------------
        paths.append(self._draw_scatter(
            village_slug,
            x_vals=improv_vals,
            y_vals=conf_vals,
            title="Confidence vs Score Improvement Scatter",
            x_label="Score Improvement",
            y_label="Confidence",
            results=results,
            out_path=out_dir / f"{village_slug}_confidence_vs_improvement.png",
            x_range=(0.0, max_val),
            y_range=(0.0, 1.0)
        ))

        # ----------------------------------------------------
        # Chart 5: Scatter plot of Correctability Score vs Confidence
        # ----------------------------------------------------
        corr_vals = [r.correctability_score for r in results.values()]
        paths.append(self._draw_scatter(
            village_slug,
            x_vals=corr_vals,
            y_vals=conf_vals,
            title="Correctability vs Confidence Scatter",
            x_label="Correctability",
            y_label="Confidence",
            results=results,
            out_path=out_dir / f"{village_slug}_correctability_vs_confidence.png",
            x_range=(0.0, 1.0),
            y_range=(0.0, 1.0)
        ))

        # ----------------------------------------------------
        # Chart 6: Horizontal bar chart of decision failure reasons
        # ----------------------------------------------------
        reasons_counts = {
            "invalid_original_geometry": 0,
            "low_confidence": 0,
            "insufficient_improvement": 0,
            "worse_than_official": 0,
            "low_relative_improvement": 0,
            "low_correctability": 0,
            "area_mismatch": 0,
            "already_correct": 0,
            "ambiguous_alignment": 0,
            "neighbor_disagreement": 0,
            "missing_official_baseline": 0,
            "invalid_final_geometry": 0,
            "weak_image_evidence": 0,
            "pass_instability": 0,
            "flat_basin_weak_edges": 0,
            "extreme_translation": 0,
        }
        for r in results.values():
            for reason in r.decision_reason:
                if reason in reasons_counts:
                    reasons_counts[reason] += 1
                else:
                    reasons_counts[reason] = 1

        paths.append(self._draw_reasons_chart(
            village_slug,
            reasons_counts,
            out_dir / f"{village_slug}_reasons_bar.png"
        ))

        return paths

    def _draw_histogram(
        self,
        village_slug: str,
        vals: List[float],
        title: str,
        out_path: Path,
        color: Tuple[int, int, int],
        val_range: Tuple[float, float],
    ) -> Path:
        img_w, img_h = 600, 400
        img = Image.new("RGB", (img_w, img_h), color=(20, 20, 20))
        draw = ImageDraw.Draw(img)
        draw.text((20, 15), f"Village {village_slug}: {title}", fill=(255, 255, 255))

        bins = np.linspace(val_range[0], val_range[1], 11)
        counts, _ = np.histogram(vals, bins=bins)
        max_count = max(counts) if len(counts) > 0 and max(counts) > 0 else 1

        graph_x1, graph_y1 = 60, 60
        graph_x2, graph_y2 = img_w - 40, img_h - 60
        graph_w = graph_x2 - graph_x1
        graph_h = graph_y2 - graph_y1

        # Draw axes
        draw.line([(graph_x1, graph_y1), (graph_x1, graph_y2)], fill=(120, 120, 120), width=1)
        draw.line([(graph_x1, graph_y2), (graph_x2, graph_y2)], fill=(120, 120, 120), width=1)

        # Draw Y-axis grid
        for i in range(5):
            f_tick = i / 4
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

            if bar_h > 0:
                draw.rectangle([bx1, by1, bx2, by2], fill=color, outline=(100, 100, 100))

            bin_center_x = (bx1 + bx2) // 2
            draw.text((bin_center_x - 10, graph_y2 + 8), f"{bins[i]:.2f}", fill=(150, 150, 150))

        draw.text((graph_x2 - 10, graph_y2 + 8), f"{bins[-1]:.2f}", fill=(150, 150, 150))
        draw.rectangle([5, 5, img_w - 5, img_h - 5], outline=(100, 100, 100), width=1)
        img.save(out_path)
        return out_path

    def _draw_scatter(
        self,
        village_slug: str,
        x_vals: List[float],
        y_vals: List[float],
        title: str,
        x_label: str,
        y_label: str,
        results: Dict[str, DecisionResult],
        out_path: Path,
        x_range: Tuple[float, float],
        y_range: Tuple[float, float],
    ) -> Path:
        img_w, img_h = 600, 450
        img = Image.new("RGB", (img_w, img_h), color=(20, 20, 20))
        draw = ImageDraw.Draw(img)
        draw.text((20, 15), f"Village {village_slug}: {title}", fill=(255, 255, 255))

        graph_x1, graph_y1 = 60, 60
        graph_x2, graph_y2 = img_w - 40, img_h - 60
        graph_w = graph_x2 - graph_x1
        graph_h = graph_y2 - graph_y1

        # Draw axes
        draw.line([(graph_x1, graph_y1), (graph_x1, graph_y2)], fill=(120, 120, 120), width=1)
        draw.line([(graph_x1, graph_y2), (graph_x2, graph_y2)], fill=(120, 120, 120), width=1)

        # Draw axis labels
        draw.text((graph_x1 + graph_w // 2 - 40, graph_y2 + 30), x_label, fill=(180, 180, 180))
        # PIL doesn't easily support rotated text, so we put Y-label at the top left of the axis
        draw.text((graph_x1 - 10, graph_y1 - 25), y_label, fill=(180, 180, 180))

        # Y-ticks
        for i in range(5):
            f = i / 4.0
            y_coord = int(graph_y2 - f * graph_h)
            y_val = y_range[0] + f * (y_range[1] - y_range[0])
            draw.line([(graph_x1 - 5, y_coord), (graph_x1, y_coord)], fill=(120, 120, 120))
            draw.text((graph_x1 - 45, y_coord - 5), f"{y_val:.2f}", fill=(150, 150, 150))

        # X-ticks
        for i in range(5):
            f = i / 4.0
            x_coord = int(graph_x1 + f * graph_w)
            x_val = x_range[0] + f * (x_range[1] - x_range[0])
            draw.line([(x_coord, graph_y2), (x_coord, graph_y2 + 5)], fill=(120, 120, 120))
            draw.text((x_coord - 15, graph_y2 + 10), f"{x_val:.2f}", fill=(150, 150, 150))

        # Plot points
        for (plot_no, res), x_val, y_val in zip(results.items(), x_vals, y_vals):
            # Map values to pixel coordinates
            if x_range[1] > x_range[0]:
                px = int(graph_x1 + ((x_val - x_range[0]) / (x_range[1] - x_range[0])) * graph_w)
            else:
                px = graph_x1
            if y_range[1] > y_range[0]:
                py = int(graph_y2 - ((y_val - y_range[0]) / (y_range[1] - y_range[0])) * graph_h)
            else:
                py = graph_y2

            # Visual clipping to graph area
            px = int(np.clip(px, graph_x1, graph_x2))
            py = int(np.clip(py, graph_y1, graph_y2))

            color = (50, 200, 100) if res.status == "corrected" else (220, 70, 70)
            # Draw point as a small circle
            draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=color)

        draw.rectangle([5, 5, img_w - 5, img_h - 5], outline=(100, 100, 100), width=1)
        img.save(out_path)
        return out_path

    def _draw_reasons_chart(self, village_slug: str, counts: Dict[str, int], out_path: Path) -> Path:
        img_w, img_h = 650, 800
        img = Image.new("RGB", (img_w, img_h), color=(20, 20, 20))
        draw = ImageDraw.Draw(img)
        draw.text((20, 15), f"Village {village_slug}: Vetoes / Failure Reasons", fill=(255, 255, 255))

        labels = list(counts.keys())
        vals = list(counts.values())
        max_val = max(vals) if vals and max(vals) > 0 else 1

        y_start = 60
        bar_height = 25
        spacing = 45
        label_col_w = 230
        bar_max_w = 350

        for idx, (label, val) in enumerate(counts.items()):
            y = y_start + idx * spacing

            # Label text
            draw.text((20, y + 5), label, fill=(200, 200, 200))

            # Background bar
            bx1 = label_col_w
            bx2 = bx1 + bar_max_w
            draw.rectangle([bx1, y, bx2, y + bar_height], fill=(40, 40, 40), outline=(60, 60, 60))

            # Foreground bar
            if val > 0:
                bw = int((val / max_val) * bar_max_w)
                draw.rectangle([bx1, y, bx1 + bw, y + bar_height], fill=(220, 70, 70))

            # Value text
            draw.text((bx1 + int((val / max_val) * bar_max_w) + 10 if val > 0 else bx1 + 10, y + 5), str(val), fill=(255, 255, 255))

        draw.rectangle([5, 5, img_w - 5, img_h - 5], outline=(100, 100, 100), width=1)
        img.save(out_path)
        return out_path
