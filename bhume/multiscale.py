"""Multi-scale processor module for the cadastral boundary correction pipeline.

This module constructs multi-resolution image pyramids (PyramidLevel) from
a PreprocessedPatch, adjusting spatial transforms and supporting debug
visualizations.
"""

from __future__ import annotations
from bhume.loader import CoordinateTransformer

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

import numpy as np
import scipy.ndimage
from affine import Affine
from PIL import Image, ImageDraw
from shapely.geometry.base import BaseGeometry

from bhume.config import Config
from bhume.preprocessor import PreprocessedPatch

logger = logging.getLogger(__name__)


@dataclass
class PyramidLevel:
    """A single level of the multi-resolution image pyramid."""

    scale: float
    image: np.ndarray  # (H_s, W_s, 3) float32 RGB
    gray: np.ndarray  # (H_s, W_s) float32 Grayscale
    boundary_hint: np.ndarray | None  # (H_s, W_s) float32 binary, or None
    transform: Affine  # Affine transform for this scale mapping pixels to CRS
    bounds: tuple[float, float, float, float]  # Bounds in EPSG:3857 (constant)


class MultiScalePatch:
    """Container holding the multi-resolution pyramid levels for a plot."""

    def __init__(
        self, plot_number: str, base_patch: PreprocessedPatch, levels: List[PyramidLevel]
    ) -> None:
        """Initialize MultiScalePatch.

        Args:
            plot_number: The plot identifier.
            base_patch: The source PreprocessedPatch.
            levels: List of generated PyramidLevel instances.
        """
        self.plot_number = plot_number
        self.base_patch = base_patch
        self.levels: Dict[float, PyramidLevel] = {lvl.scale: lvl for lvl in levels}

    def get_level(self, scale: float) -> PyramidLevel:
        """Retrieve the pyramid level for a specific scale.

        Args:
            scale: The scale factor to fetch.

        Returns:
            The corresponding PyramidLevel.

        Raises:
            KeyError if the scale is not present in the pyramid.
        """
        if scale not in self.levels:
            raise KeyError(
                f"Scale {scale} not found in pyramid. Available scales: {list(self.levels.keys())}"
            )
        return self.levels[scale]

    def iterate_levels(self) -> List[PyramidLevel]:
        """Get all pyramid levels sorted by scale in descending order (highest resolution first)."""
        return [self.levels[s] for s in sorted(self.levels.keys(), reverse=True)]


def save_multiscale_debug(
    plot_number: str,
    level: PyramidLevel,
    official_geom: BaseGeometry,
    transformer: CoordinateTransformer,
    out_dir: str,
) -> Path:
    """Save debug visualization for a specific pyramid level, showing alignment of the scaled transform."""
    d = Path(out_dir) / "multiscale"
    d.mkdir(parents=True, exist_ok=True)

    # Convert image to uint8 RGB
    img_uint8 = (level.image * 255.0).astype(np.uint8)
    pil_img = Image.fromarray(img_uint8, mode="RGB")

    # Draw scaled geometry
    draw = ImageDraw.Draw(pil_img)
    geom_crs = transformer.geom_to_crs(official_geom)
    inv_transform = ~level.transform

    def draw_ring(ring):
        pixel_pts = [inv_transform * pt for pt in ring.coords]
        draw.line(pixel_pts, fill=(0, 255, 0), width=2)  # Use Green for multiscale debug

    if geom_crs.geom_type == "Polygon":
        draw_ring(geom_crs.exterior)
        for interior in geom_crs.interiors:
            draw_ring(interior)
    elif geom_crs.geom_type == "MultiPolygon":
        for poly in geom_crs.geoms:
            draw_ring(poly.exterior)
            for interior in poly.interiors:
                draw_ring(interior)

    out_path = (
        d / f"plot_{plot_number}_scale_{level.scale:.2f}.png"
    )
    pil_img.save(out_path)
    logger.debug(f"Saved multiscale debug image to: {out_path}")
    return out_path


