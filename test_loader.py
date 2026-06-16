import sys
from pathlib import Path
from bhume.config import Config
from bhume.loader import load_village, CoordinateTransformer, build_neighbor_graph
from bhume.geo import open_imagery

def test_village(village_path: str):
    print(f"\n--- Testing Village Loader & Config for: {village_path} ---")
    
    # 1. Test Config Loading
    cfg = Config(debug_visualize=True)
    print("Loaded Config successfully. Debug visualize:", cfg.debug_visualize)
    print("Scoring weights:", cfg.scoring_weights)
    
    # 2. Test Village Loading
    village = load_village(village_path)
    print(f"Loaded village: {village.slug}")
    print(f"Number of plots: {len(village.plots)}")
    
    # 3. Test CoordinateTransformer
    with open_imagery(village.imagery_path) as src:
        transformer = CoordinateTransformer(src)
        
        # Take the first plot geometry centroid
        first_plot = village.plots.iloc[0]
        centroid = first_plot.geometry.centroid
        lon, lat = centroid.x, centroid.y
        
        # Test lonlat <-> CRS conversion
        x, y = transformer.lonlat_to_crs(lon, lat)
        lon_back, lat_back = transformer.crs_to_lonlat(x, y)
        print(f"Original Lon/Lat: ({lon:.6f}, {lat:.6f})")
        print(f"CRS Coordinates: ({x:.2f}, {y:.2f})")
        print(f"Reverted Lon/Lat: ({lon_back:.6f}, {lat_back:.6f})")
        
        # Check delta
        delta_deg = abs(lon - lon_back) + abs(lat - lat_back)
        assert delta_deg < 1e-7, f"Lon/Lat translation failed: delta={delta_deg}"
        print("Lon/Lat <-> CRS reversibility check passed.")
        
        # Test CRS <-> Pixel conversion
        col, row = transformer.crs_to_pixel(x, y)
        x_back, y_back = transformer.pixel_to_crs(col, row)
        print(f"Pixel Coordinates: (col={col:.2f}, row={row:.2f})")
        print(f"Reverted CRS Coordinates: ({x_back:.2f}, {y_back:.2f})")
        
        # Check delta
        delta_m = abs(x - x_back) + abs(y - y_back)
        assert delta_m < 1e-4, f"CRS/Pixel translation failed: delta={delta_m} meters"
        print("CRS <-> Pixel reversibility check passed.")
        
    # 4. Test Topology Neighbor Graph
    # To keep it fast, we can test on a subset of first 100 plots, or run full graph
    print("Building neighbor graph (this might take a second)...")
    graph = build_neighbor_graph(village.plots)
    print(f"Neighbor graph built successfully for {len(graph)} plots.")
    
    # Calculate some stats
    lengths = [len(n) for n in graph.values()]
    avg_neighbors = sum(lengths) / len(lengths)
    max_neighbors = max(lengths)
    min_neighbors = min(lengths)
    print(f"Neighbor stats: avg={avg_neighbors:.2f}, min={min_neighbors}, max={max_neighbors}")
    
    # Let's inspect a plot that has neighbors
    sample_plot_id = None
    for pid, neighbors in graph.items():
        if len(neighbors) > 2:
            sample_plot_id = pid
            break
            
    if sample_plot_id:
        print(f"Sample Plot '{sample_plot_id}' neighbors and shared boundary lengths (meters):")
        for neighbor_id, shared_len in graph[sample_plot_id].items():
            print(f"  Neighbor '{neighbor_id}': shared length = {shared_len:.2f} m")
            
    print("All checks completed successfully!")

def main():
    test_village("data/village_Malatavadi")
    test_village("data/village_Vadnerbhairav")

if __name__ == "__main__":
    main()
