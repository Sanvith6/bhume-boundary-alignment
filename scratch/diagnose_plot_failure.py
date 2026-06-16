import sys
import pickle
from pathlib import Path
import numpy as np
import geopandas as gpd

# Add project root to path before importing bhume modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.io import load as load_village
from bhume.config import Config
from bhume.coordinator import CoordinateTransformer
from bhume.geo import open_imagery
from bhume.score import _utm_for, _iou
from bhume.preprocessor import Preprocessor
from bhume.multiscale import MultiScaleProcessor
from bhume.edge_detector import EdgeDetector
from bhume.contour_detector import ContourDetector
from bhume.candidate_generator import CandidateGenerator
from bhume.alignment_scorer import AlignmentScorer
from bhume.optimizer import LocalOptimizer
from bhume.regularizer import SpatialRegularizer
from bhume.loader import build_neighbor_graph
from bhume.confidence import ConfidenceEstimator
from bhume.decision import DecisionEngine

def diagnose_plot(village_name, plot_no):
    print(f"\n==================================================")
    print(f"DIAGNOSING PLOT {plot_no} IN {village_name}")
    print(f"==================================================")
    
    village_dir = Path("data") / village_name
    village = load_village(village_dir)
    
    # 1. Official & Truth Geometries
    truth_gdf = village.example_truths
    if truth_gdf is None or plot_no not in truth_gdf.index:
        print(f"Error: Plot {plot_no} not in example truths.")
        return
        
    utm = _utm_for(truth_gdf.geometry.iloc[0])
    truth_u = truth_gdf.to_crs(utm).loc[plot_no, 'geometry']
    official_u = village.plots.to_crs(utm).loc[plot_no, 'geometry']
    
    official_iou = _iou(official_u, truth_u)
    official_dist = official_u.centroid.distance(truth_u.centroid)
    print(f"Official placement: IoU={official_iou:.4f}, Centroid Distance={official_dist:.2f}m")
    
    # True shift vector
    dx_true = truth_u.centroid.x - official_u.centroid.x
    dy_true = truth_u.centroid.y - official_u.centroid.y
    print(f"True Shift Vector: dx={dx_true:.2f}m, dy={dy_true:.2f}m")
    
    # 2. Run Optimizer
    config = Config(workers=1, cache_enabled=False)
    preprocessor = Preprocessor(config)
    ms_processor = MultiScaleProcessor(config)
    edge_detector = EdgeDetector(config)
    contour_detector = ContourDetector(config)
    candidate_generator = CandidateGenerator(config)
    alignment_scorer = AlignmentScorer(config)
    optimizer = LocalOptimizer(config, candidate_generator, alignment_scorer)
    
    with open_imagery(village.imagery_path) as src:
        transformer = CoordinateTransformer(src)
        patch = preprocessor.process_plot(village, plot_no)
        ms_patch = ms_processor.generate_pyramid(patch, village.plot(plot_no))
        edges = edge_detector.detect_pyramid(ms_patch, method="combined")
        edge_level = edges.get_level(1.0)
        boundary = contour_detector.parameterize_boundary(
            plot_no, village.plot(plot_no), transformer, image_shape=patch.gray.shape
        )
        
        row = village.plots.loc[plot_no]
        rec_area = row.get("recorded_area_sqm")
        map_area = row.get("map_area_sqm")
        recorded_area_m2 = float(rec_area) if rec_area is not None and not np.isnan(rec_area) else None
        map_area_m2 = float(map_area) if map_area is not None and not np.isnan(map_area) else None
        
        opt_res = optimizer.optimize(
            plot_number=plot_no,
            boundary=boundary,
            edge_level=edge_level,
            transformer=transformer,
            patch_transform=patch.transform,
            patch_gray=patch.gray,
            center_dx=0.0,
            center_dy=0.0,
            recorded_area_m2=recorded_area_m2,
            map_area_m2=map_area_m2,
        )
        
    print(f"\n--- OPTIMIZATION RESULTS ---")
    print(f"Best Candidate Shift: dx={opt_res.best_candidate.dx:.2f}m, dy={opt_res.best_candidate.dy:.2f}m")
    print(f"Best Score: {opt_res.best_score:.4f}")
    print(f"Local Maxima Count: {opt_res.statistics.number_of_local_maxima}")
    print(f"Score Gap: {opt_res.statistics.score_gap:.4f}")
    print(f"Curvature Proxy (Best - Mean) / Std: {(opt_res.statistics.best_score - opt_res.statistics.score_mean) / (opt_res.statistics.score_std if opt_res.statistics.score_std > 0 else 1.0):.2f}")
    
    # Calculate IoU of best optimizer candidate
    from bhume.decision import translate as shp_translate
    geom_crs = transformer.geom_to_crs(village.plot(plot_no))
    translated_crs = shp_translate(geom_crs, xoff=opt_res.best_candidate.dx, yoff=opt_res.best_candidate.dy)
    cand_4326 = transformer.geom_to_lonlat(translated_crs)
    cand_u = gpd.GeoSeries([cand_4326], crs="EPSG:4326").to_crs(utm).iloc[0]
    opt_iou = _iou(cand_u, truth_u)
    opt_dist = cand_u.centroid.distance(truth_u.centroid)
    print(f"Optimizer placement: IoU={opt_iou:.4f}, Centroid Distance={opt_dist:.2f}m")
    
    # Find feature scores for the best candidate
    best_scored = None
    min_dist = float('inf')
    for sc in opt_res.optimization_history:
        dist = np.hypot(sc.candidate.dx - opt_res.best_candidate.dx, sc.candidate.dy - opt_res.best_candidate.dy)
        if dist < min_dist:
            min_dist = dist
            best_scored = sc
    if best_scored:
        print(f"Feature scores: {best_scored.feature_scores}")
        
    # Official placement candidate score
    off_cand = [sc for sc in opt_res.optimization_history if abs(sc.candidate.dx) < 1e-5 and abs(sc.candidate.dy) < 1e-5]
    if off_cand:
        print(f"Official candidate score: {off_cand[0].total_score:.4f} (Features: {off_cand[0].feature_scores})")
    else:
        print("Official candidate not in history")

    # True shift candidate score if close to search grid
    true_grid_cand = min(opt_res.optimization_history, key=lambda sc: np.hypot(sc.candidate.dx - dx_true, sc.candidate.dy - dy_true))
    print(f"Score near true shift (dx={true_grid_cand.candidate.dx:.2f}, dy={true_grid_cand.candidate.dy:.2f}): {true_grid_cand.total_score:.4f} (Features: {true_grid_cand.feature_scores})")

    # 3. Regularization
    regularizer = SpatialRegularizer(config)
    # Mock neighbor results (just using this plot's opt_res)
    neighbor_graph = build_neighbor_graph(village.plots)
    results_dict = {plot_no: opt_res}
    # Load all neighbor opt results
    # To keep it fast, we can look for cache files, otherwise just make them placeholder
    for neighbor in neighbor_graph.get(plot_no, {}).keys():
        cache_file = Path("debug_output/cache") / village_name / neighbor / "opt_result.pkl"
        if cache_file.exists():
            with open(cache_file, "rb") as f:
                results_dict[neighbor] = pickle.load(f)
        else:
            # Create placeholder
            results_dict[neighbor] = opt_res # Fallback
            
    reg_results = regularizer.regularize(results_dict, village, neighbor_graph)
    reg_res = reg_results[plot_no]
    print(f"\n--- REGULARIZATION RESULTS ---")
    print(f"Regularized Shift: dx={reg_res.regularized_candidate.dx:.2f}m, dy={reg_res.regularized_candidate.dy:.2f}m")
    print(f"Neighbor Shift Consensus: dx={reg_res.neighbor_shift[0]:.2f}m, dy={reg_res.neighbor_shift[1]:.2f}m")
    print(f"Blending Factor alpha: {reg_res.blending_factor:.4f}")
    
    # 4. Confidence Estimation
    confidence_estimator = ConfidenceEstimator(config)
    conf_res = confidence_estimator.estimate(opt_res, reg_res)
    print(f"\n--- CONFIDENCE ESTIMATOR RESULTS ---")
    print(f"Final Calibrated Confidence: {conf_res.confidence:.4f}")
    for k, v in conf_res.support_signals.items():
        print(f"  {k}: {v:.4f}")
        
    # 5. Decision
    decision_engine = DecisionEngine(config)
    dec_res = decision_engine.decide(opt_res, reg_res, conf_res, village.plot(plot_no), transformer)
    print(f"\n--- DECISION RESULTS ---")
    print(f"Decision Status: {dec_res.status}")
    print(f"Decision Reason/Vetoes: {dec_res.decision_reason}")
    print(f"Correctability Score: {dec_res.correctability_score:.4f}")

if __name__ == "__main__":
    if len(sys.argv) > 2:
        diagnose_plot(sys.argv[1], sys.argv[2])
    else:
        # Default diagnoses for worst plots
        diagnose_plot("village_Malatavadi", "1763")
        diagnose_plot("village_Malatavadi", "1966")
        diagnose_plot("village_Vadnerbhairav", "2647")