class MultiScaleProcessor:
    """Generates and manages multi-resolution image pyramids for plot patches."""

    def __init__(self, config: Config) -> None:
        """Initialize MultiScaleProcessor.

        Args:
            config: Configuration object containing scale factors.
        """
        self.config = config

    def generate_pyramid(
        self, base_patch: PreprocessedPatch, official_geom: BaseGeometry
    ) -> MultiScalePatch:
        """Generate pyramid levels from a PreprocessedPatch based on configuration scales.

        Args:
            base_patch: The source PreprocessedPatch at native scale (1.0).
            official_geom: The official plot geometry (used for debug visualization).

        Returns:
            A MultiScalePatch container containing all levels.
        """
        levels: List[PyramidLevel] = []
        H, W = base_patch.image.shape[0], base_patch.image.shape[1]

        for scale in self.config.scale_pyramids:
            if abs(scale - 1.0) < 1e-5:
                # Fast path: Native scale (1.0)
                level = PyramidLevel(
                    scale=1.0,
                    image=base_patch.image.copy(),
                    gray=base_patch.gray.copy(),
                    boundary_hint=(
                        base_patch.boundary_hint.copy()
                        if base_patch.boundary_hint is not None
                        else None
                    ),
                    transform=base_patch.transform,
                    bounds=base_patch.bounds,
                )
            else:
                # Compute target dimensions
                H_s = int(round(H * scale))
                W_s = int(round(W * scale))

                # Safeguard against extreme downsampling returning empty matrices
                if H_s < 3 or W_s < 3:
                    logger.warning(
                        f"Scale {scale} for plot {base_patch.plot_number} results in too small "
                        f"dimensions ({H_s}x{W_s}). Clamping to min 3 pixels."
                    )
                    H_s = max(H_s, 3)
                    W_s = max(W_s, 3)

                # Compute actual scales used for resizing
                zoom_h = H_s / H
                zoom_w = W_s / W

                # Resize RGB imagery (bilinear zoom)
                # scipy.ndimage.zoom expects zoom factors matching dimensions (H, W, Channels)
                img_zoomed = scipy.ndimage.zoom(
                    base_patch.image, (zoom_h, zoom_w, 1.0), order=1
                )
                # Clip to prevent numerical out-of-range after interpolation
                img_zoomed = np.clip(img_zoomed, 0.0, 1.0)

                # Resize grayscale imagery (bilinear zoom)
                gray_zoomed = scipy.ndimage.zoom(
                    base_patch.gray, (zoom_h, zoom_w), order=1
                )
                gray_zoomed = np.clip(gray_zoomed, 0.0, 1.0)

                # Resize boundary hint if available (nearest-neighbor zoom to preserve binary values)
                bnd_zoomed = None
                if base_patch.boundary_hint is not None:
                    bnd_zoomed = scipy.ndimage.zoom(
                        base_patch.boundary_hint, (zoom_h, zoom_w), order=0
                    )
                    bnd_zoomed = (bnd_zoomed > 0.5).astype(np.float32)

                # Scale affine transform: pixel spacing increases by factor of 1/scale
                # In rasterio/affine, scale is applied via matrix multiplication
                # (transforms map pixel indices -> meters)
                # Since pixels are larger, we scale transform columns/rows by (1/zoom_w, 1/zoom_h)
                scale_x = 1.0 / zoom_w
                scale_y = 1.0 / zoom_h
                scaled_transform = base_patch.transform * Affine.scale(scale_x, scale_y)

                level = PyramidLevel(
                    scale=scale,
                    image=img_zoomed,
                    gray=gray_zoomed,
                    boundary_hint=bnd_zoomed,
                    transform=scaled_transform,
                    bounds=base_patch.bounds,
                )

            levels.append(level)

            # Debug visualization
            if self.config.debug_visualize:
                save_multiscale_debug(
                    base_patch.plot_number,
                    level,
                    official_geom,
                    base_patch.transformer,
                    self.config.debug_out_dir,
                )

        return MultiScalePatch(
            plot_number=base_patch.plot_number, base_patch=base_patch, levels=levels
        )
