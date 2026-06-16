import json
from pathlib import Path
import geopandas as gpd
import numpy as np

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator
from bhume.io import load as load_village
from bhume.score import _utm_for, _iou

def diagnose():
    # Load benchmark plots
    with open("scratch/benchmark_plots.json", "r") as f:
        benchmark_plots = json.load(f)
        
    cfg = Config(
        workers=4,
        cache_enabled=False,
        debug_visualize=False,
    )
    
    # We will replicate run_village but extract intermediate results
    for name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        village_dir = Path("data") / name
        village = load_village(village_dir)
        plots_to_run = benchmark_plots[name]
        
        # Load truth plots
        truth_path = Path("data") / name / "example_truths.geojson"
        if not truth_path.exists():
            continue
        truth_gdf = gpd.read_file(truth_path)
        truth_gdf['plot_number'] = truth_gdf['plot_number'].astype(str) if 'plot_number' in truth_gdf.columns else truth_gdf.index.astype(str)
        truth_gdf = truth_gdf.set_index('plot_number', drop=False)
        
        coordinator = PipelineCoordinator(cfg)
        
        # We want to run custom coordinator steps to inspect intermediate outputs
        # 1. Global Coarse Shift Estimation
        n_samples = min(20, len(plots_to_run))
        dx_coarse_list = []
        dy_coarse_list = []
        
        from bhume.geo import open_imagery
        from bhume.loader import CoordinateTransformer
        
        step = max(1, len(plots_to_run) // n_samples)
        sampled_plots = plots_to_run[::step][:n_samples]
        with open_imagery(village.imagery_path) as src:
            transformer = CoordinateTransformer(src)
            for plot_no in sampled_plots:
                try:
                    patch = coordinator.preprocessor.process_plot(village, plot_no)
                    ms_patch = coordinator.ms_processor.generate_pyramid(patch, village.plot(plot_no))
                    edges = coordinator.edge_detector.detect_pyramid(ms_patch, method="combined")
                    edge_level = edges.get_level(1.0)
                    boundary = coordinator.contour_detector.parameterize_boundary(
                        plot_no, village.plot(plot_no), transformer, image_shape=patch.gray.shape
                    )
                    row = village.plots.loc[plot_no]
                    rec_area = row.get("recorded_area_sqm")
                    map_area = row.get("map_area_sqm")
                    recorded_area_m2 = float(rec_area) if rec_area is not None and not np.isnan(rec_area) else None
                    map_area_m2 = float(map_area) if map_area is not None and not np.isnan(map_area) else None
                    
                    opt_res = coordinator.optimizer.optimize(
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
                    dx_coarse_list.append(opt_res.best_candidate.dx)
                    dy_coarse_list.append(opt_res.best_candidate.dy)
                except Exception:
                    pass
                    
        dx_global = float(np.median(dx_coarse_list)) if dx_coarse_list else 0.0
        dy_global = float(np.median(dy_coarse_list)) if dy_coarse_list else 0.0
        
        # 2. Local Processing
        opt_results = {}
        for plot_no in plots_to_run:
            try:
                patch = coordinator.preprocessor.process_plot(village, plot_no)
                ms_patch = coordinator.ms_processor.generate_pyramid(patch, village.plot(plot_no))
                edges = coordinator.edge_detector.detect_pyramid(ms_patch, method="combined")
                edge_level = edges.get_level(1.0)
                boundary = coordinator.contour_detector.parameterize_boundary(
                    plot_no, village.plot(plot_no), transformer, image_shape=patch.gray.shape
                )
                row = village.plots.loc[plot_no]
                rec_area = row.get("recorded_area_sqm")
                map_area = row.get("map_area_sqm")
                recorded_area_m2 = float(rec_area) if rec_area is not None and not np.isnan(rec_area) else None
                map_area_m2 = float(map_area) if map_area is not None and not np.isnan(map_area) else None
                
                opt_res = coordinator.optimizer.optimize(
                    plot_number=plot_no,
                    boundary=boundary,
                    edge_level=edge_level,
                    transformer=transformer,
                    patch_transform=patch.transform,
                    patch_gray=patch.gray,
                    center_dx=dx_global,
                    center_dy=dy_global,
                    recorded_area_m2=recorded_area_m2,
                    map_area_m2=map_area_m2,
                )
                opt_results[plot_no] = opt_res
            except Exception as e:
                print(f"Error optimizing {plot_no}: {e}")
                
        # 3. Spatial Regularizer
        from bhume.loader import build_neighbor_graph
        neighbor_graph = build_neighbor_graph(village.plots)
        reg_results = coordinator.regularizer.regularize(
            results=opt_results,
            village=village,
            graph=neighbor_graph,
        )
        
        # 4. Confidence
        conf_results = {}
        for plot_no in plots_to_run:
            if plot_no in opt_results:
                conf_results[plot_no] = coordinator.confidence_estimator.estimate(
                    opt_results[plot_no], reg_results[plot_no]
                )
                
        # 5. Decisions
        decision_results = {}
        with open_imagery(village.imagery_path) as src:
            transformer = CoordinateTransformer(src)
            for plot_no in plots_to_run:
                if plot_no in opt_results:
                    original_geom = village.plot(plot_no)
                    decision_results[plot_no] = coordinator.decision_engine.decide(
                        opt_results[plot_no],
                        reg_results[plot_no],
                        conf_results[plot_no],
                        original_geom,
                        transformer,
                    )
                    
        # Diagnose example truths
        print(f"\n================ DIAGNOSIS FOR {name.upper()} ================")
        utm = _utm_for(truth_gdf.geometry.iloc[0])
        truth_gdf_u = truth_gdf.to_crs(utm)
        official_gdf_u = village.plots.to_crs(utm)
        
        for pn in truth_gdf.index:
            if pn not in decision_results:
                print(f"Plot {pn} (Truth): Not processed or failed.")
                continue
                
            t = truth_gdf_u.loc[pn, 'geometry']
            o = official_gdf_u.loc[pn, 'geometry']
            iou_official = _iou(o, t)
            
            dec_res = decision_results[pn]
            opt_res = opt_results[pn]
            reg_res = reg_results[pn]
            conf_res = conf_results[pn]
            
            # project final geometry to UTM
            pg_4326 = dec_res.final_geometry
            pg_u = gpd.GeoSeries([pg_4326], crs="EPSG:4326").to_crs(utm).iloc[0]
            iou_pred = _iou(pg_u, t)
            centroid_err = pg_u.centroid.distance(t.centroid)
            
            # Find closest candidate in optimization history to truth shift
            # We can project the truth geometry relative to official to get truth shift in meters
            truth_centroid = t.centroid
            official_centroid = o.centroid
            # Compute exact shift of truth centroid relative to official centroid
            truth_dx = truth_centroid.x - official_centroid.x
            truth_dy = truth_centroid.y - official_centroid.y
            
            print(f"Plot {pn}:")
            print(f"  Status: {dec_res.status} | Confidence: {dec_res.confidence:.4f}")
            print(f"  IoU: Official = {iou_official:.4f} -> Predicted = {iou_pred:.4f} (Diff: {iou_pred - iou_official:+.4f})")
            print(f"  Centroid error: {centroid_err:.3f} m")
            print(f"  Applied shift: dx={dec_res.applied_shift[0]:.3f}, dy={dec_res.applied_shift[1]:.3f} (Mag: {np.hypot(*dec_res.applied_shift):.3f}m)")
            print(f"  True shift:    dx={truth_dx:.3f}, dy={truth_dy:.3f} (Mag: {np.hypot(truth_dx, truth_dy):.3f}m)")
            print(f"  Local opt shift: dx={reg_res.local_shift[0]:.3f}, dy={reg_res.local_shift[1]:.3f}")
            print(f"  Neighbor consensus shift: dx={reg_res.neighbor_shift[0]:.3f}, dy={reg_res.neighbor_shift[1]:.3f}")
            print(f"  Blending factor alpha: {reg_res.blending_factor:.3f}")
            print(f"  Decision Signals:")
            for k, v in dec_res.decision_signals.items():
                print(f"    {k}: {v}")
            print(f"  Decision Reason: {dec_res.decision_reason}")
            
            # Let's inspect features scores at the best candidate
            best_scored = None
            min_dist = float('inf')
            for sc in opt_res.optimization_history:
                dist = np.hypot(sc.candidate.dx - opt_res.best_candidate.dx, sc.candidate.dy - opt_res.best_candidate.dy)
                if dist < min_dist:
                    min_dist = dist
                    best_scored = sc
            if best_scored:
                print(f"  Best Candidate Feature Scores:")
                for k, v in best_scored.feature_scores.items():
                    print(f"    {k}: {v:.4f}")

if __name__ == "__main__":
    diagnose()
