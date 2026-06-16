"""Validation script for confidence calibration on the public truth plots.

Computes ECE, Brier Score, Reliability Diagram, Confidence Histogram, and Confidence vs. IoU scatter plots.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import geopandas as gpd
from PIL import Image, ImageDraw
from shapely.geometry.base import BaseGeometry

from bhume.config import Config
from bhume.loader import load_village, CoordinateTransformer
from bhume.coordinator import PipelineCoordinator
from bhume.geo import open_imagery
from bhume.score import _utm_for, _iou

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def compute_ece(confidences: list[float], labels: list[bool], n_bins: int = 3) -> float:
    """Compute Expected Calibration Error (ECE) for binary labels."""
    confs = np.array(confidences)
    y_true = np.array(labels, dtype=float)
    bin_boundaries = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        bin_lower = bin_boundaries[i]
        bin_upper = bin_boundaries[i + 1]
        in_bin = (confs >= bin_lower) & (confs < bin_upper) if i < n_bins - 1 else (confs >= bin_lower) & (confs <= bin_upper)
        prop_in_bin = np.mean(in_bin)
        if prop_in_bin > 0:
            acc_in_bin = np.mean(y_true[in_bin])
            conf_in_bin = np.mean(confs[in_bin])
            ece += prop_in_bin * np.abs(acc_in_bin - conf_in_bin)
    return float(ece)


def compute_brier_score(confidences: list[float], labels: list[bool]) -> float:
    """Compute Brier Score."""
    confs = np.array(confidences)
    y_true = np.array(labels, dtype=float)
    return float(np.mean((confs - y_true) ** 2))


def draw_calibration_panel(
    village_slug: str,
    confidences: list[float],
    labels: list[bool],
    ious: list[float],
    out_path: Path
):
    """Draw a combined Reliability Diagram and Confidence vs IoU Scatter plot using PIL."""
    img_w, img_h = 800, 450
    img = Image.new("RGB", (img_w, img_h), color=(20, 20, 20))
    draw = ImageDraw.Draw(img)

    # Title
    draw.text((20, 15), f"Village {village_slug} Confidence Calibration Panel", fill=(255, 255, 255))

    # --- LEFT PANEL: Reliability Diagram (ECE) ---
    rx1, ry1 = 60, 60
    rx2, ry2 = 360, 360
    rw, rh = rx2 - rx1, ry2 - ry1

    # Draw diagram background & diagonal (perfect calibration)
    draw.rectangle([rx1, ry1, rx2, ry2], fill=(28, 28, 28), outline=(60, 60, 60))
    draw.line([(rx1, ry2), (rx2, ry1)], fill=(100, 100, 100), width=1)  # Diagonal

    # Axes labels
    draw.text((rx1 - 40, ry1 - 20), "Accuracy", fill=(180, 180, 180))
    draw.text((rx1 + rw // 2 - 40, ry2 + 25), "Confidence", fill=(180, 180, 180))

    # Y ticks and grid
    for i in range(5):
        f = i / 4.0
        y_pos = int(ry2 - f * rh)
        draw.line([(rx1 - 5, y_pos), (rx1, y_pos)], fill=(120, 120, 120))
        draw.text((rx1 - 35, y_pos - 5), f"{f:.2f}", fill=(150, 150, 150))
        if i > 0 and i < 4:
            draw.line([(rx1, y_pos), (rx2, y_pos)], fill=(40, 40, 40), width=1)

    # X ticks
    for i in range(5):
        f = i / 4.0
        x_pos = int(rx1 + f * rw)
        draw.line([(x_pos, ry2), (x_pos, ry2 + 5)], fill=(120, 120, 120))
        draw.text((x_pos - 15, ry2 + 10), f"{f:.2f}", fill=(150, 150, 150))

    # Bin accuracies
    n_bins = 3
    bin_boundaries = np.linspace(0, 1, n_bins + 1)
    confs_arr = np.array(confidences)
    labels_arr = np.array(labels, dtype=float)

    for i in range(n_bins):
        bin_lower = bin_boundaries[i]
        bin_upper = bin_boundaries[i + 1]
        in_bin = (confs_arr >= bin_lower) & (confs_arr < bin_upper) if i < n_bins - 1 else (confs_arr >= bin_lower) & (confs_arr <= bin_upper)
        if np.any(in_bin):
            acc = np.mean(labels_arr[in_bin])
            avg_conf = np.mean(confs_arr[in_bin])
            # Draw bin bar
            bx_center = int(rx1 + avg_conf * rw)
            by_acc = int(ry2 - acc * rh)
            # Draw bin point
            draw.ellipse([bx_center - 4, by_acc - 4, bx_center + 4, by_acc + 4], fill=(0, 255, 255))
            # Draw deviation line from diagonal
            diag_y = int(ry2 - avg_conf * rh)
            draw.line([(bx_center, diag_y), (bx_center, by_acc)], fill=(220, 70, 70), width=1)

    # --- RIGHT PANEL: Confidence vs IoU Scatter ---
    sx1, sy1 = 440, 60
    sx2, sy2 = 740, 360
    sw, sh = sx2 - sx1, sy2 - sy1

    # Draw scatter plot background
    draw.rectangle([sx1, sy1, sx2, sy2], fill=(28, 28, 28), outline=(60, 60, 60))

    # Axes labels
    draw.text((sx1 - 35, sy1 - 20), "Actual IoU", fill=(180, 180, 180))
    draw.text((sx1 + sw // 2 - 40, sy2 + 25), "Confidence", fill=(180, 180, 180))

    # Y ticks and grid
    for i in range(5):
        f = i / 4.0
        y_pos = int(sy2 - f * sh)
        draw.line([(sx1 - 5, y_pos), (sx1, y_pos)], fill=(120, 120, 120))
        draw.text((sx1 - 35, y_pos - 5), f"{f:.2f}", fill=(150, 150, 150))
        if i > 0 and i < 4:
            draw.line([(sx1, y_pos), (sx2, y_pos)], fill=(40, 40, 40), width=1)

    # X ticks
    for i in range(5):
        f = i / 4.0
        x_pos = int(sx1 + f * sw)
        draw.line([(x_pos, sy2), (x_pos, sy2 + 5)], fill=(120, 120, 120))
        draw.text((x_pos - 15, sy2 + 10), f"{f:.2f}", fill=(150, 150, 150))

    # Scatter points
    for c, iou in zip(confidences, ious):
        px = int(sx1 + c * sw)
        py = int(sy2 - iou * sh)
        # Clamp to graph area
        px = int(np.clip(px, sx1, sx2))
        py = int(np.clip(py, sy1, sy2))
        draw.ellipse([px - 4, py - 4, px + 4, py + 4], fill=(50, 200, 100), outline=(255, 255, 255))

    # Summary metrics below charts
    ece = compute_ece(confidences, labels)
    brier = compute_brier_score(confidences, labels)
    draw.text((20, 400), f"Plots count: {len(confidences)} | Expected Calibration Error (ECE): {ece:.4f} | Brier Score: {brier:.4f}", fill=(180, 180, 180))

    # Border
    draw.rectangle([5, 5, img_w - 5, img_h - 5], outline=(100, 100, 100), width=1)
    img.save(out_path)
    logger.info(f"Saved calibration panel image to: {out_path}")


def main():
    data_dir = Path("data")
    villages = [d for d in data_dir.iterdir() if d.is_dir() and (d / "example_truths.geojson").exists()]

    if not villages:
        logger.error("No unzipped village directories with example_truths.geojson found in data/")
        sys.exit(1)

    cfg = Config(debug_visualize=True, cache_enabled=False)
    coordinator = PipelineCoordinator(cfg)

    all_confs = []
    all_labels = []
    all_ious = []

    print("\n=====================================================================")
    print("      BHUME PIPELINE CONFIDENCE CALIBRATION ANALYSIS ON PUBLIC TRUTHS")
    print("=====================================================================\n")

    print(f"{'Village':<20} | {'Plot':<6} | {'Status':<10} | {'Confidence':<10} | {'Pred IoU':<8} | {'Official':<8} | {'Improvement':<11}")
    print("-" * 88)

    for v_dir in villages:
        village = load_village(v_dir)
        truth_plots = list(village.example_truths.index.astype(str))

        # Run pipeline only on the subset of truth plots
        res = coordinator.run_village(v_dir, plot_numbers=truth_plots)

        # Retrieve predictions
        pred_gdf = gpd.read_file(res.predictions_path)
        pred_gdf = pred_gdf.set_index('plot_number', drop=False)
        pred_gdf.index = pred_gdf.index.astype(str)

        truth_u = village.example_truths
        official_u = village.plots

        utm = _utm_for(truth_u.geometry.iloc[0])
        truth_u = truth_u.to_crs(utm)
        official_u = official_u.to_crs(utm)
        pred_u = pred_gdf.to_crs(utm)

        for pn in truth_plots:
            t = truth_u.loc[pn, 'geometry']
            o = official_u.loc[pn, 'geometry']
            iou_official = _iou(o, t)

            row = pred_gdf.loc[pn]
            status = str(row.get('status'))
            conf = float(row.get('confidence'))

            pg = pred_u.loc[pn, 'geometry']
            iou_pred = _iou(pg, t)
            improvement = iou_pred - iou_official

            print(f"{village.slug[:20]:<20} | {pn:<6} | {status:<10} | {conf:<10.4f} | {iou_pred:<8.4f} | {iou_official:<8.4f} | {improvement:<+11.4f}")

            # Collect metrics (flagged plots count as accuracy = iou_official >= 0.5)
            # Corrected plots accuracy = iou_pred >= 0.5
            accuracy_label = (iou_pred >= 0.5)
            all_confs.append(conf)
            all_labels.append(accuracy_label)
            all_ious.append(iou_pred)

    out_dir = Path("debug_output") / "calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    draw_calibration_panel(
        "Combined_Truths",
        all_confs,
        all_labels,
        all_ious,
        out_dir / "combined_truths_calibration.png"
    )

    ece = compute_ece(all_confs, all_labels)
    brier = compute_brier_score(all_confs, all_labels)
    print("\n" + "=" * 88)
    print(f"OVERALL CALIBRATION STATISTICS:")
    print(f"  Expected Calibration Error (ECE): {ece:.4f}")
    print(f"  Brier Score:                     {brier:.4f}")
    print("=" * 88 + "\n")


if __name__ == "__main__":
    main()
