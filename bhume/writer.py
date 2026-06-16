"""Prediction writer module for the cadastral boundary correction pipeline.

Formats and outputs corrected geometries and pipeline diagnostics according to the submission contract.
"""

from __future__ import annotations

import csv
import datetime
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Union

import numpy as np
import geopandas as gpd
from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry
from shapely.geometry import mapping
from shapely.ops import transform as shp_transform

from bhume.decision import DecisionResult, DecisionEngine

logger = logging.getLogger(__name__)


class PredictionWriterError(Exception):
    """Base exception for PredictionWriter errors."""
    pass


class DuplicatePlotNumberError(PredictionWriterError):
    """Raised when a plot number is duplicated in predictions."""
    pass


class MissingPlotNumberError(PredictionWriterError):
    """Raised when a plot number is missing or not in original input.geojson."""
    pass


class InvalidStatusError(PredictionWriterError):
    """Raised when status is not 'corrected' or 'flagged'."""
    pass


class InvalidConfidenceError(PredictionWriterError):
    """Raised when confidence value is not in [0.0, 1.0]."""
    pass


class InvalidGeometryError(PredictionWriterError):
    """Raised when geometry is invalid or empty."""
    pass


def round_coordinates(geom: BaseGeometry, precision: int = 7) -> BaseGeometry:
    """Recursively round geometry coordinates to a specific precision."""
    if geom is None or geom.is_empty:
        return geom
    def _round(x, y, z=None):
        if z is not None:
            return np.round(x, precision), np.round(y, precision), np.round(z, precision)
        return np.round(x, precision), np.round(y, precision)
    return shp_transform(_round, geom)


def round_geojson_coords(coords, precision: int = 7):
    """Recursively round GeoJSON coordinate lists/tuples to a specific precision."""
    if isinstance(coords, float):
        return round(coords, precision)
    elif isinstance(coords, (list, tuple)):
        return [round_geojson_coords(c, precision) for c in coords]
    return coords


def generate_method_note(res: DecisionResult) -> str:
    """Generate or retrieve a method note for a decision result."""
    note = getattr(res, "method_note", None)
    if note:
        if len(note) > 150:
            note = note[:147] + "..."
        return note

    if res.status == "corrected":
        note = "Image alignment with edge matching and neighbor regularization."
    else:
        note = "Low confidence or ambiguous optimization landscape."

    return note


