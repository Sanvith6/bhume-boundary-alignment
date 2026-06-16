import json
from pathlib import Path
import numpy as np
import geopandas as gpd
from shapely.geometry import shape
from bhume.io import load as load_village
from bhume.score import _utm_for, _iou

def main():
    print("| Village | Plot Number | Status | Confidence | IoU | Centroid Error (m) | Translation Error (m) |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    
    for name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        village_dir = Path("data") / name
        pred_path = village_dir / "predictions.geojson"
        
        if not pred_path.exists():
            continue
            
        village = load_village(village_dir)
        pred = gpd.read_file(pred_path).set_index("plot_number", drop=False)
        truth = village.example_truths
        official = village.plots
        
        if truth is None:
            continue
            
        utm = _utm_for(truth.geometry.iloc[0])
        truth_u = truth.to_crs(utm)
        official_u = official.to_crs(utm)
        pred_u = pred.to_crs(utm)
        
        for pn in truth.index:
            t_geom = truth_u.loc[pn, 'geometry']
            o_geom = official_u.loc[pn, 'geometry']
            
            # True shift vector
            t_centroid = t_geom.centroid
            o_centroid = o_geom.centroid
            dx_truth = t_centroid.x - o_centroid.x
            dy_truth = t_centroid.y - o_centroid.y
            
            p_row = pred.loc[pn]
            p_status = p_row.get("status")
            p_conf = p_row.get("confidence", 0.0)
            
            pg = pred_u.loc[pn, 'geometry']
            
            iou = _iou(pg, t_geom)
            centroid_err = pg.centroid.distance(t_centroid)
            
            # Predicted shift vector (based on centroid movement)
            dx_pred = pg.centroid.x - o_centroid.x
            dy_pred = pg.centroid.y - o_centroid.y
            
            translation_err = np.hypot(dx_pred - dx_truth, dy_pred - dy_truth)
            
            print(f"| {name.replace('village_', '')} | {pn} | {p_status} | {p_conf:.4f} | {iou:.4f} | {centroid_err:.2f} | {translation_err:.2f} |")

if __name__ == "__main__":
    main()
