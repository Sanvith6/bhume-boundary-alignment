import sys
import json
import numpy as np
from pathlib import Path
import geopandas as gpd
import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bhume.io import load as load_village

def diagnose():
    print("--- STEP 0 DIAGNOSIS ---")
    
    # 1. Confirm plot count in input.geojson
    for vname in ["village_Malatavadi", "village_Vadnerbhairav"]:
        geojson_path = Path("data") / vname / "input.geojson"
        if geojson_path.exists():
            gdf = gpd.read_file(geojson_path)
            print(f"{vname} input.geojson plot count: {len(gdf)}")
            
    # 2. Read baseline.json to report example-truth results
    baseline_path = Path("scratch/baseline.json")
    if baseline_path.exists():
        with open(baseline_path) as f:
            baseline = json.load(f)
            
        print("\nExample-truth plots from baseline:")
        for r in baseline["results"]:
            print(f"Village: {r['village']}, Plot: {r['plot']}, Status: {r['status']}, "
                  f"Conf: {r['confidence']:.4f}, IoU: {r['iou']:.4f}, "
                  f"dx_pred: {r['dx_pred']:.4f}, dy_pred: {r['dy_pred']:.4f}")
            
    # 3. Check predictions for Plot 1763
    # We can load predictions.geojson for Malatavadi
    pred_path_m = Path("data/village_Malatavadi/predictions.geojson")
    if pred_path_m.exists():
        pred_m = gpd.read_file(pred_path_m).set_index("plot_number")
        if "1763" in pred_m.index:
            row = pred_m.loc["1763"]
            print(f"\nPlot 1763 (Malatavadi) pred properties:\n{row.to_dict()}")
            
    # 4. Read decision_statistics.csv for Vadnerbhairav (which ran second and is currently on disk)
    csv_path = Path("debug_output/predictions/decision_statistics.csv")
    if csv_path.exists():
        df = pd.read_csv(csv_path)
        print("\nFlagged plots in last run (Vadnerbhairav):")
        # Let's filter to flagged plots only
        flagged = df[df["status"] == "flagged"]
        for _, row in flagged.iterrows():
            # Only print first 10 to keep it clean, or print those that are benchmark plots
            if str(row["plot_number"]) in ["1145", "1403", "1476", "1710", "2647", "622"]:
                print(f"  Plot {row['plot_number']}: reason={row['decision_reason']}, conf={row['confidence']:.4f}, shift_mag={row['shift_magnitude']:.4f}")
                
if __name__ == "__main__":
    diagnose()
