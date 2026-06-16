"""Contour detector module for the cadastral boundary correction pipeline.

This module parameterizes official plot boundaries by sampling them at uniform
intervals, computing tangents, outward normals, cumulative lengths, and curvature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import shapely.ops
from PIL import Image, ImageDraw
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from bhume.config import Config
from bhume.loader import CoordinateTransformer

logger = logging.getLogger(__name__)


@dataclass
class BoundaryRepresentation:
    """Parametric representation of a plot boundary contour."""

    plot_number: str
    sampled_points: np.ndarray  # (N, 2) float64 coordinates in CRS meters
    tangents: np.ndarray  # (N, 2) float64 normalized tangent vectors
    normals: np.ndarray  # (N, 2) float64 normalized outward normal vectors
    curvature: np.ndarray  # (N,) float64 curvature values (rad/m)
    cumulative_length: np.ndarray  # (N,) float64 distance along the contour (meters)
    total_length: float  # Total perimeter in meters
    sampling_interval: float  # Configured interval in meters


def save_contour_debug(
    boundary: BoundaryRepresentation,
    transformer: CoordinateTransformer,
    image_shape: tuple[int, int],
    out_dir: str,
) -> Path:
    """Save debug visualization showing sampled points, normals, tangents, and indices."""
    d = Path(out_dir) / "contours"
    d.mkdir(parents=True, exist_ok=True)

    H, W = image_shape
    # Create dark background image to clearly inspect vectors
    pil_img = Image.new("RGB", (W, H), color=(20, 20, 20))
    draw = ImageDraw.Draw(pil_img)

    # Invert transform to map CRS meters to patch pixel space
    inv_transform = transformer.inv_transform

    pts = boundary.sampled_points
    N = len(pts)

    # Convert all points to pixels
    pixel_pts = [inv_transform * (pt[0], pt[1]) for pt in pts]

    # Draw the boundary lines
    draw.line(pixel_pts + [pixel_pts[0]], fill=(100, 100, 100), width=1)

    # Draw tangents, normals, and indices
    for i in range(N):
        x_m, y_m = pts[i]
        px, py = pixel_pts[i]

        # Draw sampled point (blue circle)
        draw.ellipse([px - 2, py - 2, px + 2, py + 2], fill=(0, 120, 255))

        # 3-meter vectors
        vec_len_m = 3.0
        tx_m, ty_m = boundary.tangents[i] * vec_len_m
        nx_m, ny_m = boundary.normals[i] * vec_len_m

        # Tangent endpoint (Green)
        px_t, py_t = inv_transform * (x_m + tx_m, y_m + ty_m)
        draw.line([(px, py), (px_t, py_t)], fill=(0, 255, 0), width=1)

        # Normal endpoint (Red)
        px_n, py_n = inv_transform * (x_m + nx_m, y_m + ny_m)
        draw.line([(px, py), (px_n, py_n)], fill=(255, 0, 0), width=1)

        # Draw index for every 10th point to avoid clutter
        if i % 10 == 0:
            draw.text((px + 4, py - 4), str(i), fill=(200, 200, 200))

    out_path = d / f"plot_{boundary.plot_number}_contour.png"
    pil_img.save(out_path)
    logger.debug(f"Saved contour debug visualization to: {out_path}")
    return out_path


class ContourDetector:
    """Samples and parameterizes plot boundary geometries."""

    def __init__(self, config: Config) -> None:
        """Initialize ContourDetector.

        Args:
            config: Config object containing the sampling interval.
        """
        self.config = config

    def parameterize_boundary(
        self,
        plot_number: str,
        geom_4326: BaseGeometry,
        transformer: CoordinateTransformer,
        image_shape: tuple[int, int] | None = None,
    ) -> BoundaryRepresentation:
        """Sample points along the exterior boundary and compute normals and tangents.

        Args:
            plot_number: The unique plot identifier.
            geom_4326: The official plot geometry in EPSG:4326.
            transformer: The CoordinateTransformer mapping to EPSG:3857.
            image_shape: (H, W) tuple to resize/visualize (optional).

        Returns:
            BoundaryRepresentation of the parameterized boundary.
        """
        # 1. Project geometry to raster CRS EPSG:3857 (meters)
        geom_crs = transformer.geom_to_crs(geom_4326)

        # 2. Extract the main Polygon component for MultiPolygons
        if geom_crs.geom_type == "MultiPolygon":
            logger.debug(
                f"Plot {plot_number} is MultiPolygon. Extracting largest component by area."
            )
            poly = max(geom_crs.geoms, key=lambda p: p.area)
        elif geom_crs.geom_type == "Polygon":
            poly = geom_crs
        else:
            raise ValueError(
                f"Unsupported geometry type: {geom_crs.geom_type} for plot {plot_number}"
            )

        # Clean topology self-intersections
        if not poly.is_valid:
            poly = poly.buffer(0.0)

        # 3. Force counter-clockwise orientation so normals consistently point outward
        poly_ccw = shapely.ops.orient(poly, sign=1.0)
        exterior = poly_ccw.exterior
        total_length = exterior.length

        # 4. Compute sampling distances
        ds = self.config.contour_sample_interval_m
        if total_length < ds:
            # For extremely small plots, ensure we still sample at least 5 points
            ds = max(total_length / 5.0, 0.1)

        distances = np.arange(0.0, total_length, ds, dtype=np.float64)
        if len(distances) < 3:
            distances = np.linspace(
                0.0, total_length, 5, endpoint=False, dtype=np.float64
            )

        # 5. Interpolate points along the exterior ring
        pts_list = [exterior.interpolate(d) for d in distances]
        sampled_points = np.array(
            [[pt.x, pt.y] for pt in pts_list], dtype=np.float64
        )
        N = len(sampled_points)

        # 6. Compute Tangents using central difference
        tangents = np.zeros_like(sampled_points)
        for i in range(N):
            p_next = sampled_points[(i + 1) % N]
            p_prev = sampled_points[(i - 1) % N]
            diff = p_next - p_prev
            norm = np.linalg.norm(diff)
            if norm > 1e-6:
                tangents[i] = diff / norm
            else:
                # Fallback to forward difference if points overlap
                diff_fwd = p_next - sampled_points[i]
                norm_fwd = np.linalg.norm(diff_fwd)
                tangents[i] = (
                    diff_fwd / norm_fwd if norm_fwd > 1e-6 else np.array([1.0, 0.0])
                )

        # 7. Compute Outward Normals
        # Since the ring is CCW, the outward normal is rotated 90 degrees clockwise from tangent
        # If tangent T = (tx, ty), outward normal N = (ty, -tx)
        normals = np.zeros_like(sampled_points)
        normals[:, 0] = tangents[:, 1]
        normals[:, 1] = -tangents[:, 0]

        # 8. Compute Local Curvature (rate of change of tangent angle)
        # Curvature K = d_theta / d_s
        angles = np.arctan2(tangents[:, 1], tangents[:, 0])
        curvature = np.zeros(N, dtype=np.float64)
        for i in range(N):
            a_next = angles[(i + 1) % N]
            a_prev = angles[(i - 1) % N]
            # Handle angle wrap-around in [-pi, pi]
            diff_angle = np.arctan2(
                np.sin(a_next - a_prev), np.cos(a_next - a_prev)
            )
            # Central difference over 2*ds
            curvature[i] = diff_angle / (2.0 * ds)

        representation = BoundaryRepresentation(
            plot_number=plot_number,
            sampled_points=sampled_points,
            tangents=tangents,
            normals=normals,
            curvature=curvature,
            cumulative_length=distances,
            total_length=float(total_length),
            sampling_interval=float(ds),
        )

        # 9. Debug visualization
        if self.config.debug_visualize and image_shape is not None:
            save_contour_debug(representation, transformer, image_shape, self.config.debug_out_dir)

        return representation
