"""Preprocessor module for the cadastral boundary correction pipeline.

This module extracts local crops (patches) for both imagery and boundary
rasters, normalizes pixel values using percentile-based contrast stretching,
and supports debug visualizations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import numpy as np
import rasterio
from PIL import Image, ImageDraw
from shapely.geometry.base import BaseGeometry

from bhume.config import Config
from bhume.io import Village
from bhume.loader import CoordinateTransformer

logger = logging.getLogger(__name__)


@dataclass
class PreprocessedPatch:
    """Dataclass holding preprocessed local spatial data for a single plot."""

    plot_number: str
    image: np.ndarray  # (H, W, 3) float32 RGB normalized to [0, 1]
    gray: np.ndarray  # (H, W) float32 Grayscale normalized to [0, 1]
    boundary_hint: (
        np.ndarray | None
    )  # (H, W) float32 binary boundaries, or None
    transform: object  # affine transform mapping patch pixels to CRS
    bounds: Tuple[float, float, float, float]  # (left, bottom, right, top)
    transformer: CoordinateTransformer


def enhance_contrast(img: np.ndarray) -> np.ndarray:
    """Enhance image contrast using 2nd and 98th percentile stretching.

    Args:
        img: Grayscale or RGB image array (any dtype).

    Returns:
        Float32 array normalized to [0, 1].
    """
    img_float = img.astype(np.float32)
    p2, p98 = np.percentile(img_float, (2, 98))
    span = p98 - p2
    if span < 1e-5:
        # Avoid division by zero in zero-contrast patches
        return np.zeros_like(img_float)

    stretched = np.clip((img_float - p2) / span, 0.0, 1.0)
    return stretched


def save_preprocessing_debug(
    patch: PreprocessedPatch, official_geom: BaseGeometry, out_dir: str
) -> Path:
    """Save debug visualization overlaying the official polygon on the preprocessed patch."""
    # Ensure directory exists
    d = Path(out_dir) / "preprocessing"
    d.mkdir(parents=True, exist_ok=True)

    # Convert normalized image to uint8 RGB for Pillow
    img_uint8 = (patch.image * 255.0).astype(np.uint8)
    pil_img = Image.fromarray(img_uint8, mode="RGB")

    # Draw the official polygon contour in red
    draw = ImageDraw.Draw(pil_img)

    # Convert geometry coordinates to pixel coordinates in the patch
    geom_crs = patch.transformer.geom_to_crs(official_geom)
    inv_transform = ~patch.transform

    def draw_ring(ring):
        pixel_pts = [inv_transform * pt for pt in ring.coords]
        # ImageDraw polygon requires tuples
        draw.line(pixel_pts, fill=(255, 0, 0), width=2)

    if geom_crs.geom_type == "Polygon":
        draw_ring(geom_crs.exterior)
        for interior in geom_crs.interiors:
            draw_ring(interior)
    elif geom_crs.geom_type == "MultiPolygon":
        for poly in geom_crs.geoms:
            draw_ring(poly.exterior)
            for interior in poly.interiors:
                draw_ring(interior)

    # Save image
    out_path = d / f"plot_{patch.plot_number}_preprocess.png"
    pil_img.save(out_path)
    logger.debug(f"Saved preprocessing debug visualization to: {out_path}")
    return out_path


class Preprocessor:
    """Extracts and prepares normalized local image and boundary crops around plots."""

    def __init__(self, config: Config) -> None:
        """Initialize the Preprocessor.

        Args:
            config: Pipeline configuration object.
        """
        self.config = config

    def process_plot(self, village: Village, plot_number: str) -> PreprocessedPatch:
        """Extract and preprocess the local raster patches for a single plot.

        Args:
            village: The active village bundle object.
            plot_number: The plot number identifier.

        Returns:
            PreprocessedPatch containing normalized arrays and transforms.
        """
        geom_4326 = village.plot(plot_number)
        if geom_4326 is None or geom_4326.is_empty:
            raise ValueError(f"Plot {plot_number} has empty or missing geometry.")

        # 1. Open rasters
        with rasterio.open(village.imagery_path) as img_src:
            transformer = CoordinateTransformer(img_src)
            geom_crs = transformer.geom_to_crs(geom_4326)

            # 2. Compute crop bounding box with padding
            minx, miny, maxx, maxy = geom_crs.bounds
            pad = self.config.patch_pad_m
            left, bottom, right, top = (
                minx - pad,
                miny - pad,
                maxx + pad,
                maxy + pad,
            )

            # Clip crop extent to imagery bounds to handle edges
            dl, db, dr, dt = img_src.bounds
            left, bottom, right, top = (
                max(left, dl),
                max(bottom, db),
                min(right, dr),
                min(top, dt),
            )

            if right <= left or top <= bottom:
                raise ValueError(
                    f"Padded plot bounds for {plot_number} do not overlap imagery footprint."
                )

            # 3. Read imagery patch
            window = rasterio.windows.from_bounds(
                left, bottom, right, top, transform=img_src.transform
            )
            # Read bands 1, 2, 3 (RGB)
            rgb = img_src.read([1, 2, 3], window=window)
            img_patch = np.transpose(rgb, (1, 2, 0))  # Convert to (H, W, 3)
            patch_transform = img_src.window_transform(window)
            H_img, W_img = img_patch.shape[0], img_patch.shape[1]

        # 4. Enhance contrast and normalize RGB to [0, 1]
        norm_img = enhance_contrast(img_patch)

        # 5. Compute normalized grayscale image
        # Standard luminance conversion: Y = 0.299 R + 0.587 G + 0.114 B
        gray_patch = (
            0.299 * norm_img[:, :, 0]
            + 0.587 * norm_img[:, :, 1]
            + 0.114 * norm_img[:, :, 2]
        ).astype(np.float32)

        # 6. Read and resample optional boundary patch
        boundary_patch = None
        if village.boundaries_path and village.boundaries_path.exists():
            with rasterio.open(village.boundaries_path) as bnd_src:
                bnd_window = rasterio.windows.from_bounds(
                    left, bottom, right, top, transform=bnd_src.transform
                )
                bnd_raw = bnd_src.read(1, window=bnd_window)

                # Resize boundaries to match imagery patch dimensions pixel-for-pixel
                if bnd_raw.size > 0:
                    pil_bnd = Image.fromarray(bnd_raw)
                    pil_bnd_resized = pil_bnd.resize(
                        (W_img, H_img), resample=Image.NEAREST
                    )
                    # Convert to normalized float32 [0, 1] binary edge map
                    boundary_patch = (np.array(pil_bnd_resized) > 127).astype(
                        np.float32
                    )

        # Create the resulting patch object
        preprocessed = PreprocessedPatch(
            plot_number=plot_number,
            image=norm_img,
            gray=gray_patch,
            boundary_hint=boundary_patch,
            transform=patch_transform,
            bounds=(left, bottom, right, top),
            transformer=transformer,
        )

        # 7. Debug visualization support
        if self.config.debug_visualize:
            save_preprocessing_debug(
                preprocessed, geom_4326, self.config.debug_out_dir
            )

        return preprocessed
