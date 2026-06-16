"""Pipeline coordinator module for the cadastral boundary correction pipeline.

Coordinates the end-to-end processing of a village bundle from loading to final predictions.
"""

from __future__ import annotations

import csv
import datetime
import json
import logging
import multiprocessing
import shutil
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple, Union

import numpy as np
import geopandas as gpd

from bhume.config import Config
from bhume.loader import CoordinateTransformer, build_neighbor_graph, load_village
from bhume.preprocessor import PreprocessedPatch, Preprocessor
from bhume.multiscale import MultiScaleProcessor
from bhume.edge_detector import EdgeDetector
from bhume.contour_detector import ContourDetector
from bhume.candidate_generator import CandidateGenerator
from bhume.alignment_scorer import AlignmentScorer
from bhume.optimizer import LocalOptimizer, OptimizationResult, OptimizationStatistics
from bhume.regularizer import SpatialRegularizer, RegularizedOptimizationResult
from bhume.confidence import ConfidenceEstimator, ConfidenceResult, stretch_confidence
from bhume.decision import DecisionEngine, DecisionResult
from bhume.writer import PredictionWriter
from bhume.geo import open_imagery
from bhume.candidate_generator import CandidateTransformation

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Dataclass holding execution results and statistics for the pipeline run."""

    village_slug: str
    runtime_seconds: float
    total_plots: int
    processed_plots: int
    corrected: int
    flagged: int
    failed: int
    average_confidence: float
    average_shift: float
    average_improvement: float
    predictions_path: Path
    pipeline_success: bool
    stage_times: Dict[str, float] = field(default_factory=dict)


_VILLAGE_CACHE: Dict[str, object] = {}


def get_cached_village(village_dir: Path | str) -> object:
    """Cache loaded villages in workers to avoid redundant disk I/O."""
    path_key = str(Path(village_dir).resolve())
    if path_key not in _VILLAGE_CACHE:
        _VILLAGE_CACHE[path_key] = load_village(village_dir)
    return _VILLAGE_CACHE[path_key]


def _process_plot_worker(args) -> Tuple[str, Union[OptimizationResult, None], float, float, float, Union[Dict, None]]:
    """Worker function for processing a single plot in a parallel process."""
    # Unpack arguments with backward compatibility fallback
    expected_drift = None
    force_no_cache = False
    pass1_best_shift = None
    if len(args) == 8:
        config, village_dir, plot_no, center_dx, center_dy, expected_drift, force_no_cache, pass1_best_shift = args
    elif len(args) == 7:
        config, village_dir, plot_no, center_dx, center_dy, expected_drift, force_no_cache = args
    elif len(args) == 6:
        config, village_dir, plot_no, center_dx, center_dy, expected_drift = args
    else:
        config, village_dir, plot_no, center_dx, center_dy = args

    # Disk caching check: NEVER read cache when cache_enabled is False OR when force_no_cache is True
    cache_enabled = getattr(config, "cache_enabled", False)
    village_dir = Path(village_dir)
    village_slug = village_dir.name
    cache_dir = Path(config.debug_out_dir) / "cache" / village_slug / plot_no
    cache_file = cache_dir / "opt_result.pkl"

    if cache_enabled and not force_no_cache and cache_file.exists():
        import pickle
        try:
            with open(cache_file, "rb") as f:
                opt_res = pickle.load(f)
            if opt_res is not None:
                opt_res.optimization_history = []
            return plot_no, opt_res, 0.0, 0.0, 0.0, None
        except Exception:
            pass

    t_preprocess = 0.0
    t_edges = 0.0
    t_optimize = 0.0

    try:
        mock_fail = getattr(config, "_mock_fail_plot", None)
        if mock_fail is not None and str(plot_no) == str(mock_fail):
            raise RuntimeError("Simulated Preprocessing Error")
        # Load/fetch cached village reference inside the process
        village = get_cached_village(village_dir)
        original_geom = village.plot(plot_no)

        # Area-Ratio Pre-Filter
        row = village.plots.loc[plot_no]
        rec_area = row.get("recorded_area_sqm")
        map_area = row.get("map_area_sqm")
        pot_kharaba = row.get("pot_kharaba_ha")

        recorded_area_m2 = float(rec_area) if rec_area is not None and not np.isnan(rec_area) else None
        map_area_m2 = float(map_area) if map_area is not None and not np.isnan(map_area) else None
        pot_kharaba_ha = float(pot_kharaba) if pot_kharaba is not None and not np.isnan(pot_kharaba) else 0.0

        if recorded_area_m2 is not None and map_area_m2 is not None and recorded_area_m2 > 0:
            total_recorded_sqm = recorded_area_m2 + (pot_kharaba_ha * 10000.0)
            if total_recorded_sqm > 0:
                area_ratio = map_area_m2 / total_recorded_sqm
                if area_ratio < 0.60 or area_ratio > 1.60:
                    opt_res = make_placeholder_optimization_result(plot_no)
                    opt_res.best_candidate.metadata["area_ratio_pre_filter_flagged"] = True
                    opt_res.best_candidate.metadata["area_ratio"] = area_ratio
                    return plot_no, opt_res, 0.0, 0.0, 0.0, None

        # Instantiate modules inside the process
        preprocessor = Preprocessor(config)
        ms_processor = MultiScaleProcessor(config)
        edge_detector = EdgeDetector(config)
        contour_detector = ContourDetector(config)
        candidate_generator = CandidateGenerator(config)
        alignment_scorer = AlignmentScorer(config)
        optimizer = LocalOptimizer(config, candidate_generator, alignment_scorer)

        with open_imagery(village.imagery_path) as src:
            transformer = CoordinateTransformer(src)

            # 1. Preprocess
            print(f"DEBUG: Plot {plot_no} - Starting preprocessing...")
            t0 = time.perf_counter()
            patch = preprocessor.process_plot(village, plot_no)
            t_preprocess = time.perf_counter() - t0

            # 2. Pyramid & Edges
            print(f"DEBUG: Plot {plot_no} - Starting pyramid generation...")
            t1 = time.perf_counter()
            ms_patch = ms_processor.generate_pyramid(patch, original_geom)
            print(f"DEBUG: Plot {plot_no} - Starting edge detection...")
            edges = edge_detector.detect_pyramid(ms_patch, method="combined", plot_area_m2=original_geom.area)
            edge_level = edges.get_level(1.0)
            t_edges = time.perf_counter() - t1

            # 3. Contours & Local Optimizer
            print(f"DEBUG: Plot {plot_no} - Starting contour detector...")
            t2 = time.perf_counter()
            boundary = contour_detector.parameterize_boundary(
                plot_no, original_geom, transformer, image_shape=patch.gray.shape
            )
            print(f"DEBUG: Plot {plot_no} - Contours done.")

            row = village.plots.loc[plot_no]
            rec_area = row.get("recorded_area_sqm")
            map_area = row.get("map_area_sqm")
            pot_kharaba_val = row.get("pot_kharaba_ha")
            recorded_area_m2 = float(rec_area) if rec_area is not None and not np.isnan(rec_area) else None
            map_area_m2 = float(map_area) if map_area is not None and not np.isnan(map_area) else None
            pot_kharaba_ha_val = float(pot_kharaba_val) if pot_kharaba_val is not None and not np.isnan(pot_kharaba_val) else 0.0

            # A5: Use total recorded area (cultivable + pot_kharaba) per CONTRACT.md
            total_recorded_area_m2 = recorded_area_m2
            if recorded_area_m2 is not None:
                total_recorded_area_m2 = recorded_area_m2 + (pot_kharaba_ha_val * 10000.0)
            # DYNAMIC PLOT-SIZE SCALING
            # DYNAMIC PLOT-SIZE SCALING
            # Combine village-level drift (which handles systematic shifts even for small plots)
            # with plot-level size scaling (which gives massive outliers enough room to be found).
            base_radius = getattr(config, '_base_village_radius', getattr(config, 'search_radius_m', 15.0))
            config._base_village_radius = base_radius
            dynamic_radius = max(10.0, min(50.0, boundary.total_length / 6.0))
            config.search_radius_m = float(max(base_radius, dynamic_radius))

            print(f"DEBUG: Processing {plot_no}, perimeter={boundary.total_length:.2f}, search_radius_m={getattr(config, 'search_radius_m', 15.0)}")
            opt_res = optimizer.optimize(
                plot_number=plot_no,
                boundary=boundary,
                edge_level=edge_level,
                transformer=transformer,
                patch_transform=patch.transform,
                patch_gray=patch.gray,
                center_dx=center_dx,
                center_dy=center_dy,
                recorded_area_m2=total_recorded_area_m2,
                map_area_m2=map_area_m2,
                perimeter_m=boundary.total_length,
                expected_drift=expected_drift,
            )
            opt_res.best_candidate.metadata["plot_number"] = plot_no
            opt_res.best_candidate.metadata["perimeter_m"] = boundary.total_length
            opt_res.best_candidate.metadata["map_area_m2"] = map_area_m2

            # Run multi-scale optimizations to compute agreement
            coarse_best_shift = None
            medium_best_shift = None
            fine_best_shift = (opt_res.best_candidate.dx, opt_res.best_candidate.dy)

            if 0.5 in ms_patch.levels:
                try:
                    lvl_05 = ms_patch.get_level(0.5)
                    edge_level_05 = edges.get_level(0.5)
                    opt_res_05 = optimizer.optimize(
                        plot_number=plot_no,
                        boundary=boundary,
                        edge_level=edge_level_05,
                        transformer=transformer,
                        patch_transform=lvl_05.transform,
                        patch_gray=lvl_05.gray,
                        center_dx=center_dx,
                        center_dy=center_dy,
                        recorded_area_m2=recorded_area_m2,
                        map_area_m2=map_area_m2,
                        perimeter_m=boundary.total_length,
                        expected_drift=expected_drift,
                    )
                    medium_best_shift = (opt_res_05.best_candidate.dx, opt_res_05.best_candidate.dy)
                except Exception as e:
                    logger.warning(f"Failed to optimize at scale 0.5: {e}")

            if 0.25 in ms_patch.levels:
                try:
                    lvl_025 = ms_patch.get_level(0.25)
                    edge_level_025 = edges.get_level(0.25)
                    opt_res_025 = optimizer.optimize(
                        plot_number=plot_no,
                        boundary=boundary,
                        edge_level=edge_level_025,
                        transformer=transformer,
                        patch_transform=lvl_025.transform,
                        patch_gray=lvl_025.gray,
                        center_dx=center_dx,
                        center_dy=center_dy,
                        recorded_area_m2=recorded_area_m2,
                        map_area_m2=map_area_m2,
                        perimeter_m=boundary.total_length,
                        expected_drift=expected_drift,
                    )
                    coarse_best_shift = (opt_res_025.best_candidate.dx, opt_res_025.best_candidate.dy)
                except Exception as e:
                    logger.warning(f"Failed to optimize at scale 0.25: {e}")

            opt_res.statistics.coarse_best_shift = coarse_best_shift
            opt_res.statistics.medium_best_shift = medium_best_shift
            opt_res.statistics.fine_best_shift = fine_best_shift

            opt_res.best_candidate.metadata["coarse_best_shift"] = coarse_best_shift
            opt_res.best_candidate.metadata["medium_best_shift"] = medium_best_shift
            opt_res.best_candidate.metadata["fine_best_shift"] = fine_best_shift

            t_optimize = time.perf_counter() - t2

            # Like-for-like comparison & fallback safety check
            if opt_res is not None and pass1_best_shift is not None:
                from bhume.candidate_generator import CandidateTransformation
                p1_dx, p1_dy = pass1_best_shift
                pass1_cand = CandidateTransformation(
                    dx=p1_dx, dy=p1_dy, search_level="pass1_fallback",
                    metadata={"plot_number": plot_no}
                )
                scored_pass1_under_pass2 = optimizer.scorer.score_candidate(
                    pass1_cand,
                    boundary=boundary,
                    edge_res=edge_level,
                    transformer=transformer,
                    patch_transform=patch.transform,
                    recorded_area_m2=total_recorded_area_m2,
                    map_area_m2=map_area_m2,
                    neighbor_shift=None,
                    expected_drift=expected_drift,
                )
                if opt_res.best_score < 0.99 * scored_pass1_under_pass2.total_score:
                    logger.info(
                        f"Safety Check Fallback [Plot {plot_no}]: Pass-2 score ({opt_res.best_score:.4f}) "
                        f"is lower than Pass-1 shift re-scored ({scored_pass1_under_pass2.total_score:.4f}) by >1%. "
                        f"Falling back to Pass-1 shift ({p1_dx:.3f}, {p1_dy:.3f})."
                    )
                    opt_res.best_candidate = scored_pass1_under_pass2.candidate
                    opt_res.best_score = scored_pass1_under_pass2.total_score
                    opt_res.statistics.best_score = scored_pass1_under_pass2.total_score

        # Ensure max_allowed_radius is ALWAYS present in metadata for regularizer clipping
        if opt_res is not None and opt_res.best_candidate is not None:
            area_to_use = total_recorded_area_m2 if total_recorded_area_m2 is not None else map_area_m2
            perimeter = boundary.total_length if boundary.total_length > 0 else 1.0
            span = 2.0 * area_to_use / perimeter
            opt_res.best_candidate.metadata["max_allowed_radius"] = float(max(75.0, span))
            opt_res.best_candidate.metadata["plot_number"] = plot_no

        # Clear optimization history BEFORE pickling/caching to prevent massive cache files (KB instead of 20MB)
        if opt_res is not None:
            opt_res.optimization_history = []

        # Write to cache if enabled
        if cache_enabled and not force_no_cache:
            cache_dir.mkdir(parents=True, exist_ok=True)
            import pickle
            try:
                with open(cache_file, "wb") as f:
                    pickle.dump(opt_res, f)
            except Exception:
                pass

        return plot_no, opt_res, t_preprocess, t_edges, t_optimize, None

    except Exception as e:
        err_info = {
            "plot_number": plot_no,
            "module": "Stage2Worker",
            "exception": str(e),
            "traceback": traceback.format_exc()
        }
        return plot_no, None, t_preprocess, t_edges, t_optimize, err_info


def make_placeholder_optimization_result(plot_no: str) -> OptimizationResult:
    """Create a default placeholder OptimizationResult for a failed plot."""
    best_cand = CandidateTransformation(dx=0.0, dy=0.0, search_level="coarse", metadata={"plot_number": plot_no})
    stats = OptimizationStatistics(
        best_score=0.0,
        second_best_score=0.0,
        score_gap=0.0,
        score_mean=0.0,
        score_std=0.0,
        score_entropy=0.0,
        number_of_local_maxima=0,
        candidate_count=0,
        optimum_on_boundary=False,
        convergence_path=[]
    )
    return OptimizationResult(
        best_candidate=best_cand,
        best_score=0.0,
        evaluated_candidate_count=0,
        optimization_history=[],
        refinement_levels={},
        top_k_candidates=[],
        statistics=stats,
        peaks=[],
        official_score=None  # unknown baseline, never pretend it was 0.0
    )


def make_placeholder_regularized_result(best_cand: CandidateTransformation) -> RegularizedOptimizationResult:
    """Create a default placeholder RegularizedOptimizationResult."""
    return RegularizedOptimizationResult(
        original_candidate=best_cand,
        regularized_candidate=best_cand,
        applied_shift=(0.0, 0.0),
        local_shift=(0.0, 0.0),
        neighbor_shift=(0.0, 0.0),
        blending_factor=1.0,
        neighbor_statistics={"neighbor_count": 0, "neighbor_contributions": []},
        debug_metadata={}
    )


def make_placeholder_confidence_result() -> ConfidenceResult:
    """Create a default placeholder ConfidenceResult."""
    return ConfidenceResult(
        confidence=0.0,
        optimization_quality=0.0,
        neighbor_agreement=0.0,
        area_consistency=0.0,
        edge_strength=0.0,
        boundary_coverage=0.0,
        peak_gap=0.0,
        entropy=0.0,
        score_improvement=0.0,
        support_signals={},
        debug_metadata={}
    )


class PipelineCoordinator:
    """End-to-end coordinator tying together all boundary correction modules."""

    def __init__(self, config: Config) -> None:
        """Initialize PipelineCoordinator and all internal sub-modules.

        Args:
            config: Pipeline configuration settings.
        """
        import copy
        self._original_config = copy.deepcopy(config)
        self.config = config
        self.preprocessor = Preprocessor(config)
        self.ms_processor = MultiScaleProcessor(config)
        self.edge_detector = EdgeDetector(config)
        self.contour_detector = ContourDetector(config)
        self.candidate_generator = CandidateGenerator(config)
        self.alignment_scorer = AlignmentScorer(config)
        self.optimizer = LocalOptimizer(config, self.candidate_generator, self.alignment_scorer)
        self.regularizer = SpatialRegularizer(config)
        self.confidence_estimator = ConfidenceEstimator(config)
        self.decision_engine = DecisionEngine(config)
        self.writer = PredictionWriter(config)

    def run_village(
        self,
        village_dir: str | Path,
        plot_numbers: List[str] | None = None,
    ) -> PipelineResult:
        """Run the end-to-end boundary correction pipeline for a single village.

        Args:
            village_dir: Path to the village bundle directory.
            plot_numbers: Optional subset of plot numbers to process. If None,
                          processes all plots in the village.

        Returns:
            A populated PipelineResult dataclass containing stats.
        """
        t_global_start = time.perf_counter()
        stage_times = {}

        # ----------------------------------------------------
        # Stage 1: Load and Validate Village (Fatal errors abort)
        # ----------------------------------------------------
        t_stage_1_start = time.perf_counter()
        print("Village loaded")

        village_dir = Path(village_dir)
        if not village_dir.exists():
            raise FileNotFoundError(f"Village directory {village_dir} not found.")

        # Fatal error: GeoJSON unreadable/missing
        input_geojson_path = village_dir / "input.geojson"
        if not input_geojson_path.exists():
            raise FileNotFoundError(f"Fatal error: input.geojson missing from {village_dir}.")

        # Fatal error: imagery missing
        if not (village_dir / "imagery.tif").exists():
            raise FileNotFoundError(f"Fatal error: imagery missing from {village_dir}.")

        try:
            village = load_village(village_dir)
            village_slug = village.slug
        except Exception as e:
            raise ValueError(f"Fatal error: GeoJSON unreadable in {village_dir}: {e}")

        # Fatal error: unique plot IDs check
        if not village.plots.index.is_unique:
            raise ValueError("Fatal error: duplicate plot numbers found in cadastre dataset.")

        # Fatal error: CRS missing
        if village.plots.crs is None:
            raise ValueError("Fatal error: CRS missing from input plots dataset.")

        # Fatal error: raster dimensions invalid
        try:
            with open_imagery(village.imagery_path) as src:
                if src.width <= 0 or src.height <= 0:
                    raise ValueError("Fatal error: raster dimensions invalid.")
        except Exception as e:
            raise ValueError(f"Fatal error: failed to read raster: {e}")

        # Fatal error: geometries valid check (if no geometries, it's corrupted)
        if len(village.plots) == 0:
            raise ValueError("Fatal error: input plots GeoDataFrame is empty.")
        if village.plots.geometry.isna().all():
            raise ValueError("Fatal error: all plot geometries are missing or null.")

        # Resolve plot numbers to process
        is_partial_run = False
        if plot_numbers is None:
            plot_numbers = sorted(village.plots.index.astype(str).tolist())
        else:
            plot_numbers = [str(pn) for pn in plot_numbers]
            is_partial_run = (len(plot_numbers) < len(village.plots))

        total_plots = len(plot_numbers)
        print(f"{total_plots} plots")

        stage_times["load"] = time.perf_counter() - t_stage_1_start

        # Dynamic Config copy and scaling
        import copy
        self.config = copy.deepcopy(self._original_config)
        self.preprocessor.config = self.config
        self.ms_processor.config = self.config
        self.edge_detector.config = self.config
        self.contour_detector.config = self.config
        self.candidate_generator.config = self.config
        self.alignment_scorer.config = self.config
        self.optimizer.config = self.config
        self.optimizer.scorer.config = self.config
        self.optimizer.generator.config = self.config
        self.regularizer.config = self.config
        self.confidence_estimator.config = self.config
        self.decision_engine.config = self.config
        self.writer.config = self.config

        # Compute file MD5 hash of input.geojson
        import hashlib
        def get_file_hash(filepath: Path) -> str:
            hasher = hashlib.md5()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    hasher.update(chunk)
            return hasher.hexdigest()

        input_hash = get_file_hash(input_geojson_path)
        config_cache_path = Path(getattr(self.config, "debug_out_dir", "debug_output")) / "pipeline" / f"{village_slug}_scaled_config.json"

        # Defaults in case neither cache nor sampling produces values
        village_shift_mad = 2.5
        confidence_center = float(getattr(self.config, "stretch_center_default", 0.68))
        confidence_k = float(getattr(self.config, "stretch_k_default", 25.0))

        loaded_from_cache = False
        if is_partial_run and config_cache_path.exists():
            try:
                with open(config_cache_path, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)
                if cached_data.get("input_file_hash") == input_hash:
                    self.config.search_radius_m = cached_data["search_radius_m"]
                    self.config.patch_pad_m = cached_data["patch_pad_m"]
                    self.config.threshold_already_correct = cached_data["threshold_already_correct"]
                    dx_global = cached_data["dx_global"]
                    dy_global = cached_data["dy_global"]
                    confidence_center = cached_data["confidence_center"]
                    confidence_k = cached_data["confidence_k"]
                    village_shift_mad = float(cached_data.get("village_shift_mad", 2.5))
                    loaded_from_cache = True
                    logger.info(f"Loaded scaled config from cache: {config_cache_path}")
            except Exception as e:
                logger.warning(f"Failed to load cached config: {e}. Recomputing.")

        if not loaded_from_cache:
            # 1. Median plot dimension (used for plot-size-related parameters ONLY:
            # already-correct threshold and base patch padding. NOT for search
            # radius: drift is a property of georeferencing quality, not plot size).
            areas = village.plots["recorded_area_sqm"].dropna()
            if areas.empty:
                areas = village.plots["map_area_sqm"].dropna()
            if areas.empty:
                # Fallback: compute area in UTM
                lon = village.plots.geometry.iloc[0].centroid.x
                utm_zone = f"EPSG:{32600 + int((lon + 180) // 6) + 1}"
                areas = village.plots.to_crs(utm_zone).geometry.area

            median_area = float(np.median(areas))
            median_dim = float(np.sqrt(median_area))

            self.config.threshold_already_correct = float(np.clip(0.05 * median_dim, 1.0, 3.0))
            base_pad = float(np.clip(0.4 * median_dim, 15.0, 40.0))

            # ----------------------------------------------------
            # Stage 0: Global Initialization (drift estimation)
            # ----------------------------------------------------
            # Bootstrap with the WIDEST allowed search radius so the sample can
            # observe the village's true drift, however large it is. The final
            # radius is then derived from the measured drift distribution.
            radius_min = float(getattr(self.config, "search_radius_min_m", 10.0))
            radius_max = float(getattr(self.config, "search_radius_max_m", 30.0))
            pad_margin = float(getattr(self.config, "patch_pad_margin_m", 5.0))
            mad_mult = float(getattr(self.config, "drift_radius_mad_multiplier", 3.0))
            sample_size = int(getattr(self.config, "global_shift_sample_size", 50))

            self.config.search_radius_m = radius_max
            self.config.patch_pad_m = float(max(base_pad, radius_max + pad_margin))

            t_stage_0_start = time.perf_counter()
            all_village_plot_numbers = sorted(village.plots.index.astype(str).tolist())
            n_samples = min(sample_size, len(all_village_plot_numbers))
            dx_coarse_list = []
            dy_coarse_list = []

            if n_samples > 0:
                step = max(1, len(all_village_plot_numbers) // n_samples)
                sampled_plots = all_village_plot_numbers[::step][:n_samples]

                with open_imagery(village.imagery_path) as src:
                    transformer = CoordinateTransformer(src)
                    for plot_no in sampled_plots:
                        try:
                            # Wide bootstrap search for Stage 0 drift sampling
                            original_geom_stg0 = village.plot(plot_no)
                            patch = self.preprocessor.process_plot(village, plot_no)
                            ms_patch = self.ms_processor.generate_pyramid(patch, original_geom_stg0)
                            edges = self.edge_detector.detect_pyramid(ms_patch, method="combined", plot_area_m2=original_geom_stg0.area)
                            edge_level = edges.get_level(1.0)
                            boundary = self.contour_detector.parameterize_boundary(
                                plot_no, village.plot(plot_no), transformer, image_shape=patch.gray.shape
                            )

                            row = village.plots.loc[plot_no]
                            rec_area = row.get("recorded_area_sqm")
                            map_area = row.get("map_area_sqm")
                            pot_kharaba = row.get("pot_kharaba_ha")
                            recorded_area_m2 = float(rec_area) if rec_area is not None and not np.isnan(rec_area) else None
                            map_area_m2 = float(map_area) if map_area is not None and not np.isnan(map_area) else None
                            pot_kharaba_ha = float(pot_kharaba) if pot_kharaba is not None and not np.isnan(pot_kharaba) else 0.0

                            # A5: Compute total recorded area (cultivable + pot_kharaba)
                            total_recorded_sqm = None
                            if recorded_area_m2 is not None:
                                total_recorded_sqm = recorded_area_m2 + (pot_kharaba_ha * 10000.0)

                            if recorded_area_m2 is not None and map_area_m2 is not None and recorded_area_m2 > 0:
                                if total_recorded_sqm is not None and total_recorded_sqm > 0:
                                    area_ratio = map_area_m2 / total_recorded_sqm
                                    if area_ratio < 0.60 or area_ratio > 1.60:
                                        continue

                            opt_res = self.optimizer.optimize(
                                plot_number=plot_no,
                                boundary=boundary,
                                edge_level=edge_level,
                                transformer=transformer,
                                patch_transform=patch.transform,
                                patch_gray=patch.gray,
                                center_dx=0.0,
                                center_dy=0.0,
                                recorded_area_m2=total_recorded_sqm,
                                map_area_m2=map_area_m2,
                                perimeter_m=boundary.total_length,
                                coarse_only=True,
                            )
                            opt_res.best_candidate.metadata["plot_number"] = plot_no
                            dx_coarse_list.append(opt_res.best_candidate.dx)
                            dy_coarse_list.append(opt_res.best_candidate.dy)
                        except Exception:
                            pass

            if dx_coarse_list and dy_coarse_list:
                dx_global = float(np.median(dx_coarse_list))
                dy_global = float(np.median(dy_coarse_list))
                # Spread of per-plot shifts around the global median: this is the
                # village's measured residual drift (MAD of Euclidean residuals).
                residuals = np.hypot(
                    np.array(dx_coarse_list) - dx_global,
                    np.array(dy_coarse_list) - dy_global,
                )
                village_shift_mad = float(np.median(residuals))
            else:
                dx_global = 0.0
                dy_global = 0.0
                village_shift_mad = 2.5

            global_shift_mag = float(np.hypot(dx_global, dy_global))

            # Drift-based search radius: cover the measured global shift plus
            # k MADs of residual spread, clamped to sane bounds.
            if getattr(self.config, "enable_drift_based_radius", True):
                self.config.search_radius_m = float(np.clip(
                    global_shift_mag + mad_mult * village_shift_mad,
                    radius_min, radius_max,
                ))
            else:
                self.config.search_radius_m = self._original_config.search_radius_m
            logger.info(
                f"Stage 0 Global Median Shift: dx={dx_global:.3f}m, dy={dy_global:.3f}m, "
                f"shift_mad={village_shift_mad:.3f}m -> search_radius={self.config.search_radius_m:.2f}m"
            )
            stage_times["global_init"] = time.perf_counter() - t_stage_0_start

            # Guard patch_pad_m against search extent + global shift
            self.config.patch_pad_m = float(max(
                base_pad,
                self.config.search_radius_m + global_shift_mag + pad_margin,
            ))
            logger.info(f"Padded search extent: patch_pad_m={self.config.patch_pad_m:.2f}m")

        # ----------------------------------------------------
        # Stage 2: Two-Pass Local Processing Loop
        # ----------------------------------------------------
        t_stage_2_start = time.perf_counter()

        workers = getattr(self.config, "workers", 1)
        if isinstance(workers, str) and workers == "auto":
            # Allow the system to use all available CPU cores for the massive dataset run
            n_workers = multiprocessing.cpu_count()
        else:
            n_workers = int(workers)

        t_preprocess_acc = 0.0
        t_edges_acc = 0.0
        t_optimize_acc = 0.0

        all_village_plot_numbers = sorted(village.plots.index.astype(str).tolist())
        requested_plot_numbers = set(plot_numbers)

        # Determine plots to optimize in Pass 1
        if is_partial_run:
            pass1_plots = set(requested_plot_numbers)
            neighbor_graph = build_neighbor_graph(village.plots)
            for p in requested_plot_numbers:
                if p in neighbor_graph:
                    pass1_plots.update(neighbor_graph[p].keys())
            pass1_plots = sorted(list(pass1_plots))
            print(f"Pass 1: Partial run. Optimizing {len(pass1_plots)} of {len(all_village_plot_numbers)} plots in village")
        else:
            pass1_plots = all_village_plot_numbers
            print(f"Pass 1: Optimizing all {len(all_village_plot_numbers)} plots in village")

        pass1_opt_results: Dict[str, OptimizationResult] = {}
        pass1_failures: List[Dict] = []
        pass1_failed_plots = set()

        # In Pass 1, we only force no cache for requested plot_numbers if cache is disabled
        pass1_cache_enabled = getattr(self.config, "cache_enabled", False)
        pass1_tasks = []
        for plot_no in pass1_plots:
            force_no_cache = (plot_no in requested_plot_numbers) and (not pass1_cache_enabled)
            pass1_tasks.append((self.config, village_dir, plot_no, dx_global, dy_global, None, force_no_cache, None))

        if n_workers > 1:
            ctx = multiprocessing.get_context("spawn")
            processed_count = 0
            with ctx.Pool(processes=n_workers, maxtasksperchild=10) as pool:
                for plot_no, opt_res, t_p, t_e, t_o, err_info in pool.imap_unordered(_process_plot_worker, pass1_tasks):
                    processed_count += 1
                    t_preprocess_acc += t_p
                    t_edges_acc += t_e
                    t_optimize_acc += t_o

                    if err_info is not None:
                        logger.error(f"Pass 1: Failed processing plot {plot_no}: {err_info['exception']}")
                        pass1_failures.append(err_info)
                        pass1_failed_plots.add(plot_no)
                        pass1_opt_results[plot_no] = make_placeholder_optimization_result(plot_no)
                    else:
                        pass1_opt_results[plot_no] = opt_res
        else:
            processed_count = 0
            for task in pass1_tasks:
                processed_count += 1
                plot_no, opt_res, t_p, t_e, t_o, err_info = _process_plot_worker(task)
                t_preprocess_acc += t_p
                t_edges_acc += t_e
                t_optimize_acc += t_o

                if err_info is not None:
                    logger.error(f"Pass 1: Failed processing plot {plot_no}: {err_info['exception']}")
                    pass1_failures.append(err_info)
                    pass1_failed_plots.add(plot_no)
                    pass1_opt_results[plot_no] = make_placeholder_optimization_result(plot_no)
                else:
                    pass1_opt_results[plot_no] = opt_res

        # Fill missing results for non-optimized plots
        for plot_no in all_village_plot_numbers:
            if plot_no not in pass1_opt_results:
                pass1_opt_results[plot_no] = make_placeholder_optimization_result(plot_no)

        # Consensus expected drift calculation
        pass1_shifts = {}
        for plot_no, opt_res in pass1_opt_results.items():
            if plot_no not in pass1_failed_plots and opt_res is not None:
                pass1_shifts[plot_no] = (opt_res.best_candidate.dx, opt_res.best_candidate.dy)

        all_dxs = [s[0] for s in pass1_shifts.values()]
        all_dys = [s[1] for s in pass1_shifts.values()]
        if all_dxs and all_dys:
            village_consensus_dx = float(np.median(all_dxs))
            village_consensus_dy = float(np.median(all_dys))
        else:
            village_consensus_dx = 0.0
            village_consensus_dy = 0.0

        skip_pass2 = len(all_village_plot_numbers) < 4

        # Compute per-plot local consensus or fall back to village-wide median
        plot_consensus = {}
        if not skip_pass2:
            centroids = {}
            for plot_no in all_village_plot_numbers:
                geom = village.plot(plot_no)
                centroids[plot_no] = (geom.centroid.x, geom.centroid.y)

            for plot_no in all_village_plot_numbers:
                p_centroid = centroids[plot_no]
                dists = []
                for other_no, other_centroid in centroids.items():
                    if other_no == plot_no:
                        continue
                    if other_no in pass1_shifts:
                        d = np.hypot(p_centroid[0] - other_centroid[0], p_centroid[1] - other_centroid[1])
                        dists.append((d, pass1_shifts[other_no]))
                
                dists.sort(key=lambda x: x[0])
                nearest_shifts = [x[1] for x in dists[:7]]

                if len(nearest_shifts) >= 3:
                    dx_local = float(np.median([s[0] for s in nearest_shifts]))
                    dy_local = float(np.median([s[1] for s in nearest_shifts]))
                    plot_consensus[plot_no] = (dx_local, dy_local)
                else:
                    plot_consensus[plot_no] = (village_consensus_dx, village_consensus_dy)
        else:
            for plot_no in all_village_plot_numbers:
                plot_consensus[plot_no] = (village_consensus_dx, village_consensus_dy)

        # PASS 2: Re-run optimization with consensus for requested plots (or all if not subset)
        opt_results: Dict[str, OptimizationResult] = {}
        pipeline_failures: List[Dict] = []
        failed_plot_numbers = set()

        if skip_pass2:
            logger.info("Village has < 4 plots. Skipping Pass 2 consensus.")
            opt_results = pass1_opt_results
            pipeline_failures = pass1_failures
            failed_plot_numbers = pass1_failed_plots
        else:
            print(f"Pass 2: Optimizing {len(plot_numbers)} plots using local/village consensus")
            pass2_tasks = []
            for plot_no in plot_numbers:
                consensus = plot_consensus.get(plot_no, (village_consensus_dx, village_consensus_dy))
                p1_res = pass1_opt_results.get(plot_no)
                p1_shift = (p1_res.best_candidate.dx, p1_res.best_candidate.dy) if (p1_res and plot_no not in pass1_failed_plots) else None
                # force_no_cache is always True in Pass 2 since consensus is dynamic
                pass2_tasks.append((self.config, village_dir, plot_no, consensus[0], consensus[1], consensus, True, p1_shift))

            if n_workers > 1:
                ctx = multiprocessing.get_context("spawn")
                processed_count = 0
                with ctx.Pool(processes=n_workers, maxtasksperchild=10) as pool:
                    for plot_no, opt_res, t_p, t_e, t_o, err_info in pool.imap_unordered(_process_plot_worker, pass2_tasks):
                        processed_count += 1
                        t_preprocess_acc += t_p
                        t_edges_acc += t_e
                        t_optimize_acc += t_o

                        if err_info is not None:
                            logger.error(f"Pass 2: Failed processing plot {plot_no}: {err_info['exception']}")
                            pipeline_failures.append(err_info)
                            failed_plot_numbers.add(plot_no)
                            opt_results[plot_no] = make_placeholder_optimization_result(plot_no)
                        else:
                            opt_results[plot_no] = opt_res
            else:
                processed_count = 0
                for task in pass2_tasks:
                    processed_count += 1
                    plot_no, opt_res, t_p, t_e, t_o, err_info = _process_plot_worker(task)
                    t_preprocess_acc += t_p
                    t_edges_acc += t_e
                    t_optimize_acc += t_o

                    if err_info is not None:
                        logger.error(f"Pass 2: Failed processing plot {plot_no}: {err_info['exception']}")
                        pipeline_failures.append(err_info)
                        failed_plot_numbers.add(plot_no)
                        opt_results[plot_no] = make_placeholder_optimization_result(plot_no)
                    else:
                        opt_results[plot_no] = opt_res

            # Plot 1763 debug logging requirement & Control-Plot Monitor
            for plot_no in plot_numbers:
                opt_res1 = pass1_opt_results.get(plot_no)
                opt_res2 = opt_results.get(plot_no)
                consensus = plot_consensus.get(plot_no, (village_consensus_dx, village_consensus_dy))

                shift1_str = f"({opt_res1.best_candidate.dx:.3f}, {opt_res1.best_candidate.dy:.3f})" if opt_res1 else "None"
                shift2_str = f"({opt_res2.best_candidate.dx:.3f}, {opt_res2.best_candidate.dy:.3f})" if opt_res2 else "None"
                score2 = opt_res2.best_score if opt_res2 else 0.0
                official_score = opt_res2.official_score if opt_res2 else (opt_res1.official_score if opt_res1 else None)
                official_score_str = f"{official_score:.4f}" if official_score is not None else "None"

                logger.info(
                    f"Consensus Debug [Plot {plot_no}]: Pass-1 shift={shift1_str}, consensus used=({consensus[0]:.3f}, {consensus[1]:.3f}), "
                    f"Pass-2 shift={shift2_str}, Pass-2 score={score2:.4f}, official_score={official_score_str}"
                )

                # Control-plot monitor: propagate instability flag to candidate
                # metadata so the decision engine's D2 veto can fire.
                if opt_res1 and opt_res2:
                    shift1_mag = np.hypot(opt_res1.best_candidate.dx, opt_res1.best_candidate.dy)
                    shift2_mag = np.hypot(opt_res2.best_candidate.dx, opt_res2.best_candidate.dy)
                    if shift1_mag < 1.5 and shift2_mag > 3.0:
                        logger.warning(
                            f"Control-plot risk alert [Plot {plot_no}]: Pass-1 shift was near-zero ({shift1_mag:.2f} m), "
                            f"but Pass-2 shift is large ({shift2_mag:.2f} m) under consensus drift."
                        )
                        opt_res2.best_candidate.metadata["pass_instability"] = True

            # All-plots coverage: populating opt_results with Pass 1 results for plots not in requested plot_numbers
            for plot_no in all_village_plot_numbers:
                if plot_no not in opt_results:
                    opt_results[plot_no] = pass1_opt_results[plot_no]
                    if plot_no in pass1_failed_plots:
                        failed_plot_numbers.add(plot_no)

        t_stage_2 = time.perf_counter() - t_stage_2_start
        stage_times["preprocess"] = t_preprocess_acc
        stage_times["edges"] = t_edges_acc
        stage_times["optimize"] = t_optimize_acc

        # ----------------------------------------------------
        # Stage 3: Spatial Regularizer (whole village)
        # ----------------------------------------------------
        t_stage_3_start = time.perf_counter()
        print("Running regularization")

        neighbor_graph = build_neighbor_graph(village.plots)
        reg_results = self.regularizer.regularize(
            results=opt_results,
            village=village,
            graph=neighbor_graph,
        )

        # Override placeholders for failed plots
        for plot_no in failed_plot_numbers:
            best_cand = opt_results[plot_no].best_candidate
            reg_results[plot_no] = make_placeholder_regularized_result(best_cand)

        stage_times["regularize"] = time.perf_counter() - t_stage_3_start

        # ----------------------------------------------------
        # Stage 4: Re-scoring & Confidence Estimation
        # ----------------------------------------------------
        t_stage_4_start = time.perf_counter()
        print("Running confidence estimation")

        conf_results: Dict[str, ConfidenceResult] = {}
        final_scores: Dict[str, float] = {}
        final_feature_scores_dict: Dict[str, Dict[str, float]] = {}

        plots_to_process = plot_numbers if is_partial_run else all_village_plot_numbers
        plots_to_process_set = set(plots_to_process)

        for plot_no in all_village_plot_numbers:
            if plot_no not in plots_to_process_set:
                conf_results[plot_no] = make_placeholder_confidence_result()
                final_scores[plot_no] = 0.0
                final_feature_scores_dict[plot_no] = {}

        with open_imagery(village.imagery_path) as src:
            transformer = CoordinateTransformer(src)
            for plot_no in plots_to_process:
                if plot_no in failed_plot_numbers:
                    conf_results[plot_no] = make_placeholder_confidence_result()
                    final_scores[plot_no] = 0.0
                    final_feature_scores_dict[plot_no] = {}
                else:
                    opt_res = opt_results[plot_no]
                    reg_res = reg_results[plot_no]

                    # Check if regularized shift is different from optimized shift
                    opt_dx, opt_dy = opt_res.best_candidate.dx, opt_res.best_candidate.dy
                    reg_dx, reg_dy = reg_res.applied_shift

                    final_score = opt_res.best_score
                    final_feature_scores = None

                    if np.hypot(opt_dx - reg_dx, opt_dy - reg_dy) > 1e-4:
                        # Re-score on the fly
                        try:
                            patch = self.preprocessor.process_plot(village, plot_no)
                            ms_patch = self.ms_processor.generate_pyramid(patch, village.plot(plot_no))
                            edges = self.edge_detector.detect_pyramid(ms_patch, method="combined")
                            edge_level = edges.get_level(1.0)
                            boundary = self.contour_detector.parameterize_boundary(
                                plot_no, village.plot(plot_no), transformer, image_shape=patch.gray.shape
                            )

                            row = village.plots.loc[plot_no]
                            rec_area = row.get("recorded_area_sqm")
                            map_area = row.get("map_area_sqm")
                            pot_kharaba_val = row.get("pot_kharaba_ha")
                            recorded_area_m2 = float(rec_area) if rec_area is not None and not np.isnan(rec_area) else None
                            map_area_m2 = float(map_area) if map_area is not None and not np.isnan(map_area) else None
                            pot_kharaba_ha_val = float(pot_kharaba_val) if pot_kharaba_val is not None and not np.isnan(pot_kharaba_val) else 0.0

                            # A5: Use total recorded area (cultivable + pot_kharaba) per CONTRACT.md
                            total_recorded_area_m2 = recorded_area_m2
                            if recorded_area_m2 is not None:
                                total_recorded_area_m2 = recorded_area_m2 + (pot_kharaba_ha_val * 10000.0)

                            reg_cand = CandidateTransformation(dx=reg_dx, dy=reg_dy, search_level="regularized")
                            scored_cand = self.alignment_scorer.score_candidate(
                                reg_cand,
                                boundary=boundary,
                                edge_res=edge_level,
                                transformer=transformer,
                                patch_transform=patch.transform,
                                recorded_area_m2=total_recorded_area_m2,
                                map_area_m2=map_area_m2,
                                neighbor_shift=reg_res.neighbor_shift
                            )
                            final_score = scored_cand.total_score
                            final_feature_scores = scored_cand.feature_scores
                        except Exception as e:
                            logger.error(f"Error during re-scoring regularized candidate for plot {plot_no}: {e}")

                    final_scores[plot_no] = final_score
                    final_feature_scores_dict[plot_no] = final_feature_scores

                    try:
                        conf_results[plot_no] = self.confidence_estimator.estimate(
                            opt_res, reg_res, final_score=final_score, final_feature_scores=final_feature_scores
                        )
                    except Exception as e:
                        logger.error(f"Confidence estimation failed for plot {plot_no}: {e}")
                        pipeline_failures.append({
                            "plot_number": plot_no,
                            "module": "ConfidenceEstimator",
                            "exception": str(e),
                            "traceback": traceback.format_exc()
                        })
                        failed_plot_numbers.add(plot_no)
                        conf_results[plot_no] = make_placeholder_confidence_result()

        stage_times["confidence"] = time.perf_counter() - t_stage_4_start

        # ----------------------------------------------------
        # Dynamic Confidence Calibration (computed from raw distribution,
        # applied ONCE to surviving corrected plots in Stage 5)
        # ----------------------------------------------------
        if not loaded_from_cache:
            # Collect non-zero raw confidences across all processed plots
            valid_raw_confs = [
                res.raw_confidence for res in conf_results.values()
                if res.raw_confidence > 1e-9
            ]
            if len(valid_raw_confs) >= 10:
                confidence_center = float(np.median(valid_raw_confs))
                # Scale steepness so the IQR maps to a wide reported range
                q25, q75 = np.percentile(valid_raw_confs, [25, 75])
                iqr = max(float(q75 - q25), 1e-3)
                confidence_k = float(np.clip(2.2 / iqr, 10.0, 35.0))
            else:
                all_raw_confs = [res.raw_confidence for res in conf_results.values()]
                if len(all_raw_confs) > 1:
                    c_min = float(np.min(all_raw_confs))
                    c_max = float(np.max(all_raw_confs))
                    if c_max - c_min > 1e-5:
                        confidence_center = (c_max + c_min) / 2.0
                        confidence_k = float(np.clip(4.4 / (c_max - c_min), 10.0, 50.0))
                    else:
                        confidence_center = float(getattr(self.config, "stretch_center_default", 0.68))
                        confidence_k = float(getattr(self.config, "stretch_k_default", 25.0))
                else:
                    confidence_center = float(getattr(self.config, "stretch_center_default", 0.68))
                    confidence_k = float(getattr(self.config, "stretch_k_default", 25.0))

        logger.info(f"Calibrating confidence using center={confidence_center:.4f}, k={confidence_k:.2f}")

        stretch_min = float(getattr(self.config, "stretch_output_min", 0.10))
        stretch_max = float(getattr(self.config, "stretch_output_max", 0.95))

        # ----------------------------------------------------
        # Stage 5: Decision Engine
        # ----------------------------------------------------
        t_stage_5_start = time.perf_counter()
        print("Running decisions")

        decision_results: Dict[str, DecisionResult] = {}
        for plot_no in all_village_plot_numbers:
            if plot_no not in plots_to_process_set:
                original_geom = village.plot(plot_no)
                decision_results[plot_no] = DecisionResult(
                    plot_number=plot_no,
                    status="flagged",
                    confidence=0.0,
                    final_geometry=original_geom,
                    applied_shift=(0.0, 0.0),
                    decision_reason=["skipped_partial_run"],
                    decision_signals={},
                    score_improvement=0.0,
                    correctability_score=0.0,
                    decision_trace={},
                    debug_metadata={}
                )

        with open_imagery(village.imagery_path) as src:
            transformer = CoordinateTransformer(src)
            for plot_no in plots_to_process:
                original_geom = village.plot(plot_no)
                if plot_no in failed_plot_numbers:
                    decision_results[plot_no] = DecisionResult(
                        plot_number=plot_no,
                        status="flagged",
                        confidence=0.0,
                        final_geometry=original_geom,
                        applied_shift=(0.0, 0.0),
                        decision_reason=["pipeline_error"],
                        decision_signals={},
                        score_improvement=0.0,
                        correctability_score=0.0,
                        decision_trace={},
                        debug_metadata={}
                    )
                else:
                    try:
                        dec_res = self.decision_engine.decide(
                            opt_results[plot_no],
                            reg_results[plot_no],
                            conf_results[plot_no],
                            original_geom,
                            transformer,
                            final_score=final_scores[plot_no],
                            village_shift_mad=village_shift_mad,
                        )
                        # Apply calibrated sigmoid stretch ONCE to confidences of
                        # surviving corrected plots; flagged plots keep reason-based confidence.
                        if dec_res.status == "corrected":
                            raw_c = conf_results[plot_no].raw_confidence
                            calibrated_c = stretch_confidence(
                                raw_c, confidence_center, confidence_k,
                                stretch_min, stretch_max,
                            )
                            dec_res.confidence = calibrated_c
                            conf_results[plot_no].confidence = calibrated_c
                        else:
                            conf_results[plot_no].confidence = dec_res.confidence

                        decision_results[plot_no] = dec_res
                    except Exception as e:
                        logger.error(f"Decision engine failed for plot {plot_no}: {e}")
                        pipeline_failures.append({
                            "plot_number": plot_no,
                            "module": "DecisionEngine",
                            "exception": str(e),
                            "traceback": traceback.format_exc()
                        })
                        failed_plot_numbers.add(plot_no)
                        decision_results[plot_no] = DecisionResult(
                            plot_number=plot_no,
                            status="flagged",
                            confidence=0.0,
                            final_geometry=original_geom,
                            applied_shift=(0.0, 0.0),
                            decision_reason=["pipeline_error"],
                            decision_signals={},
                            score_improvement=0.0,
                            correctability_score=0.0,
                            decision_trace={},
                            debug_metadata={}
                        )

        # Write diagnostics jsonl report (Task 8 of promptop.md)
        try:
            debug_pipeline_dir = Path(getattr(self.config, "debug_out_dir", "debug_output")) / "pipeline"
            debug_pipeline_dir.mkdir(parents=True, exist_ok=True)
            diag_file_path = debug_pipeline_dir / f"{village_slug}_diagnostics.jsonl"
            with open(diag_file_path, "w", encoding="utf-8") as f:
                for plot_no in plots_to_process:
                    dec_res = decision_results[plot_no]
                    trace = dec_res.decision_trace or {}
                    conf_res = conf_results.get(plot_no)
                    raw_conf = conf_res.raw_confidence if conf_res else 0.0
                    
                    record = {
                        "plot_number": dec_res.plot_number,
                        "village": village_slug,
                        "optimizer_score": trace.get("optimized_score", 0.0),
                        "official_score": trace.get("official_score"),
                        "score_improvement": dec_res.score_improvement,
                        "raw_confidence": raw_conf,
                        "confidence": dec_res.confidence,
                        "decision_score": trace.get("decision_score", 0.0),
                        "decision": dec_res.status,
                        "score_gap": trace.get("score_gap", 0.0),
                        "ambiguity_index": trace.get("ambiguity_index", 0.0),
                        "peak_sharpness": trace.get("peak_sharpness", 0.0),
                        "repeatability_score": trace.get("repeatability_score", 0.0),
                        "translation": trace.get("shift_magnitude", 0.0),
                        "neighbor_score": trace.get("neighbor_score", 0.5),
                        "edge_score": trace.get("edge_score", 0.5),
                        "area_score": trace.get("area_score", 0.5),
                        "reason_for_flag": dec_res.decision_reason
                    }
                    f.write(json.dumps(record) + "\n")
            logger.info(f"Wrote village diagnostics to {diag_file_path}")
        except Exception as e:
            logger.error(f"Failed to write diagnostics JSONL file: {e}")

        stage_times["decision"] = time.perf_counter() - t_stage_5_start

        # ----------------------------------------------------
        # Stage 6: Prediction Writer
        # ----------------------------------------------------
        t_stage_6_start = time.perf_counter()
        print("Writing predictions")

        if is_partial_run:
            decision_results_to_write = {
                pn: decision_results[pn] for pn in plot_numbers if pn in decision_results
            }
        else:
            decision_results_to_write = decision_results

        predictions_path = self.writer.write(village.dir, decision_results_to_write, self.decision_engine)

        stage_times["writer"] = time.perf_counter() - t_stage_6_start

        # Overall Run Stats & Metadata
        total_time = time.perf_counter() - t_global_start
        corrected_count = sum(1 for r in decision_results_to_write.values() if r.status == "corrected")
        flagged_count = len(decision_results_to_write) - corrected_count
        failed_count = sum(1 for pn in plot_numbers if pn in failed_plot_numbers)

        confidences = [float(r.confidence) for r in decision_results_to_write.values()]
        shifts = []
        for r in decision_results_to_write.values():
            dx, dy = getattr(r, "applied_shift", (0.0, 0.0))
            shifts.append(float(np.hypot(dx, dy)))

        avg_conf = float(np.mean(confidences)) if confidences else 0.0
        avg_shift = float(np.mean(shifts)) if shifts else 0.0
        avg_improvement = float(np.mean([r.score_improvement for r in decision_results_to_write.values()])) if decision_results_to_write else 0.0

        # Save Pipeline Debug Reports
        debug_pipeline_dir = Path(getattr(self.config, "debug_out_dir", "debug_output")) / "pipeline"
        debug_pipeline_dir.mkdir(parents=True, exist_ok=True)

        # 1. pipeline_summary.json
        summary_data = {
            "runtime": total_time,
            "plots": total_plots,
            "corrected": corrected_count,
            "flagged": flagged_count,
            "mean confidence": avg_conf,
            "mean improvement": avg_improvement,
        }
        with open(debug_pipeline_dir / "pipeline_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=4)

        # 2. runtime_breakdown.csv
        # Sum wall-clock durations and calculate percentages
        csv_path = debug_pipeline_dir / "runtime_breakdown.csv"
        stages_info = [
            ("load", stage_times.get("load", 0.0)),
            ("preprocess", stage_times.get("preprocess", 0.0)),
            ("edges", stage_times.get("edges", 0.0)),
            ("optimize", stage_times.get("optimize", 0.0)),
            ("regularize", stage_times.get("regularize", 0.0)),
            ("confidence", stage_times.get("confidence", 0.0)),
            ("decision", stage_times.get("decision", 0.0)),
            ("writer", stage_times.get("writer", 0.0)),
        ]
        sum_stage_times = sum(time for _, time in stages_info)
        with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["module", "execution_time", "percentage"])
            for module, exec_t in stages_info:
                percentage = (exec_t / sum_stage_times * 100.0) if sum_stage_times > 0 else 0.0
                writer.writerow([module, f"{exec_t:.6f}", f"{percentage:.2f}"])

        # 3. pipeline_errors.json
        with open(debug_pipeline_dir / "pipeline_errors.json", "w", encoding="utf-8") as f:
            json.dump(pipeline_failures, f, indent=4)

        # Write config cache
        if not loaded_from_cache:
            try:
                cached_data = {
                    "input_file_hash": input_hash,
                    "search_radius_m": self.config.search_radius_m,
                    "patch_pad_m": self.config.patch_pad_m,
                    "threshold_already_correct": self.config.threshold_already_correct,
                    "dx_global": dx_global,
                    "dy_global": dy_global,
                    "village_shift_mad": village_shift_mad,
                    "confidence_center": confidence_center,
                    "confidence_k": confidence_k
                }
                config_cache_path.parent.mkdir(parents=True, exist_ok=True)
                with open(config_cache_path, "w", encoding="utf-8") as f:
                    json.dump(cached_data, f, indent=4)
                logger.info(f"Saved scaled config to cache: {config_cache_path}")
            except Exception as e:
                logger.warning(f"Failed to write config cache: {e}")

        print("Pipeline complete")

        return PipelineResult(
            village_slug=village_slug,
            runtime_seconds=total_time,
            total_plots=total_plots,
            processed_plots=total_plots - failed_count,
            corrected=corrected_count,
            flagged=flagged_count,
            failed=failed_count,
            average_confidence=avg_conf,
            average_shift=avg_shift,
            average_improvement=avg_improvement,
            predictions_path=predictions_path,
            pipeline_success=True,
            stage_times=stage_times,
        )
