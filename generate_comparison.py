import json
import csv
from pathlib import Path
import geopandas as gpd
import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry


def _utm_for(geom: BaseGeometry) -> str:
    lon = geom.centroid.x
    return f"EPSG:{32600 + int((lon + 180) // 6) + 1}"


def _iou(a: BaseGeometry, b: BaseGeometry) -> float:
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    union = a.union(b).area
    return float(a.intersection(b).area / union) if union > 0 else 0.0


def generate_comparison_csv(village_dir: Path, output_csv_path: Path):
    village_slug = village_dir.name
    print(f"\nGenerating truths comparison CSV for village: {village_slug}")

    pred_path = village_dir / "predictions.geojson"
    truths_path = village_dir / "example_truths.geojson"
    input_path = village_dir / "input.geojson"

    if not pred_path.exists():
        print(f"Error: {pred_path} does not exist. Run pipeline first.")
        return
    if not truths_path.exists():
        print(f"Warning: {truths_path} does not exist for {village_slug}. Skipping.")
        return
    if not input_path.exists():
        print(f"Error: {input_path} does not exist.")
        return

    # Load GeoDataFrames
    pred = gpd.read_file(pred_path).set_index("plot_number", drop=False)
    truth = gpd.read_file(truths_path).set_index("plot_number", drop=False)
    official = gpd.read_file(input_path).set_index("plot_number", drop=False)

    # Force plot_number index to string
    pred.index = pred.index.astype(str)
    truth.index = truth.index.astype(str)
    official.index = official.index.astype(str)

    # Project to local UTM for physical measurements in meters
    utm = _utm_for(truth.geometry.iloc[0])
    truth_u = truth.to_crs(utm)
    official_u = official.to_crs(utm)
    pred_u = pred.to_crs(utm)

    # Read decision statistics to get optimizer scores & improvements
    # The decision statistics CSV is located at: debug_output/predictions/decision_statistics.csv
    # But wait, in coordinator.py, PredictionWriter saves statistics in `debug_output/predictions/decision_statistics.csv`.
    # Note: since Malatavadi runs separate from Vadnerbhairav, and they share the same Config.debug_out_dir,
    # did the statistics get overwritten, or should we load them?
    # Actually, the statistics CSV has rows for the plots processed in the latest run.
    # Let's read the CSV and build a mapping by plot_number.
    stats_map = {}
    stats_csv_path = Path("debug_output/predictions/decision_statistics.csv")
    if stats_csv_path.exists():
        with open(stats_csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                pn = row.get("plot_number")
                if pn:
                    stats_map[str(pn)] = row

    rows = []
    for pn in truth.index:
        if pn not in official_u.index:
            print(f"Warning: Truth plot {pn} not in input cadastre plots.")
            continue

        t_geom = truth_u.loc[pn, "geometry"]
        o_geom = official_u.loc[pn, "geometry"]

        # If predicted, get pred geom; otherwise fallback to official geom (naive)
        if pn in pred_u.index:
            p_geom = pred_u.loc[pn, "geometry"]
            status = pred.loc[pn, "status"]
            confidence = pred.loc[pn, "confidence"]
        else:
            p_geom = o_geom
            status = "not_attempted"
            confidence = 0.0

        iou = _iou(p_geom, t_geom)
        centroid_err = p_geom.centroid.distance(t_geom.centroid)

        # Translation vector in meters relative to official starting centroid
        dx = p_geom.centroid.x - o_geom.centroid.x
        dy = p_geom.centroid.y - o_geom.centroid.y

        # Get values from statistics CSV
        row_stats = stats_map.get(pn, {})
        opt_score = float(row_stats.get("correctability_score", 0.0))  # fallback
        # Let's look for optimization score in row_stats:
        # Columns in decision_statistics.csv:
        # plot_number,status,confidence,correctability_score,optimization_quality,score_improvement,applied_dx,applied_dy,shift_magnitude,decision_reason
        opt_score = float(row_stats.get("optimization_quality", 0.0))
        score_imp = float(row_stats.get("score_improvement", 0.0))
        reg_dx = float(row_stats.get("applied_dx", 0.0))
        reg_dy = float(row_stats.get("applied_dy", 0.0))

        rows.append({
            "plot_number": pn,
            "status": status,
            "IoU": round(iou, 4),
            "centroid_error": round(centroid_err, 4),
            "translation_vector_dx": round(dx, 4),
            "translation_vector_dy": round(dy, 4),
            "confidence": round(float(confidence), 6),
            "optimization_score": round(opt_score, 4),
            "score_improvement": round(score_imp, 4),
            "regularized_shift_dx": round(reg_dx, 4),
            "regularized_shift_dy": round(reg_dy, 4),
        })

    # Write to output CSV
    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "plot_number",
        "status",
        "IoU",
        "centroid_error",
        "translation_vector_dx",
        "translation_vector_dy",
        "confidence",
        "optimization_score",
        "score_improvement",
        "regularized_shift_dx",
        "regularized_shift_dy",
    ]
    with open(output_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved comparison CSV to: {output_csv_path}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        # Defaults
        generate_comparison_csv(
            Path("data/village_Malatavadi"),
            Path("debug_output/pipeline/malatavadi_truths_comparison.csv")
        )
    else:
        generate_comparison_csv(Path(sys.argv[1]), Path(sys.argv[2]))
