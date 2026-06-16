import sys
import time
import json
from pathlib import Path
import numpy as np
import geopandas as gpd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator
from bhume.io import load as load_village
from bhume.score import _utm_for, _iou

def run_pipeline_on_truth(cfg, name, plots_to_run):
    village_dir = Path("data") / name
    village = load_village(village_dir)
    coordinator = PipelineCoordinator(cfg)
    
    res = coordinator.run_village(village_dir, plot_numbers=plots_to_run)
    pred_path = village_dir / "predictions.geojson"
    pred_gdf = gpd.read_file(pred_path).set_index("plot_number", drop=False)
    return pred_gdf, village

def main():
    plots = {
        "village_Malatavadi": ["1177", "1763", "1966"],
        "village_Vadnerbhairav": ["1145", "1403", "1476", "1710", "2647", "622"]
    }
    
    # 1. Setup Baseline Config
    cfg_base = Config(
        workers=1,
        cache_enabled=False,
        debug_visualize=False,
        enable_multi_start=False,
        enable_adaptive_search=False,
        enable_multi_hypothesis=False,
        enable_reoptimization=False,
        enable_basin_stability=False,
        lambda_smooth=0.04,
        scoring_weights={
            "distance_transform": 0.45,
            "boundary_hint": 0.05,
            "contour_similarity": 0.20,
            "gradient_agreement": 0.10,
            "area_consistency": 0.05,
            "translation_smoothness": 0.20,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.0,
        }
    )
    
    # 2. Setup New Config with Restored Smoothness
    cfg_new = Config(
        workers=1,
        cache_enabled=False,
        debug_visualize=False,
        lambda_smooth=0.04,
        scoring_weights={
            "distance_transform": 0.35,  # adjusted to sum to 1.0
            "boundary_hint": 0.05,
            "contour_similarity": 0.20,
            "gradient_agreement": 0.15,
            "area_consistency": 0.05,
            "translation_smoothness": 0.20,  # restored
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.05,
        }
    )
    
    results = {}
    
    for name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        print(f"\n================ {name.upper()} ================")
        village_dir = Path("data") / name
        truth_path = village_dir / "example_truths.geojson"
        truth_gdf = gpd.read_file(truth_path)
        truth_gdf['plot_number'] = truth_gdf['plot_number'].astype(str) if 'plot_number' in truth_gdf.columns else truth_gdf.index.astype(str)
        truth_gdf = truth_gdf.set_index('plot_number', drop=False)
        
        utm = _utm_for(truth_gdf.geometry.iloc[0])
        truth_gdf_u = truth_gdf.to_crs(utm)
        
        plots_to_run = plots[name]
        
        print("Running Baseline pipeline...")
        pred_base, village = run_pipeline_on_truth(cfg_base, name, plots_to_run)
        
        print("Running Improved pipeline...")
        pred_new, _ = run_pipeline_on_truth(cfg_new, name, plots_to_run)
        
        pred_base_u = pred_base.to_crs(utm)
        pred_new_u = pred_new.to_crs(utm)
        official_u = village.plots.to_crs(utm)
        
        for pn in plots_to_run:
            t_geom = truth_gdf_u.loc[pn, 'geometry']
            o_geom = official_u.loc[pn, 'geometry']
            
            pb_geom = pred_base_u.loc[pn, 'geometry']
            pn_geom = pred_new_u.loc[pn, 'geometry']
            
            iou_off = _iou(o_geom, t_geom)
            iou_base = _iou(pb_geom, t_geom)
            iou_new = _iou(pn_geom, t_geom)
            
            status_base = pred_base.loc[pn, 'status']
            status_new = pred_new.loc[pn, 'status']
            
            conf_base = pred_base.loc[pn, 'confidence']
            conf_new = pred_new.loc[pn, 'confidence']
            
            # true shift
            dx_true = t_geom.centroid.x - o_geom.centroid.x
            dy_true = t_geom.centroid.y - o_geom.centroid.y
            
            # base shift
            dx_base = pb_geom.centroid.x - o_geom.centroid.x
            dy_base = pb_geom.centroid.y - o_geom.centroid.y
            
            # new shift
            dx_new = pn_geom.centroid.x - o_geom.centroid.x
            dy_new = pn_geom.centroid.y - o_geom.centroid.y
            
            print(f"\nPlot {pn}:")
            print(f"  Official IoU: {iou_off:.4f} | True Shift: ({dx_true:.2f}, {dy_true:.2f})")
            print(f"  [Baseline] Status: {status_base:<10} | Conf: {conf_base:.4f} | IoU: {iou_base:.4f} | Shift: ({dx_base:.2f}, {dy_base:.2f})")
            print(f"  [New]      Status: {status_new:<10} | Conf: {conf_new:.4f} | IoU: {iou_new:.4f} | Shift: ({dx_new:.2f}, {dy_new:.2f})")
            if abs(iou_new - iou_base) > 0.001:
                print(f"  *** Difference: {iou_new - iou_base:+.4f} ***")

if __name__ == "__main__":
    main()
