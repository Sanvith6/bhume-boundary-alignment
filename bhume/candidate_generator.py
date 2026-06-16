"""Candidate generator module for the cadastral boundary correction pipeline.

This module generates candidate geometric transformations (CandidateTransformation)
using a hierarchical search strategy, supporting debug visualizations of the search grid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple

import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import Point

from bhume.config import Config
from bhume.loader import CoordinateTransformer
from bhume.preprocessor import PreprocessedPatch

logger = logging.getLogger(__name__)


@dataclass
class CandidateTransformation:
    """Dataclass representing a generic candidate transformation."""

    dx: float  # Translation in X direction (meters)
    dy: float  # Translation in Y direction (meters)
    rotation: float = 0.0  # Rotation in degrees (optional, default 0)
    scale: float = 1.0  # Scale factor (optional, default 1)
    search_level: str = "coarse"  # 'coarse', 'refinement', 'sub-meter'
    parent_candidate_id: str | None = None  # ID of the parent candidate refined from
    score: float | None = None  # Alignment score (filled later by scorer)
    metadata: Dict = field(default_factory=dict)  # Extensible metadata dictionary

    @property
    def candidate_id(self) -> str:
        """Generate a unique string identifier based on the transformation parameters."""
        return f"{self.dx:.4f}_{self.dy:.4f}_{self.rotation:.4f}_{self.scale:.4f}"


def save_candidate_search_debug(
    plot_number: str,
    patch: PreprocessedPatch,
    official_geom_4326: object,
    candidates: List[CandidateTransformation],
    out_dir: str,
) -> Path:
    """Save debug visualization plotting candidate translation grid centroids over the patch."""
    d = Path(out_dir) / "candidate_search"
    d.mkdir(parents=True, exist_ok=True)

    # Convert grayscale image to RGB for visualization overlays
    gray_uint8 = (patch.gray * 255.0).clip(0, 255).astype(np.uint8)
    pil_img = Image.fromarray(gray_uint8).convert("RGB")
    draw = ImageDraw.Draw(pil_img)

    transformer = patch.transformer
    inv_transform = ~patch.transform

    # Centroid of the official polygon
    geom_crs = transformer.geom_to_crs(official_geom_4326)
    centroid_crs = geom_crs.centroid
    cx, cy = centroid_crs.x, centroid_crs.y

    # Draw candidates
    for cand in candidates:
        # Translate centroid by candidates offset
        tx = cx + cand.dx
        ty = cy + cand.dy

        # Convert back to pixel space
        px, py = inv_transform * (tx, ty)

        # Color-code based on search levels
        if cand.search_level == "coarse":
            color = (255, 255, 0)  # Yellow
            size = 1
        elif cand.search_level == "refinement":
            color = (0, 255, 255)  # Cyan
            size = 1
        else:
            color = (255, 0, 255)  # Magenta
            size = 1

        # Draw a small dot
        draw.ellipse([px - size, py - size, px + size, py + size], fill=color)

    # Also draw the official centroid in red
    px_c, py_c = inv_transform * (cx, cy)
    draw.ellipse([px_c - 3, py_c - 3, px_c + 3, py_c + 3], fill=(255, 0, 0))

    out_path = d / f"plot_{plot_number}_search_grid.png"
    pil_img.save(out_path)
    logger.debug(f"Saved candidate search debug visualization to: {out_path}")
    return out_path


class CandidateGenerator:
    """Generates structured CandidateTransformation coordinates using hierarchical grids."""

    def __init__(self, config: Config) -> None:
        """Initialize CandidateGenerator.

        Args:
            config: Config settings.
        """
        self.config = config

    def generate_coarse(
        self, center_dx: float = 0.0, center_dy: float = 0.0
    ) -> List[CandidateTransformation]:
        """Generate a uniform coarse translation grid in meters.

        Args:
            center_dx: Grid center offset X in meters.
            center_dy: Grid center offset Y in meters.

        Returns:
            List of CandidateTransformation objects.
        """
        radius = self.config.search_radius_m
        step = self.config.search_step_m

        if step <= 0.0:
            raise ValueError(f"Coarse search step size must be positive, got {step}")

        if radius <= 0.0:
            # Return center point only
            return [
                CandidateTransformation(
                    dx=center_dx, dy=center_dy, search_level="coarse"
                )
            ]

        # Generate coordinate arrays
        x_offsets = np.arange(-radius, radius + 1e-5, step) + center_dx
        y_offsets = np.arange(-radius, radius + 1e-5, step) + center_dy

        candidates: List[CandidateTransformation] = []
        for dx in x_offsets:
            for dy in y_offsets:
                candidates.append(
                    CandidateTransformation(dx=float(dx), dy=float(dy), search_level="coarse")
                )

        logger.debug(
            f"Generated {len(candidates)} coarse candidates centered at ({center_dx:.2f}, {center_dy:.2f})"
        )
        return candidates

    def refine_around(
        self,
        parent: CandidateTransformation,
        radius: float | None = None,
        step: float | None = None,
        level: str = "refinement",
    ) -> List[CandidateTransformation]:
        """Generate a finer grid of candidates centered around a parent transformation.

        Args:
            parent: The parent CandidateTransformation to refine around.
            radius: Refinement radius in meters (falls back to config if None).
            step: Refinement step size in meters (falls back to config if None).
            level: Label for this search stage.

        Returns:
            List of refined CandidateTransformation objects.
        """
        if radius is None:
            radius = self.config.fine_search_radius_m
        if step is None:
            step = self.config.fine_search_step_m

        if step <= 0.0:
            raise ValueError(f"Refinement step size must be positive, got {step}")

        if radius <= 0.0:
            return []

        # Generate local offset coordinate grids
        x_offsets = np.arange(-radius, radius + 1e-5, step) + parent.dx
        y_offsets = np.arange(-radius, radius + 1e-5, step) + parent.dy

        parent_id = parent.candidate_id
        candidates: List[CandidateTransformation] = []

        for dx in x_offsets:
            for dy in y_offsets:
                cand = CandidateTransformation(
                    dx=float(dx),
                    dy=float(dy),
                    rotation=parent.rotation,
                    scale=parent.scale,
                    search_level=level,
                    parent_candidate_id=parent_id,
                )
                # Avoid adding the exact parent coordinate itself to prevent duplicate evaluations
                if cand.candidate_id != parent_id:
                    candidates.append(cand)

        logger.debug(
            f"Generated {len(candidates)} refined candidates around ({parent.dx:.2f}, {parent.dy:.2f}) at level: {level}"
        )
        return candidates

    def visualize_search(
        self,
        plot_number: str,
        patch: PreprocessedPatch,
        official_geom_4326: object,
        candidates: List[CandidateTransformation],
    ) -> Path | None:
        """Expose visual debugging support to save candidate coordinates overlaid on the patch."""
        if self.config.debug_visualize:
            return save_candidate_search_debug(
                plot_number,
                patch,
                official_geom_4326,
                candidates,
                self.config.debug_out_dir,
            )
        return None