class PredictionWriter:
    """Formats and writes predictions GeoJSON, summary JSON, and decision diagnostics."""

    def __init__(self, config) -> None:
        """Initialize PredictionWriter.

        Args:
            config: Pipeline configuration settings.
        """
        self.config = config

    def write(
        self,
        village_dir: Path,
        results: List[DecisionResult] | Dict[str, DecisionResult],
        engine: DecisionEngine | None = None,
        source_crs: str | None = None,
        transformer: object = None,
    ) -> Path:
        """Compile, validate, and write predictions for a village.

        Args:
            village_dir: Path to the village directory.
            results: Dict of {plot_number: DecisionResult} or List[DecisionResult].
            engine: The DecisionEngine instance (optional).
            source_crs: Explicit coordinate reference system of results' final geometries.
            transformer: Optional CoordinateTransformer instance.

        Returns:
            Path to the written predictions.geojson.
        """
        village_dir = Path(village_dir)
        village_slug = village_dir.name

        # Convert input to a deterministic ordered list sorted by plot_number
        if isinstance(results, dict):
            results_list = list(results.values())
        else:
            results_list = list(results)

        results_list = sorted(results_list, key=lambda r: str(r.plot_number))

        # Load original plot ids from input.geojson
        input_geojson_path = village_dir / "input.geojson"
        original_plot_ids = set()
        if input_geojson_path.exists():
            with open(input_geojson_path, "r", encoding="utf-8") as f:
                input_data = json.load(f)
                for feature in input_data.get("features", []):
                    pn = feature.get("properties", {}).get("plot_number")
                    if pn is not None:
                        original_plot_ids.add(str(pn))
        else:
            raise FileNotFoundError(f"Input file {input_geojson_path} does not exist.")

        # Variables for contract validation report
        schema_valid = True
        geometry_valid = True
        crs_valid = True
        duplicate_plots_found = False
        invalid_confidence_found = False
        validation_passed = True
        error_to_raise = None

        seen_plot_numbers = set()
        validated_features = []

        try:
            for res in results_list:
                plot_number = res.plot_number

                # Plot Number Checks
                if plot_number is None or str(plot_number).strip() == "":
                    schema_valid = False
                    raise MissingPlotNumberError("Plot number is missing or empty.")

                plot_number_str = str(plot_number)
                if not isinstance(plot_number, str):
                    schema_valid = False
                    raise MissingPlotNumberError(f"Plot number {plot_number} must be a string.")

                if plot_number_str in seen_plot_numbers:
                    duplicate_plots_found = True
                    raise DuplicatePlotNumberError(f"Duplicate plot number: {plot_number_str}")
                seen_plot_numbers.add(plot_number_str)

                if plot_number_str not in original_plot_ids:
                    schema_valid = False
                    raise MissingPlotNumberError(
                        f"Plot number {plot_number_str} not found in original input.geojson."
                    )

                # Status Check
                if res.status not in ("corrected", "flagged"):
                    raise InvalidStatusError(
                        f"Plot {plot_number_str} has invalid status '{res.status}'."
                    )

                # Confidence Check
                if res.confidence is None or not isinstance(res.confidence, (int, float)) or not (0.0 <= res.confidence <= 1.0):
                    invalid_confidence_found = True
                    raise InvalidConfidenceError(
                        f"Plot {plot_number_str} has invalid confidence {res.confidence}."
                    )

                # Geometry Check
                geom = res.final_geometry
                if geom is None or geom.is_empty:
                    geometry_valid = False
                    raise InvalidGeometryError(
                        f"Plot {plot_number_str} has missing or empty geometry."
                    )

                # Attempt buffer(0) once to repair
                if not geom.is_valid:
                    geom = geom.buffer(0)

                if not geom.is_valid or geom.is_empty:
                    geometry_valid = False
                    raise InvalidGeometryError(
                        f"Plot {plot_number_str} has topologically invalid geometry."
                    )

                # CRS validation and reprojection
                if source_crs is not None:
                    try:
                        from_crs_obj = CRS(source_crs)
                        to_crs_obj = CRS("EPSG:4326")
                        if not from_crs_obj.equals(to_crs_obj):
                            proj_transformer = Transformer.from_crs(
                                from_crs_obj, to_crs_obj, always_xy=True
                            )
                            geom = shp_transform(proj_transformer.transform, geom)
                    except Exception as e:
                        crs_valid = False
                        raise InvalidGeometryError(
                            f"Plot {plot_number_str} failed CRS reprojection: {e}"
                        )

                # Final WGS84 bounds check (never infer, but verify)
                centroid = geom.centroid
                if not (-180.0 <= centroid.x <= 180.0 and -90.0 <= centroid.y <= 90.0):
                    crs_valid = False
                    raise InvalidGeometryError(
                        f"Plot {plot_number_str} coordinates are not in WGS84 bounds (centroid: {centroid.x}, {centroid.y})."
                    )

                # Round coordinates to 7 decimal places
                geom = round_coordinates(geom, precision=7)

                # Round confidence to 6 decimal places
                conf_rounded = round(float(res.confidence), 6)

                # Method note generation
                method_note = generate_method_note(res)

                validated_features.append((plot_number_str, res.status, conf_rounded, method_note, geom))

        except PredictionWriterError as e:
            validation_passed = False
            error_to_raise = e

        # Ensure debug directory exists
        debug_dir = Path(getattr(self.config, "debug_out_dir", "debug_output")) / "predictions"
        debug_dir.mkdir(parents=True, exist_ok=True)

        # Write contract validation report
        report_data = {
            "schema_valid": schema_valid,
            "geometry_valid": geometry_valid,
            "crs_valid": crs_valid,
            "duplicate_plots": duplicate_plots_found,
            "invalid_confidence": invalid_confidence_found,
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "validation_passed": validation_passed,
        }
        report_path = debug_dir / "contract_validation_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report_data, f, indent=4)

        if error_to_raise is not None:
            logger.error(f"Contract validation failed: {error_to_raise}")
            raise error_to_raise

        # Construct GeoJSON FeatureCollection
        features = []
        for plot_number_str, status, confidence, method_note, geom in validated_features:
            geom_dict = mapping(geom)
            geom_dict["coordinates"] = round_geojson_coords(geom_dict["coordinates"], 7)

            feature = {
                "type": "Feature",
                "geometry": geom_dict,
                "properties": {
                    "plot_number": plot_number_str,
                    "status": status,
                    "confidence": confidence,
                    "method_note": method_note,
                }
            }
            features.append(feature)

        geojson_data = {
            "type": "FeatureCollection",
            "crs": {
                "type": "name",
                "properties": {
                    "name": "urn:ogc:def:crs:OGC:1.3:CRS84"
                }
            },
            "features": features
        }

        # Write official GeoJSON output
        output_path = village_dir / "predictions.geojson"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(geojson_data, f)
        logger.info(f"Wrote predictions for village {village_slug} to {output_path}")

        # Write predictions_summary.json
        total_plots = len(results_list)
        corrected_count = sum(1 for r in results_list if r.status == "corrected")
        flagged_count = total_plots - corrected_count

        # Sanity guard: corrected plots must have VARYING confidence values.
        # A near-constant distribution means calibration broke upstream
        # (flat confidence -> calibration AUC 0.5, the most-weighted metric).
        corrected_confs = {
            round(float(r.confidence), 4) for r in results_list if r.status == "corrected"
        }
        if corrected_count > 10 and len(corrected_confs) <= 3:
            logger.warning(
                f"Village {village_slug}: only {len(corrected_confs)} unique confidence "
                f"value(s) across {corrected_count} corrected plots. Confidence calibration "
                f"is likely broken (flat confidence -> AUC 0.5)."
            )

        all_confs = {
            round(float(r.confidence), 4) for r in results_list
        }
        if len(results_list) > 10 and len(all_confs) < 4:
            logger.warning(
                f"Village {village_slug}: only {len(all_confs)} unique confidence "
                f"value(s) across all {len(results_list)} (corrected + flagged) plots. "
                f"Confidence calibration is likely broken."
            )

        confidences = [float(r.confidence) for r in results_list]
        shifts = []
        for r in results_list:
            dx, dy = getattr(r, "applied_shift", (0.0, 0.0))
            shifts.append(float(np.hypot(dx, dy)))

        avg_conf = float(np.mean(confidences)) if confidences else 0.0
        med_conf = float(np.median(confidences)) if confidences else 0.0
        avg_shift = float(np.mean(shifts)) if shifts else 0.0
        med_shift = float(np.median(shifts)) if shifts else 0.0

        summary_data = {
            "total_plots": total_plots,
            "corrected": corrected_count,
            "flagged": flagged_count,
            "unique_corrected_confidences": len(corrected_confs),
            "average_confidence": avg_conf,
            "median_confidence": med_conf,
            "average_shift": avg_shift,
            "median_shift": med_shift,
        }
        summary_path = debug_dir / "predictions_summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_data, f, indent=4)

        # Write decision_statistics.csv
        stats_path = debug_dir / "decision_statistics.csv"
        with open(stats_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                "plot_number",
                "status",
                "confidence",
                "correctability_score",
                "optimization_quality",
                "score_improvement",
                "applied_dx",
                "applied_dy",
                "shift_magnitude",
                "decision_reason",
            ])
            for r in results_list:
                dx, dy = getattr(r, "applied_shift", (0.0, 0.0))
                opt_qual = r.decision_signals.get("optimization_quality", 0.0) if hasattr(r, "decision_signals") and r.decision_signals else 0.0
                reasons = ";".join(r.decision_reason) if hasattr(r, "decision_reason") and r.decision_reason else ""
                writer.writerow([
                    r.plot_number,
                    r.status,
                    r.confidence,
                    r.correctability_score,
                    opt_qual,
                    r.score_improvement,
                    dx,
                    dy,
                    np.hypot(dx, dy),
                    reasons,
                ])

        # Write diagnostic_plot_log.csv (Part 6)
        diag_path = debug_dir / "diagnostic_plot_log.csv"
        with open(diag_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                "plot_number",
                "optimizer_score",
                "official_score",
                "score_improvement",
                "confidence",
                "decision_score",
                "decision",
                "score_gap",
                "ambiguity",
                "peak_sharpness",
                "repeatability",
                "translation",
                "neighbor_score",
                "edge_score",
                "area_score",
                "reason_for_flag",
            ])
            for r in results_list:
                trace = getattr(r, "decision_trace", None) or {}
                dx, dy = getattr(r, "applied_shift", (0.0, 0.0))
                opt_score = trace.get("optimized_score", 0.0)
                off_score = trace.get("official_score", 0.0)
                if off_score is None:
                    off_score = 0.0
                score_imp = trace.get("raw_score_improvement", 0.0)
                dec_score = trace.get("decision_score", 0.0)
                
                score_gap = trace.get("score_gap", 0.0)
                ambiguity = trace.get("ambiguity_index", 0.0)
                peak_sharpness = trace.get("peak_sharpness", 0.0)
                repeatability = trace.get("repeatability_score", 0.0)
                translation = trace.get("shift_magnitude", np.hypot(dx, dy))
                
                neighbor_score = trace.get("neighbor_score", 0.5)
                if neighbor_score is None:
                    neighbor_score = 0.5
                edge_score = trace.get("edge_score", 0.5)
                if edge_score is None:
                    edge_score = 0.5
                area_score = trace.get("area_score", 0.5)
                if area_score is None:
                    area_score = 0.5
                    
                reasons = ";".join(r.decision_reason) if hasattr(r, "decision_reason") and r.decision_reason else ""
                
                writer.writerow([
                    r.plot_number,
                    opt_score,
                    off_score,
                    score_imp,
                    r.confidence,
                    dec_score,
                    r.status,
                    score_gap,
                    ambiguity,
                    peak_sharpness,
                    repeatability,
                    translation,
                    neighbor_score,
                    edge_score,
                    area_score,
                    reasons,
                ])
                
                if r.status == "flagged":
                    logger.info(
                        f"[PLOT_FLAGGED] plot={r.plot_number} score={opt_score:.4f} "
                        f"off_score={off_score:.4f} imp={score_imp:.4f} dec_score={dec_score:.4f} "
                        f"reasons={reasons}"
                    )

        # Write summary.txt
        corr_rate = float(corrected_count / total_plots) if total_plots > 0 else 0.0
        avg_improvement = float(np.mean([r.score_improvement for r in results_list])) if results_list else 0.0

        txt_path = debug_dir / "summary.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"Total plots: {total_plots}\n")
            f.write(f"Corrected: {corrected_count}\n")
            f.write(f"Flagged: {flagged_count}\n")
            f.write(f"Correction rate: {corr_rate:.6f}\n")
            f.write(f"Average confidence: {avg_conf:.6f}\n")
            f.write(f"Average improvement: {avg_improvement:.6f}\n")
            f.write(f"Average shift: {avg_shift:.6f}\n")

        # Generate PIL diagnostic charts if engine is provided
        if engine is not None:
            engine.generate_diagnostics(village_slug, results)
            logger.info(f"Generated decision diagnostics charts for village {village_slug}")

        return output_path
