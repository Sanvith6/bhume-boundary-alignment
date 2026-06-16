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
    
    print(f"{label:<45} | Status: {status:<10} | IoU: {iou:.4f} | Shift: ({dx:.2f}, {dy:.2f})")

def main():
    print("Weight Ablation Study for Plot 1177 (village_Malatavadi):")
    print("-" * 100)
    
    # Base configuration: current Config (All Enabled)
    evaluate_config(
        "village_Malatavadi", "1177", "New Config (Current)",
    )
    
    # 1. Restore baseline smoothness (translation_smoothness=0.20, lambda_smooth=0.04)
    evaluate_config(
        "village_Malatavadi", "1177", "Restore Baseline Smoothness Only",
        lambda_smooth=0.04,
        scoring_weights={
            "distance_transform": 0.40,
            "boundary_hint": 0.05,
            "contour_similarity": 0.20,
            "gradient_agreement": 0.15,
            "area_consistency": 0.05,
            "translation_smoothness": 0.20,  # restored
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.05,
        }
    )
    
    # 2. Restore baseline smoothness lambda only
    evaluate_config(
        "village_Malatavadi", "1177", "Restore Baseline lambda_smooth Only (0.04)",
        lambda_smooth=0.04
    )
    
    # 3. Restore baseline smoothness weight only
    evaluate_config(
        "village_Malatavadi", "1177", "Restore Baseline Smoothness Weight Only (0.20)",
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
    
    # 4. Remove neighbor consistency
    evaluate_config(
        "village_Malatavadi", "1177", "Remove Neighbor Consistency (0.0)",
        scoring_weights={
            "distance_transform": 0.45,  # adjusted
            "boundary_hint": 0.05,
            "contour_similarity": 0.20,
            "gradient_agreement": 0.15,
            "area_consistency": 0.05,
            "translation_smoothness": 0.10,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.0,  # removed
        }
    )
    
    # 5. Restore baseline image weights (distance_transform=0.45, gradient_agreement=0.10)
    evaluate_config(
        "village_Malatavadi", "1177", "Restore Baseline Image Evidence Weights Only",
        scoring_weights={
            "distance_transform": 0.45,  # restored
            "boundary_hint": 0.05,
            "contour_similarity": 0.20,
            "gradient_agreement": 0.10,  # restored
            "area_consistency": 0.05,
            "translation_smoothness": 0.10,
            "shape_preservation": 0.0,
            "neighbor_consistency": 0.05,
        }
    )

if __name__ == "__main__":
    main()
