import pandas as pd
import json
from pathlib import Path

def curate():
    df = pd.read_csv("all_plots_baseline_results.csv")
    
    # Truth plots
    # We can load these from example_truths.geojson for each village
    truth_plots = {
        "village_Malatavadi": ["1119", "1120", "1118"], # Let's read from file dynamically to be safe
        "village_Vadnerbhairav": []
    }
    
    for name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        truth_path = Path("data") / name / "example_truths.geojson"
        if truth_path.exists():
            import geopandas as gpd
            gdf = gpd.read_file(truth_path)
            truth_plots[name] = list(gdf.index.astype(str))
            # Just in case the index is not plot numbers, let's see if there is plot_number column
            if 'plot_number' in gdf.columns:
                truth_plots[name] = list(gdf['plot_number'].astype(str))
            
    print("Truth plots discovered:", truth_plots)
    
    benchmark_by_village = {}
    
    for name in ["village_Malatavadi", "village_Vadnerbhairav"]:
        df_v = df[df["village"] == name].copy()
        df_v["plot_number"] = df_v["plot_number"].astype(str)
        
        # 1. Example truths and their 1st-degree neighbors
        t_plots = set(truth_plots[name])
        # Find neighbors of truth plots
        village_dir = Path("data") / name
        from bhume.loader import load_village, build_neighbor_graph
        village = load_village(village_dir)
        graph = build_neighbor_graph(village.plots)
        
        t_plots_and_neighbors = set(t_plots)
        for tp in t_plots:
            if tp in graph:
                t_plots_and_neighbors.update(graph[tp].keys())
                
        # Make sure they exist in the village plots
        t_plots_and_neighbors = {tp for tp in t_plots_and_neighbors if tp in df_v["plot_number"].values}
        
        # 2. Low-confidence plots: status == corrected but confidence < 0.25
        low_conf = df_v[df_v["confidence"] < 0.25]["plot_number"].head(15).tolist()
        
        # 3. Large-shift plots: shift_magnitude > 15.0
        large_shift = df_v[df_v["shift_magnitude"] > 15.0]["plot_number"].head(15).tolist()
        
        # 4. Ambiguous-peak plots: number_of_local_maxima > 200 or score_gap < 0.015
        ambig_peak = df_v[(df_v["number_of_local_maxima"] > 200) | (df_v["score_gap"] < 0.015)]["plot_number"].head(15).tolist()
        
        # 5. Flagged plots: status == flagged
        flagged = df_v[df_v["status"] == "flagged"]["plot_number"].head(15).tolist()
        
        # Combine all
        all_selected = set()
        all_selected.update(t_plots_and_neighbors)
        all_selected.update(low_conf)
        all_selected.update(large_shift)
        all_selected.update(ambig_peak)
        all_selected.update(flagged)
        
        benchmark_by_village[name] = sorted(list(all_selected))
        print(f"Selected {len(benchmark_by_village[name])} plots for {name}")
        print(f"  Truths + Neighbors: {len(t_plots_and_neighbors)}")
        print(f"  Low Conf: {len(set(low_conf))}")
        print(f"  Large Shift: {len(set(large_shift))}")
        print(f"  Ambig Peak: {len(set(ambig_peak))}")
        print(f"  Flagged: {len(set(flagged))}")
        
    with open("scratch/benchmark_plots.json", "w") as f:
        json.dump(benchmark_by_village, f, indent=4)
    print("Saved benchmark plots to scratch/benchmark_plots.json")

if __name__ == "__main__":
    curate()
