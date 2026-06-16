import sys
import time
from pathlib import Path
import geopandas as gpd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.config import Config
from bhume.coordinator import PipelineCoordinator
from bhume.io import load as load_village
from bhume.score import _utm_for, _iou

def evaluate_config(name, plot_no, label, **overrides):
    cfg = Config(
        workers=1,
        cache_enabled=False,
        debug_visualize=False,
        **overrides
    )
    village_dir = Path("data") / name
    village = load_village(village_dir)
    coordinator = PipelineCoordinator(cfg)
    
    res = coordinator.run_village(village_dir, plot_numbers=[plot_no])
    
    pred_path = village_dir / "predictions.geojson"
    pred_gdf = gpd.read_file(pred_path).set_index("plot_number", drop=False)
    
    utm = _utm_for(village.plots.loc[plot_no, 'geometry'])
    t_path = village_dir / "example_truths.geojson"
    t_gdf = gpd.read_file(t_path)
    t_gdf['plot_number'] = t_gdf['plot_number'].astype(str) if 'plot_number' in t_gdf.columns else t_gdf.index.astype(str)
    t_geom = t_gdf.set_index('plot_number').to_crs(utm).loc[plot_no, 'geometry']
    
    p_geom = pred_gdf.to_crs(utm).loc[plot_no, 'geometry']
    o_geom = village.plots.to_crs(utm).loc[plot_no, 'geometry']
    
    iou = _iou(p_geom, t_geom)
    dx = p_geom.centroid.x - o_geom.centroid.x
    dy = p_geom.centroid.y - o_geom.centroid.y
    status = pred_gdf.loc[plot_no, 'status']
    conf = pred_gdf.loc[plot_no, 'confidence']
    
    print(f"{label:<30} | Status: {status:<10} | Conf: {conf:.4f} | IoU: {iou:.4f} | Shift: ({dx:.2f}, {dy:.2f})")

def main():
    print("Ablation Study for Plot 1177 (village_Malatavadi):")
    print("-" * 100)
    
    # 1. Baseline
    evaluate_config(
        "village_Malatavadi", "1177", "Baseline Config",
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
    
    # 2. New Config (all enabled)
    evaluate_config(
        "village_Malatavadi", "1177", "New Config (All Enabled)"
    )
    
    # 3. New Config but baseline weights
    evaluate_config(
        "village_Malatavadi", "1177", "New Config + Baseline Weights",
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
    
    # 4. New Config but without Multi-start
    evaluate_config(
        "village_Malatavadi", "1177", "New Config without Multi-start",
        enable_multi_start=False
    )
    
    # 5. New Config but without Adaptive search
    evaluate_config(
        "village_Malatavadi", "1177", "New Config without Adaptive",
        enable_adaptive_search=False
    )
    
    # 6. New Config but without Multi-hypothesis
    evaluate_config(
        "village_Malatavadi", "1177", "New Config without Multi-hyp",
        enable_multi_hypothesis=False
    )
    
    # 7. New Config but without Reoptimization
    evaluate_config(
        "village_Malatavadi", "1177", "New Config without Reopt",
        enable_reoptimization=False
    )

if __name__ == "__main__":
    main()
