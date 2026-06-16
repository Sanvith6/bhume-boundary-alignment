"""Configuration module for the cadastral boundary correction pipeline.

This module provides the Config class, which holds all hyperparameter settings
and options for Loader, Preprocessor, EdgeDetector, etc., avoiding hardcoded constants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Config:
    """Configuration settings for the boundary correction pipeline."""

    # 1. Image & Patch Extraction
    patch_pad_m: float = 35.0  # Padding in meters around each plot
    patch_pad_margin_m: float = 5.0  # NEW: safety margin so candidates never score off-patch
    scale_pyramids: List[float] = field(
        default_factory=lambda: [1.0, 0.5, 0.25]
    )  # Pyramids scale factors for multi-scale voting

    # 2. Contour Sampling
    contour_sample_interval_m: float = 1.0  # Step size in meters for sampling boundary points

    # 3. Candidate Search Space
    search_radius_m: float = 12.0  # Coarse search range (+/- meters)
    search_step_m: float = 1.0  # Coarse search step (meters)
    fine_search_radius_m: float = 3.5  # Fine search range around coarse optimum (+/- meters)
    fine_search_step_m: float = 0.25  # Fine search step (meters)
    submeter_search_radius_m: float = 0.25  # Submeter search range (+/- meters)
    submeter_search_step_m: float = 0.05  # Submeter search step (meters)

    # 3a. NEW: Drift-based dynamic search sizing (replaces plot-size-based scaling).
    # The search radius must cover how far plots ACTUALLY drift in this village,
    # which is a property of georeferencing quality, not plot size. The coordinator
    # estimates per-plot shifts on a Stage-1 sample and sets:
    #   search_radius_m = clip(|global_shift| + mad_mult * MAD(sample_shifts), min, max)
    enable_drift_based_radius: bool = False  # NEW
    drift_radius_mad_multiplier: float = 3.0  # NEW: how many MADs of shift spread to cover
    search_radius_min_m: float = 10.0  # NEW: lower clamp for dynamic search radius
    search_radius_max_m: float = 50.0  # NEW: upper clamp for dynamic search radius
    global_shift_sample_size: int = 40  # NEW: number of plots sampled for Stage-1 estimation

    # 3b. Search Preservation & Peak Detection
    top_k_candidates: int = 50  # Maintain top-K highest scoring candidates
    multi_peak_threshold: float = 0.02  # Score tolerance for detecting secondary peaks
    multi_peak_distance_m: float = 1.5  # Spatial distance in meters to separate peaks

    # 3c. Multi-start, Adaptive, Multi-hypothesis, Multi-scale & Stability
    enable_multi_start: bool = True  # Init from official, global, neighbor
    enable_adaptive_search: bool = False  # Completely disabled: causes severe hallucination on small houses.
    entropy_high_threshold: float = 1.5  # Entropy threshold to trigger adaptive expansion
    peak_gap_low_threshold: float = 0.05  # Peak gap threshold to trigger adaptive expansion
    adaptive_radius_multiplier: float = 2.5  # How much to expand radius in adaptive mode
    adaptive_step_multiplier: float = 2.0  # NEW: Factor to expand search if confidence is low
    enable_multi_hypothesis: bool = True  # Cluster and refine top candidates
    num_hypotheses_to_refine: int = 3  # Number of cluster centers to refine
    cluster_radius_m: float = 3.0  # Spatial clustering distance
    enable_multi_scale: bool = True  # Optimize at multiple resolutions and vote
    enable_basin_stability: bool = True  # Evaluate stability around optimum
    basin_offsets_m: List[float] = field(
        default_factory=lambda: [0.5, 1.0, 2.0]
    )  # Offsets for basin stability evaluation
    enable_second_pass: bool = True  # Re-optimize centered on regularized shift
    enable_reoptimization: bool = False  # Exhaustive search if confidence is low
    reopt_confidence_threshold: float = 0.55  # Confidence below which to reoptimize
    min_coarse_score: float = 0.25  # Minimum coarse alignment score required to refine candidate
    alpha_gap: float = 10.0  # Exponent parameter for score gap in adaptive radius
    beta_peak: float = 15.0  # Exponent parameter for peak sharpness in adaptive radius
    repeatability_scale_m: float = 3.0  # Normalization distance in meters for repeatability score

    # 4. Alignment Scoring Feature Weights
    scoring_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "distance_transform": 0.50,
            "boundary_hint": 0.0,       # CHANGED: fused into edge map; separate signal was duplicate of contour_similarity
            "contour_similarity": 0.20,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.20,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.0,
        }
    )
    lambda_smooth: float = 0.04
    sigma_dt: float = 2.0

    # 4a. Semantic Edge Gating Settings
    semantic_corridor_width_px: float = 10.0

    # 5. Spatial Regularizer Settings
    regularization_factor: float = 0.55  # Weight of neighbors vs local shift (0.0 to 1.0)
    regularization_iterations: int = 2  # Number of topological smoothing passes
    neighbor_max_distance_m: float = 50.0  # Maximum centroid distance to consider neighbors if not touching
    regularization_quality_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "best_score": 0.5,
            "score_gap": 0.3,
            "entropy": 0.2,
        }
    )
    regularization_outlier_threshold_m: float = 10.0  # Distance in meters from median to ignore neighbor outlier
    regularization_min_neighbor_quality: float = 0.2  # Minimum neighbor quality to include in smoothing
    regularization_high_quality_threshold: float = 0.45  # Quality threshold where local shift is preserved (alpha = 1.0)
    regularization_low_quality_threshold: float = 0.3  # Quality threshold where local shift is blended most (alpha = min_alpha)
    regularization_min_alpha: float = 0.2  # Minimum blending factor (rely at most 100% on neighbors)

    # 6. Confidence Estimation Settings
    confidence_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "peak_sharpness": 0.3,
            "perturbation_stability": 0.2,
            "area_consistency": 0.15,
            "neighbor_consistency": 0.2,
            "edge_strength": 0.15,
        }
    )
    confidence_signal_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "opt_quality": 0.10,
            "peak_gap": 0.05,
            "ambiguity": 0.15,
            "entropy": 0.05,
            "area_consistency": 0.05,
            "neighbor_agreement": 0.10,
            "consensus_strength": 0.07,
            "edge_strength": 0.05,
            "boundary_coverage": 0.00,
            "gradient_agreement": 0.05,
            "score_improvement": 0.05,
            "basin_stability": 0.10,
            "stability": 0.08,          # NEW: optimizer convergence + init agreement
            "scale_agreement": 0.05,    # NEW: multi-scale translation consistency
        }
    )
    confidence_sigma_gap: float = 0.1
    confidence_sigma_neighbor_dist: float = 8.0
    confidence_sigma_neighbor_variance: float = 16.0
    confidence_sigma_improvement: float = 0.06  # CHANGED: 0.01 was too small (saturated for all corrections)
    confidence_sigma_repeatability_variance: float = 4.0  # NEW: for repeatability gate
    confidence_sigma_drift: float = 4.0  # NEW: for G_drift gate
    confidence_sigma_translation: float = 5.0  # NEW: for G_trans gate
    confidence_neutral_value: float = 0.5  # NEW: fallback when a signal is unavailable

    # 6a. NEW: Spatial ambiguity signal / veto.
    # A competing hypothesis further than ambiguity_distance_m away from the best,
    # scoring within ambiguity_score_ratio of it, means the evidence supports two
    # different fields -> low confidence, and the decision engine should flag.
    ambiguity_distance_m: float = 5.0  # NEW: min separation to count as a distinct rival
    ambiguity_score_ratio: float = 0.15  # NEW: rival within 15% of best score = ambiguous
    threshold_ambiguity_veto: float = 0.30  # NEW: S_ambiguity below this -> flag

    # 6b. NEW: Dynamic confidence stretch (coordinator applies ONCE, village-wide).
    # estimate() returns raw confidence only; the coordinator computes center from
    # the village's raw distribution and applies the sigmoid stretch exactly once.
    stretch_center_default: float = 0.68  # NEW: fallback center if distribution unusable
    stretch_k_default: float = 25.0  # NEW: fallback steepness
    stretch_output_min: float = 0.10  # NEW: lower bound of reported confidence
    stretch_output_max: float = 0.95  # NEW: upper bound of reported confidence

    # 7. Decision Engine Thresholds
    threshold_confidence: float = 0.6  # Minimum confidence to accept a correction
    threshold_alignment_score: float = 0.3  # Minimum score to accept a correction
    threshold_score_improvement: float = 0.05  # Minimum improvement over official placement
    threshold_correctability: float = 0.5
    threshold_confidence_veto: float = 0.33
    threshold_improvement_veto: float = 0.01  # Restraint rule for negligible improvements
    threshold_already_correct: float = 1.0  # Distance threshold under which geometry is deemed already correct
    # NEW: neighbor-disagreement veto: local shift differing from neighborhood
    # consensus by more than mad_mult * MAD(village shifts), with a non-dominant
    # peak, is almost always a fooled matcher -> flag instead of smooth.
    neighbor_disagreement_mad_multiplier: float = 2.0  # NEW
    correctability_signal_weights: Dict[str, float] = field(
        default_factory=lambda: {
            "confidence": 0.3,
            "score_improvement": 0.2,
            "optimization_quality": 0.2,
            "area_consistency": 0.15,
            "neighbor_agreement": 0.15,
        }
    )

    # 8. Debug & Visualization Options
    debug_visualize: bool = False  # Enable step-by-step debug visualization
    debug_out_dir: str = "debug_output"  # Directory to save debug plots

    # 9. Pipeline Coordination Options
    workers: int | str = "auto"  # Number of parallel worker processes ("auto" or int)
    cache_enabled: bool = True  # Enable caching of intermediate processing steps

    def to_dict(self) -> Dict:
        """Serialize configuration to a dictionary."""
        return {
            "patch_pad_m": self.patch_pad_m,
            "patch_pad_margin_m": self.patch_pad_margin_m,
            "scale_pyramids": self.scale_pyramids,
            "contour_sample_interval_m": self.contour_sample_interval_m,
            "search_radius_m": self.search_radius_m,
            "search_step_m": self.search_step_m,
            "fine_search_radius_m": self.fine_search_radius_m,
            "fine_search_step_m": self.fine_search_step_m,
            "submeter_search_radius_m": self.submeter_search_radius_m,
            "submeter_search_step_m": self.submeter_search_step_m,
            "enable_drift_based_radius": self.enable_drift_based_radius,
            "drift_radius_mad_multiplier": self.drift_radius_mad_multiplier,
            "search_radius_min_m": self.search_radius_min_m,
            "search_radius_max_m": self.search_radius_max_m,
            "global_shift_sample_size": self.global_shift_sample_size,
            "top_k_candidates": self.top_k_candidates,
            "multi_peak_threshold": self.multi_peak_threshold,
            "multi_peak_distance_m": self.multi_peak_distance_m,
            "enable_multi_start": self.enable_multi_start,
            "enable_adaptive_search": self.enable_adaptive_search,
            "entropy_high_threshold": self.entropy_high_threshold,
            "peak_gap_low_threshold": self.peak_gap_low_threshold,
            "enable_multi_hypothesis": self.enable_multi_hypothesis,
            "num_hypotheses_to_refine": self.num_hypotheses_to_refine,
            "cluster_radius_m": self.cluster_radius_m,
            "enable_multi_scale": self.enable_multi_scale,
            "enable_basin_stability": self.enable_basin_stability,
            "basin_offsets_m": self.basin_offsets_m,
            "enable_second_pass": self.enable_second_pass,
            "enable_reoptimization": self.enable_reoptimization,
            "reopt_confidence_threshold": self.reopt_confidence_threshold,
            "min_coarse_score": self.min_coarse_score,
            "alpha_gap": self.alpha_gap,
            "beta_peak": self.beta_peak,
            "repeatability_scale_m": self.repeatability_scale_m,

            "scoring_weights": self.scoring_weights,
            "semantic_corridor_width_px": self.semantic_corridor_width_px,
            "regularization_factor": self.regularization_factor,
            "regularization_iterations": self.regularization_iterations,
            "neighbor_max_distance_m": self.neighbor_max_distance_m,
            "regularization_quality_weights": self.regularization_quality_weights,
            "regularization_outlier_threshold_m": self.regularization_outlier_threshold_m,
            "regularization_min_neighbor_quality": self.regularization_min_neighbor_quality,
            "regularization_high_quality_threshold": self.regularization_high_quality_threshold,
            "regularization_low_quality_threshold": self.regularization_low_quality_threshold,
            "regularization_min_alpha": self.regularization_min_alpha,
            "confidence_weights": self.confidence_weights,
            "confidence_signal_weights": self.confidence_signal_weights,
             "confidence_sigma_gap": self.confidence_sigma_gap,
            "confidence_sigma_neighbor_dist": self.confidence_sigma_neighbor_dist,
            "confidence_sigma_improvement": self.confidence_sigma_improvement,
            "confidence_sigma_neighbor_variance": self.confidence_sigma_neighbor_variance,
            "confidence_sigma_repeatability_variance": self.confidence_sigma_repeatability_variance,
            "confidence_sigma_drift": self.confidence_sigma_drift,
            "confidence_sigma_translation": self.confidence_sigma_translation,
            "confidence_neutral_value": self.confidence_neutral_value,
            "ambiguity_distance_m": self.ambiguity_distance_m,
            "ambiguity_score_ratio": self.ambiguity_score_ratio,
            "threshold_ambiguity_veto": self.threshold_ambiguity_veto,
            "stretch_center_default": self.stretch_center_default,
            "stretch_k_default": self.stretch_k_default,
            "stretch_output_min": self.stretch_output_min,
            "stretch_output_max": self.stretch_output_max,
            "threshold_confidence": self.threshold_confidence,
            "threshold_alignment_score": self.threshold_alignment_score,
            "threshold_score_improvement": self.threshold_score_improvement,
            "threshold_correctability": self.threshold_correctability,
            "threshold_confidence_veto": self.threshold_confidence_veto,
            "threshold_improvement_veto": self.threshold_improvement_veto,
            "threshold_already_correct": self.threshold_already_correct,
            "neighbor_disagreement_mad_multiplier": self.neighbor_disagreement_mad_multiplier,
            "correctability_signal_weights": self.correctability_signal_weights,
            "debug_visualize": self.debug_visualize,
            "debug_out_dir": self.debug_out_dir,
            "workers": self.workers,
            "cache_enabled": self.cache_enabled,
        }
