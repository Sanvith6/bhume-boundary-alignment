"""Edge detector module for the cadastral boundary correction pipeline.

This module implements multiple edge detection strategies (Sobel, Scharr,
Canny, Morphological, and Combined) and computes Euclidean Distance
Transforms calibrated in meters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import scipy.ndimage
from PIL import Image, ImageDraw

from bhume.config import Config
from bhume.loader import CoordinateTransformer
from bhume.multiscale import MultiScalePatch, PyramidLevel

logger = logging.getLogger(__name__)


@dataclass
class EdgeLevelResult:
    """Edge detection results for a single pyramid level."""

    scale: float
    edges: np.ndarray  # (H, W) float32 binary mask (0.0 or 1.0)
    magnitude: np.ndarray  # (H, W) float32 gradient magnitude in [0, 1]
    orientation: np.ndarray  # (H, W) float32 gradient orientation in radians [-pi, pi]
    edt: np.ndarray  # (H, W) float32 Euclidean Distance Transform in METERS


@dataclass
class EdgePatchResult:
    """Collection of edge detection results for all scales of a plot."""

    plot_number: str
    levels: Dict[float, EdgeLevelResult]

    def get_level(self, scale: float) -> EdgeLevelResult:
        """Get the edge level result for a specific scale."""
        if scale not in self.levels:
            raise KeyError(
                f"Scale {scale} not found in edge results. Available: {list(self.levels.keys())}"
            )
        return self.levels[scale]


def _non_maximum_suppression(mag: np.ndarray, angle: np.ndarray) -> np.ndarray:
    """Vectorized non-maximum suppression to thin edges."""
    H, W = mag.shape
    nms = np.zeros_like(mag)
    # Convert angle to [0, 180) degrees
    angle_deg = np.rad2deg(angle) % 180

    # Pad image to handle boundary neighbors
    padded = np.pad(mag, 1, mode="constant", constant_values=0)

    # Slice neighbor matrices
    # 0 degrees (horizontal)
    n_0_1 = padded[1:-1, 2:]
    n_0_2 = padded[1:-1, :-2]

    # 45 degrees (diagonal /)
    n_45_1 = padded[2:, 2:]
    n_45_2 = padded[:-2, :-2]

    # 90 degrees (vertical)
    n_90_1 = padded[2:, 1:-1]
    n_90_2 = padded[:-2, 1:-1]

    # 135 degrees (diagonal \)
    n_135_1 = padded[2:, :-2]
    n_135_2 = padded[:-2, 2:]

    # Masks for angles
    m_0 = (
        (angle_deg >= 0) & (angle_deg < 22.5)
    ) | ((angle_deg >= 157.5) & (angle_deg <= 180))
    m_45 = (angle_deg >= 22.5) & (angle_deg < 67.5)
    m_90 = (angle_deg >= 67.5) & (angle_deg < 112.5)
    m_135 = (angle_deg >= 112.5) & (angle_deg < 157.5)

    # Determine peaks
    mask = np.zeros_like(mag, dtype=bool)
    mask[m_0] = (mag[m_0] >= n_0_1[m_0]) & (mag[m_0] >= n_0_2[m_0])
    mask[m_45] = (mag[m_45] >= n_45_1[m_45]) & (mag[m_45] >= n_45_2[m_45])
    mask[m_90] = (mag[m_90] >= n_90_1[m_90]) & (mag[m_90] >= n_90_2[m_90])
    mask[m_135] = (mag[m_135] >= n_135_1[m_135]) & (mag[m_135] >= n_135_2[m_135])

    nms[mask] = mag[mask]
    return nms


def _canny_hysteresis(
    nms: np.ndarray, low_thresh: float, high_thresh: float
) -> np.ndarray:
    """Canny edge tracking by hysteresis using connected component labeling."""
    all_edges = nms >= low_thresh
    strong_edges = nms >= high_thresh

    # Connected component labeling of all candidate edge pixels
    labels, _ = scipy.ndimage.label(all_edges)

    # Find the labels associated with strong edge pixels
    strong_labels = np.unique(labels[strong_edges])

    # Keep only those components that contain a strong edge pixel (and ignore background 0)
    canny_mask = np.isin(labels, strong_labels) & (labels > 0)
    return canny_mask.astype(np.float32)


def save_edge_debug(
    plot_number: str,
    level: PyramidLevel,
    result: EdgeLevelResult,
    out_dir: str,
) -> None:
    """Save debug visualizations for edge detection steps of a level."""
    d = Path(out_dir) / "edges"
    d.mkdir(parents=True, exist_ok=True)

    def to_img(arr, mode="L"):
        val = (arr * 255.0).clip(0, 255).astype(np.uint8)
        return Image.fromarray(val, mode=mode)

    # Save original grayscale
    to_img(level.gray).save(
        d / f"plot_{plot_number}_scale_{level.scale:.2f}_gray.png"
    )

    # Save detected binary edges
    to_img(result.edges).save(
        d / f"plot_{plot_number}_scale_{level.scale:.2f}_edges.png"
    )

    # Save gradient magnitude
    to_img(result.magnitude).save(
        d / f"plot_{plot_number}_scale_{level.scale:.2f}_gradient.png"
    )

    # Save Distance Transform (visualized normalized)
    max_d = max(result.edt.max(), 1.0)
    to_img(result.edt / max_d).save(
        d / f"plot_{plot_number}_scale_{level.scale:.2f}_edt.png"
    )


class EdgeDetector:
    """Computes edge maps and metric distance transforms using multiple strategies."""

    def __init__(self, config: Config) -> None:
        """Initialize EdgeDetector.

        Args:
            config: Config settings.
        """
        self.config = config
        # Adaptive fallbacks if parameters aren't present in config
        self.canny_high = getattr(config, "canny_high_threshold", 0.2)
        self.canny_low = getattr(config, "canny_low_threshold", 0.08)
        self.canny_sigma = getattr(config, "canny_gaussian_sigma", 1.0)

    def detect_pyramid(
        self, ms_patch: MultiScalePatch, method: str = "combined", plot_area_m2: float | None = None
    ) -> EdgePatchResult:
        """Detect edges for all levels of a MultiScalePatch.

        Args:
            ms_patch: MultiScalePatch container.
            method: The edge strategy: 'sobel', 'scharr', 'canny',
              'morphological', 'combined'.
            plot_area_m2: Optional area of the plot to dynamically scale parameters.

        Returns:
            EdgePatchResult containing results for all scales.
        """
        levels: Dict[float, EdgeLevelResult] = {}
        
        d_eq = None
        if plot_area_m2 is not None:
            d_eq = 2.0 * np.sqrt(plot_area_m2 / np.pi)

        for scale, lvl in ms_patch.levels.items():
            # 1. Gradients and edge detection
            if method == "sobel":
                edges, mag, ori = self._detect_sobel(lvl.gray)
            elif method == "scharr":
                edges, mag, ori = self._detect_scharr(lvl.gray)
            elif method == "canny":
                effective_sigma = None
                if d_eq is not None and d_eq < 30.0:
                    effective_sigma = max(0.5, self.canny_sigma * (d_eq / 30.0))
                edges, mag, ori = self._detect_canny(lvl.gray, effective_sigma)
            elif method == "morphological":
                edges, mag, ori = self._detect_morphological(lvl.gray)
            elif method == "combined":
                edges, mag, ori = self._detect_combined(lvl.gray, lvl.boundary_hint, d_eq)
            else:
                raise ValueError(f"Unknown edge detection method: {method}")

            # 2. Compute Distance Transform in METERS
            # EDT measures distance to nearest 0 (edge pixel), so we run it on edges == 0
            edt_pixels = scipy.ndimage.distance_transform_edt(edges == 0.0)

            # Get physical pixel size from transform
            res_x = abs(lvl.transform.a)
            res_y = abs(lvl.transform.e)
            res_m = (res_x + res_y) / 2.0  # Average resolution in meters

            # Convert pixel distance to meter distance
            edt_meters = (edt_pixels * res_m).astype(np.float32)

            res_lvl = EdgeLevelResult(
                scale=scale,
                edges=edges,
                magnitude=mag,
                orientation=ori,
                edt=edt_meters,
            )
            levels[scale] = res_lvl

            # 3. Debug visualization support
            if self.config.debug_visualize:
                save_edge_debug(
                    ms_patch.plot_number,
                    lvl,
                    res_lvl,
                    self.config.debug_out_dir,
                )

        return EdgePatchResult(plot_number=ms_patch.plot_number, levels=levels)

    def _compute_gradients(
        self, gray: np.ndarray, kernel_x: np.ndarray, kernel_y: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Utility to convolve image with x/y kernels and compute mag/ori."""
        # Pad image to prevent edge artifacts
        padded = np.pad(gray, 1, mode="edge")
        Ix = scipy.ndimage.convolve(padded, kernel_x)[1:-1, 1:-1]
        Iy = scipy.ndimage.convolve(padded, kernel_y)[1:-1, 1:-1]

        mag = np.hypot(Ix, Iy)
        # Normalize magnitude to [0, 1] relative to theoretical max
        max_possible = max(mag.max(), 1e-5)
        mag = mag / max_possible

        ori = np.arctan2(Iy, Ix)
        return Ix, Iy, mag, ori

    def _detect_sobel(
        self, gray: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Sobel edge detection strategy."""
        Kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32)
        Ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.float32)
        _, _, mag, ori = self._compute_gradients(gray, Kx, Ky)

        # Threshold Sobel gradient magnitude to get binary edges
        edges = (mag > 0.15).astype(np.float32)
        return edges, mag, ori

    def _detect_scharr(
        self, gray: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Scharr edge detection strategy."""
        Kx = np.array([[-3, 0, 3], [-10, 0, 10], [-3, 0, 3]], dtype=np.float32)
        Ky = np.array([[-3, -10, -3], [0, 0, 0], [3, 10, 3]], dtype=np.float32)
        _, _, mag, ori = self._compute_gradients(gray, Kx, Ky)

        edges = (mag > 0.15).astype(np.float32)
        return edges, mag, ori

    def _detect_canny(
        self, gray: np.ndarray, effective_sigma: float | None = None
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Canny edge detection strategy (vectorized NMS + hysteresis)."""
        # Step 1: Smooth image with Gaussian filter
        sigma = effective_sigma if effective_sigma is not None else self.canny_sigma
        smoothed = scipy.ndimage.gaussian_filter(gray, sigma)

        # Step 2: Compute Sobel gradients
        Kx = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], dtype=np.float32)
        Ky = np.array([[-1, -2, -1], [0, 0, 0], [1, 2, 1]], dtype=np.float32)
        _, _, mag, ori = self._compute_gradients(smoothed, Kx, Ky)

        # Step 3: Non-maximum suppression (NMS) to thin edges
        nms = _non_maximum_suppression(mag, ori)

        # Step 4: Edge tracking by hysteresis
        edges = _canny_hysteresis(nms, self.canny_low, self.canny_high)
        return edges, mag, ori

    def _detect_morphological(
        self, gray: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Morphological Gradient edge detection strategy (dilation - erosion)."""
        dilation = scipy.ndimage.grey_dilation(gray, size=(3, 3))
        erosion = scipy.ndimage.grey_erosion(gray, size=(3, 3))
        mag = np.clip(dilation - erosion, 0.0, 1.0)

        # Binary thresholding
        edges = (mag > 0.15).astype(np.float32)
        # Orientation is arbitrary for morph gradient, return zero
        ori = np.zeros_like(gray)
        return edges, mag, ori

    def _detect_combined(
        self, gray: np.ndarray, boundary_hint: np.ndarray | None, d_eq: float | None = None
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Combined edge detection strategy fusing Canny imagery edges and boundary hints."""
        effective_sigma = None
        if d_eq is not None and d_eq < 30.0:
            effective_sigma = max(0.5, self.canny_sigma * (d_eq / 30.0))

        # 1. Run Canny edge detector on grayscale satellite imagery
        canny_edges, canny_mag, canny_ori = self._detect_canny(gray, effective_sigma)

        # 2. Fuse with optional boundary hint using Semantic Edge Gating
        if boundary_hint is not None:
            hint_mask = boundary_hint > 0.5
            if np.any(hint_mask):
                # Compute distance to the nearest semantic hint pixel
                dist_from_hint = scipy.ndimage.distance_transform_edt(~hint_mask)
                
                # Create a continuous decay mask: 1.0 at the hint, decaying outwards
                base_corridor = getattr(self.config, "semantic_corridor_width_px", 10.0)
                if d_eq is not None and d_eq < 30.0:
                    corridor_width_px = max(4.0, base_corridor * (d_eq / 30.0))
                else:
                    corridor_width_px = base_corridor
                corridor_mask = np.exp(-0.5 * (dist_from_hint / corridor_width_px) ** 2)
                
                # Suppress Canny edges that are far from the semantic hint
                # Only keep Canny edges within the effective corridor
                valid_canny_mask = corridor_mask > 0.05
                filtered_canny = canny_edges * valid_canny_mask
                
                # The combined binary edges used for the final EDT
                combined_edges = (filtered_canny > 0.5) | hint_mask
                # The combined magnitude is softly modulated
                combined_mag = np.maximum(canny_mag * corridor_mask, boundary_hint)
            else:
                combined_edges = canny_edges > 0.5
                combined_mag = canny_mag
        else:
            combined_edges = canny_edges > 0.5
            combined_mag = canny_mag

        return combined_edges.astype(np.float32), combined_mag, canny_ori
