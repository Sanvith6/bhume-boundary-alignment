"""Alignment scorer module for the cadastral boundary correction pipeline.

This module evaluates CandidateTransformations against local spatial evidence
and geometric constraints using a configurable, weighted aggregation system.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import scipy.ndimage
from PIL import Image, ImageDraw
import shapely.geometry as sg

from bhume.candidate_generator import CandidateTransformation
from bhume.config import Config
from bhume.contour_detector import BoundaryRepresentation
from bhume.edge_detector import EdgeLevelResult
from bhume.loader import CoordinateTransformer

try:
    import torch
    import torch.nn.functional as F
except ImportError:
    torch = None

logger = logging.getLogger(__name__)


@dataclass
class ScoredCandidate:
    """Dataclass holding evaluation scores for a CandidateTransformation."""

    candidate: CandidateTransformation
    total_score: float
    feature_scores: Dict[str, float]
    metadata: Dict = field(default_factory=dict)


def transform_points(
    pts: np.ndarray,
    centroid: Tuple[float, float],
    dx: float,
    dy: float,
    rotation_deg: float,
    scale: float,
) -> np.ndarray:
    """Apply translation, rotation, and scaling to points relative to a centroid."""
    cx, cy = centroid
    rel_pts = pts - np.array([cx, cy], dtype=np.float32)

    if abs(rotation_deg) > 1e-5:
        rad = np.radians(rotation_deg)
        cos_a, sin_a = np.cos(rad), np.sin(rad)
        rot_mat = np.array([[cos_a, -sin_a], [sin_a, cos_a]], dtype=np.float32)
        rel_pts = np.dot(rel_pts, rot_mat.T)

    if abs(scale - 1.0) > 1e-5:
        rel_pts = rel_pts * scale

    transformed = rel_pts + np.array([cx + dx, cy + dy], dtype=np.float32)
    return transformed


def transform_vectors(vectors: np.ndarray, rotation_deg: float) -> np.ndarray:
    """Rotate direction vectors (tangents/normals) by an angle."""
    if abs(rotation_deg) < 1e-5:
        return vectors.copy()

    rad = np.radians(rotation_deg)
    cos_a, sin_a = np.cos(rad), np.sin(rad)
    rot_mat = np.array([[cos_a, -sin_a], [sin_a, cos_a]], dtype=np.float32)
    return np.dot(vectors, rot_mat.T)


def save_score_alignment_debug(
    plot_number: str,
    boundary: BoundaryRepresentation,
    edge_res: EdgeLevelResult,
    candidate: CandidateTransformation,
    transformer: CoordinateTransformer,
    patch_transform: object,
    point_scores: np.ndarray,
    out_dir: str,
) -> Path:
    """Save debug visualization showing the translated boundary overlaid on the edge map."""
    d = Path(out_dir) / "score_maps"
    d.mkdir(parents=True, exist_ok=True)

    edge_uint8 = (edge_res.edges * 255.0).clip(0, 255).astype(np.uint8)
    pil_img = Image.fromarray(edge_uint8).convert("RGB")
    draw = ImageDraw.Draw(pil_img)

    inv_transform = ~patch_transform

    # Use the boundary geometry centroid in the same coordinate system as sampled_points.
    try:
        poly = sg.Polygon(np.asarray(boundary.sampled_points))
        centroid = poly.centroid
        if centroid.is_empty:
            raise ValueError("Empty centroid")
        cx, cy = float(centroid.x), float(centroid.y)
    except Exception:
        pts = np.asarray(boundary.sampled_points)
        cx = float(np.mean(pts[:, 0]))
        cy = float(np.mean(pts[:, 1]))

    trans_pts = transform_points(
        boundary.sampled_points,
        (cx, cy),
        candidate.dx,
        candidate.dy,
        candidate.rotation,
        candidate.scale,
    )

    pixel_pts = [inv_transform * (pt[0], pt[1]) for pt in trans_pts]

    # Draw contour outline
    if len(pixel_pts) > 1:
        draw.line(pixel_pts + [pixel_pts[0]], fill=(127, 127, 127), width=1)

    # Draw points color-coded by local scores
    for i, (px, py) in enumerate(pixel_pts):
        score_val = float(np.clip(point_scores[i], 0.0, 1.0))
        r = int((1.0 - score_val) * 255)
        g = int(score_val * 255)
        draw.ellipse([px - 2, py - 2, px + 2, py - 2 + 4], fill=(r, g, 0))

    out_path = d / f"plot_{plot_number}_cand_{candidate.candidate_id}.png"
    pil_img.save(out_path)
    return out_path


def save_score_heatmap_debug(
    plot_number: str,
    scored_candidates: List[ScoredCandidate],
    out_dir: str,
) -> Path | None:
    """Save a 2D heatmap visualization of scores over translation offsets (dx, dy)."""
    grid_cands = [sc for sc in scored_candidates if sc.candidate.search_level == "coarse"]
    if not grid_cands:
        return None

    d = Path(out_dir) / "score_maps"
    d.mkdir(parents=True, exist_ok=True)

    dxs = np.array([sc.candidate.dx for sc in grid_cands], dtype=np.float32)
    dys = np.array([sc.candidate.dy for sc in grid_cands], dtype=np.float32)

    unique_dx = np.unique(dxs)
    unique_dy = np.unique(dys)

    if len(unique_dx) < 2 or len(unique_dy) < 2:
        return None

    score_grid = np.zeros((len(unique_dy), len(unique_dx)), dtype=np.float32)
    dx_to_idx = {val: idx for idx, val in enumerate(unique_dx)}
    dy_to_idx = {val: idx for idx, val in enumerate(unique_dy)}

    for sc in grid_cands:
        col = dx_to_idx.get(sc.candidate.dx)
        row = dy_to_idx.get(sc.candidate.dy)
        if col is not None and row is not None:
            score_grid[len(unique_dy) - 1 - row, col] = sc.total_score

    min_s, max_s = float(score_grid.min()), float(score_grid.max())
    span = max_s - min_s
    if span < 1e-5:
        norm_grid = np.zeros_like(score_grid, dtype=np.uint8)
    else:
        norm_grid = ((score_grid - min_s) / span * 255.0).astype(np.uint8)

    img = Image.fromarray(norm_grid, mode="L").resize((300, 300), resample=Image.NEAREST)
    out_path = d / f"plot_{plot_number}_score_heatmap.png"
    img.save(out_path)
    logger.debug("Saved score heatmap visualization to: %s", out_path)
    return out_path


def _circular_true_runs(matched: np.ndarray) -> List[int]:
    """Return lengths of contiguous True runs on a circular binary sequence."""
    matched = np.asarray(matched, dtype=bool).ravel()
    N = matched.size
    if N == 0 or not np.any(matched):
        return []
    if np.all(matched):
        return [N]

    # Rotate so that the sequence starts at a False entry.
    false_idx = np.where(~matched)[0]
    start = int(false_idx[0])
    rotated = np.roll(matched, -start)

    padded = np.concatenate(([False], rotated, [False]))
    changes = np.diff(padded.astype(np.int8))
    starts = np.where(changes == 1)[0]
    ends = np.where(changes == -1)[0]
    return (ends - starts).tolist()


def compute_robust_score(p: np.ndarray) -> float:
    """Robust point aggregation:
    0.7 * mean + 0.3 * 25th percentile - 0.15 * std, clipped to [0, 1].
    """
    p = np.asarray(p, dtype=np.float32)
    if p.size == 0:
        return 0.0
    mean = float(np.mean(p))
    p25 = float(np.percentile(p, 25))
    std = float(np.std(p))
    return float(np.clip(0.7 * mean + 0.3 * p25 - 0.15 * std, 0.0, 1.0))


def compute_robust_score_vectorized(p: np.ndarray) -> np.ndarray:
    """Vectorized robust point aggregation for batch scores (M, N)."""
    if p.ndim != 2 or p.shape[1] == 0:
        return np.zeros(p.shape[0] if p.ndim >= 1 else 0, dtype=np.float32)

    mean = np.mean(p, axis=1)
    p25 = np.percentile(p, 25, axis=1)
    std = np.std(p, axis=1)
    out = 0.7 * mean + 0.3 * p25 - 0.15 * std
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def compute_single_continuity_score(matched: np.ndarray, k: int = 5) -> float:
    """Continuity score:
    0.5 * (largest contiguous run / N) + 0.5 * (points in runs >= k / N).
    """
    matched = np.asarray(matched, dtype=bool).ravel()
    N = matched.size
    if N == 0:
        return 0.0
    if not np.any(matched):
        return 0.0
    if np.all(matched):
        return 1.0

    run_lengths = _circular_true_runs(matched)
    if not run_lengths:
        return 0.0

    largest_run = max(run_lengths)
    sum_longer_than_k = sum(rl for rl in run_lengths if rl >= k)

    largest_run_ratio = largest_run / N
    fraction_longer_than_k = sum_longer_than_k / N

    return float(0.5 * largest_run_ratio + 0.5 * fraction_longer_than_k)


class AlignmentScorer:
    """Evaluates CandidateTransformations using multiple image-derived and geometric features."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def _combine_scores(
        self,
        score_dt: float,
        score_cov: float,
        score_cont: float,
        score_grad: float,
        score_area: float,
        score_smooth: float,
        score_shape: float,
        score_neigh: float,
    ) -> Tuple[float, Dict[str, float]]:
        """Combine feature scores into a single gated hybrid score."""
        w = self.config.scoring_weights

        # Boundary hints are already fused into the edge map upstream, so we do not
        # add a separate boundary_hint term here to avoid double-counting evidence.
        w_dt = float(w.get("distance_transform", 0.45))
        w_cov = float(w.get("contour_similarity", 0.20))
        w_cont = float(w.get("contour_continuity", max(0.08, 0.5 * w_cov)))
        w_grad = float(w.get("gradient_agreement", 0.10))

        w_sum_image = w_dt + w_cov + w_cont + w_grad
        if w_sum_image > 0:
            S_image = (
                w_dt * score_dt
                + w_cov * score_cov
                + w_cont * score_cont
                + w_grad * score_grad
            ) / w_sum_image
        else:
            S_image = 0.0

        gate_smooth = 0.5 + 0.5 * score_smooth
        gate_shape = 0.7 + 0.3 * score_shape
        gate_area = 0.7 + 0.3 * score_area
        gate_neighbor = 0.6 + 0.4 * score_neigh

        total_score = S_image * gate_smooth * gate_shape * gate_area * gate_neighbor

        feature_scores = {
            "distance_transform": float(score_dt),
            "boundary_hint": 0.0,
            "contour_similarity": float(score_cov),
            "contour_continuity": float(score_cont),
            "gradient_agreement": float(score_grad),
            "area_consistency": float(score_area),
            "translation_smoothness": float(score_smooth),
            "shape_preservation": float(score_shape),
            "neighbor_consistency": float(score_neigh),
        }
        return float(total_score), feature_scores

    def score_candidate(
        self,
        candidate: CandidateTransformation,
        boundary: BoundaryRepresentation,
        edge_res: EdgeLevelResult,
        transformer: CoordinateTransformer,
        patch_transform: object,
        recorded_area_m2: float | None = None,
        map_area_m2: float | None = None,
        neighbor_shift: Tuple[float, float] | None = None,
        expected_drift: Tuple[float, float] | None = None,
    ) -> ScoredCandidate:
        """Evaluate a single CandidateTransformation."""
        pts = np.asarray(boundary.sampled_points, dtype=np.float32)
        if pts.size == 0:
            return ScoredCandidate(
                candidate=candidate,
                total_score=0.0,
                feature_scores={
                    "distance_transform": 0.0,
                    "boundary_hint": 0.0,
                    "contour_similarity": 0.0,
                    "contour_continuity": 0.0,
                    "gradient_agreement": 0.0,
                    "area_consistency": 0.0,
                    "translation_smoothness": 0.0,
                    "shape_preservation": 1.0,
                    "neighbor_consistency": 1.0,
                },
                metadata={"point_scores": np.zeros(0, dtype=np.float32)},
            )

        # 1) Boundary centroid in the same coordinate system as sampled_points
        cx = float(np.mean(pts[:, 0]))
        cy = float(np.mean(pts[:, 1]))

        # 2) Transform boundary points and normals
        trans_pts = transform_points(
            pts,
            (cx, cy),
            candidate.dx,
            candidate.dy,
            candidate.rotation,
            candidate.scale,
        )
        trans_normals = transform_vectors(np.asarray(boundary.normals, dtype=np.float32), candidate.rotation)

        N = len(trans_pts)

        # 3) Convert transformed CRS coordinates to pixel coordinates in the patch
        inv_transform = ~patch_transform
        a, b, c = inv_transform.a, inv_transform.b, inv_transform.c
        d_val, e, f = inv_transform.d, inv_transform.e, inv_transform.f

        pixel_cols = a * trans_pts[:, 0] + b * trans_pts[:, 1] + c
        pixel_rows = d_val * trans_pts[:, 0] + e * trans_pts[:, 1] + f

        H, W = edge_res.edges.shape
        valid_mask = (
            (pixel_cols >= 0)
            & (pixel_cols < W - 1)
            & (pixel_rows >= 0)
            & (pixel_rows < H - 1)
        )

        # 4) Sample point-wise features
        p_dt = np.zeros(N, dtype=np.float32)
        p_grad = np.zeros(N, dtype=np.float32)
        p_cov = np.zeros(N, dtype=np.float32)

        sigma_dt = max(float(getattr(self.config, "sigma_dt", 2.0)), 1e-6)

        if np.any(valid_mask):
            vc = pixel_cols[valid_mask]
            vr = pixel_rows[valid_mask]

            edt_sampled = scipy.ndimage.map_coordinates(
                edge_res.edt, [vr, vc], order=1, cval=100.0
            )
            p_dt[valid_mask] = np.exp(-edt_sampled / sigma_dt)

            edges_sampled = scipy.ndimage.map_coordinates(
                edge_res.edges, [vr, vc], order=0, cval=0.0
            )
            p_cov[valid_mask] = edges_sampled

            angles_sampled = scipy.ndimage.map_coordinates(
                edge_res.orientation, [vr, vc], order=1, cval=0.0
            )

            mag_map = getattr(edge_res, "magnitude", None)
            if mag_map is not None:
                mag_sampled = scipy.ndimage.map_coordinates(
                    mag_map, [vr, vc], order=1, cval=0.0
                )
            else:
                mag_sampled = np.ones_like(angles_sampled, dtype=np.float32)

            gx = np.cos(angles_sampled)
            gy = np.sin(angles_sampled)
            nx = trans_normals[valid_mask, 0]
            ny = trans_normals[valid_mask, 1]
            p_grad[valid_mask] = np.abs(gx * nx + gy * ny) * mag_sampled

        # 5) Robust aggregation
        score_dt_raw = compute_robust_score(p_dt)
        score_cov_raw = compute_robust_score(p_cov)
        score_grad = compute_robust_score(p_grad)
        score_cont = compute_single_continuity_score(p_cov > 0.5, k=5)

        # 6) Edge density normalization (de-biasing)
        edge_density = float(np.mean(edge_res.edges))
        mean_dt_patch = float(np.mean(np.exp(-edge_res.edt / sigma_dt)))

        denom_dt = max(1.0 - mean_dt_patch, 0.1)
        score_dt = float(np.clip((score_dt_raw - mean_dt_patch) / denom_dt, 0.0, 1.0))

        denom_cov = max(1.0 - edge_density, 0.1)
        score_cov = float(np.clip((score_cov_raw - edge_density) / denom_cov, 0.0, 1.0))

        # 7) Geometric priors / penalties
        score_area = 1.0
        if recorded_area_m2 is not None and map_area_m2 is not None and recorded_area_m2 > 0:
            area_ratio = map_area_m2 / recorded_area_m2
            lambda_area = getattr(self.config, "lambda_area", 1.0)
            score_area = float(np.exp(-lambda_area * abs(np.log(area_ratio))))

        ex_dx, ex_dy = expected_drift if expected_drift is not None else (0.0, 0.0)
        shift_dist = np.hypot(candidate.dx - ex_dx, candidate.dy - ex_dy)
        lambda_smooth = max(float(getattr(self.config, "lambda_smooth", 0.04)), 1e-6)
        
        area_m2 = map_area_m2 if map_area_m2 is not None else (recorded_area_m2 if recorded_area_m2 is not None else 1000.0)
        d_eq = 2.0 * np.sqrt(area_m2 / np.pi)
        d_eq = max(float(d_eq), 15.0)
        
        score_smooth = float(np.exp(-lambda_smooth * shift_dist - 1.0 * (shift_dist / d_eq)**2))

        score_shape = 1.0
        if abs(candidate.rotation) > 1e-5 or abs(candidate.scale - 1.0) > 1e-5:
            lambda_rot = getattr(self.config, "lambda_rot", 0.05)
            lambda_scale = getattr(self.config, "lambda_scale", 2.0)
            score_shape = float(
                np.exp(
                    -lambda_rot * abs(candidate.rotation)
                    - lambda_scale * abs(np.log(max(candidate.scale, 1e-6)))
                )
            )

        score_neigh = 1.0
        if neighbor_shift is not None:
            ndx, ndy = neighbor_shift
            neigh_dist = np.hypot(candidate.dx - ndx, candidate.dy - ndy)
            sigma_neigh = max(float(getattr(self.config, "neighbor_consistency_sigma_m", 3.0)), 1e-6)
            score_neigh = float(np.exp(-0.5 * (neigh_dist / sigma_neigh) ** 2))

        total_score, feature_scores = self._combine_scores(
            score_dt=score_dt,
            score_cov=score_cov,
            score_cont=score_cont,
            score_grad=score_grad,
            score_area=score_area,
            score_smooth=score_smooth,
            score_shape=score_shape,
            score_neigh=score_neigh,
        )

        scored = ScoredCandidate(
            candidate=candidate,
            total_score=float(total_score),
            feature_scores=feature_scores,
            metadata={"point_scores": p_dt},
        )

        if self.config.debug_visualize:
            save_score_alignment_debug(
                boundary.plot_number,
                boundary,
                edge_res,
                candidate,
                transformer,
                patch_transform,
                p_dt,
                self.config.debug_out_dir,
            )

        return scored

    def score_candidates(
        self,
        candidates: List[CandidateTransformation],
        boundary: BoundaryRepresentation,
        edge_res: EdgeLevelResult,
        transformer: CoordinateTransformer,
        patch_transform: object,
        recorded_area_m2: float | None = None,
        map_area_m2: float | None = None,
        neighbor_shift: Tuple[float, float] | None = None,
        expected_drift: Tuple[float, float] | None = None,
    ) -> List[ScoredCandidate]:
        """Score a batch of candidates against imagery evidence in a single vectorized numpy operation."""
        if not candidates:
            return []

        has_rotation_or_scale = any(
            abs(c.rotation) > 1e-5 or abs(c.scale - 1.0) > 1e-5
            for c in candidates
        )
        if has_rotation_or_scale:
            scored_candidates = [
                self.score_candidate(
                    c,
                    boundary,
                    edge_res,
                    transformer,
                    patch_transform,
                    recorded_area_m2,
                    map_area_m2,
                    neighbor_shift,
                    expected_drift,
                )
                for c in candidates
            ]
        else:
            pts = np.asarray(boundary.sampled_points, dtype=np.float32)
            N = len(pts)
            M = len(candidates)

            dxs = np.array([c.dx for c in candidates], dtype=np.float32)
            dys = np.array([c.dy for c in candidates], dtype=np.float32)

            trans_pts_x = pts[:, 0] + dxs[:, np.newaxis]
            trans_pts_y = pts[:, 1] + dys[:, np.newaxis]

            inv_transform = ~patch_transform
            a, b, c_val = inv_transform.a, inv_transform.b, inv_transform.c
            d_val, e, f = inv_transform.d, inv_transform.e, inv_transform.f

            pixel_cols = a * trans_pts_x + b * trans_pts_y + c_val
            pixel_rows = d_val * trans_pts_x + e * trans_pts_y + f

            H, W = edge_res.edges.shape
            valid_mask = (
                (pixel_cols >= 0)
                & (pixel_cols < W - 1)
                & (pixel_rows >= 0)
                & (pixel_rows < H - 1)
            )

            flat_rows = pixel_rows.ravel()
            flat_cols = pixel_cols.ravel()
            coords = np.stack([flat_rows, flat_cols], axis=0)

            sigma_dt = max(float(getattr(self.config, "sigma_dt", 2.0)), 1e-6)

            use_gpu = False # Forced to CPU to allow Windows multiprocessing

            if use_gpu:
                # ==========================================
                # PyTorch GPU Accelerated Path
                # ==========================================
                device = torch.device('cuda')
                
                # Stack image channels: [edt, edges, orientation, magnitude]
                mag_map = getattr(edge_res, "magnitude", None)
                if mag_map is None:
                    mag_map = np.ones_like(edge_res.orientation, dtype=np.float32)
                    
                stacked_imgs = np.stack([
                    edge_res.edt.astype(np.float32),
                    edge_res.edges.astype(np.float32),
                    edge_res.orientation.astype(np.float32),
                    mag_map.astype(np.float32)
                ], axis=0) # shape (4, H, W)
                
                img_tensor = torch.from_numpy(stacked_imgs).unsqueeze(0).float().to(device) # (1, 4, H, W)
                
                # Prepare grid
                # pixel_cols and pixel_rows are (M, N)
                # PyTorch grid sample requires coordinates in [-1, 1]
                x_norm = (pixel_cols / (W - 1)) * 2.0 - 1.0
                y_norm = (pixel_rows / (H - 1)) * 2.0 - 1.0
                
                grid_np = np.stack([x_norm, y_norm], axis=-1) # (M, N, 2)
                grid_tensor = torch.from_numpy(grid_np).unsqueeze(0).float().to(device) # (1, M, N, 2)
                
                # Sample bilinear channels (edt, orientation, mag)
                sampled_bilinear = F.grid_sample(
                    img_tensor[:, [0, 2, 3], :, :], 
                    grid_tensor, 
                    mode='bilinear', 
                    padding_mode='zeros', 
                    align_corners=True
                ).squeeze(0) # (3, M, N)
                
                # Sample nearest channels (edges)
                sampled_nearest = F.grid_sample(
                    img_tensor[:, [1], :, :], 
                    grid_tensor, 
                    mode='nearest', 
                    padding_mode='zeros', 
                    align_corners=True
                ).squeeze(0) # (1, M, N)
                
                # Unpack and move back to CPU
                edt_sampled = sampled_bilinear[0].cpu().numpy()
                angles_sampled = sampled_bilinear[1].cpu().numpy()
                mag_sampled = sampled_bilinear[2].cpu().numpy()
                edges_sampled = sampled_nearest[0].cpu().numpy()
                
            else:
                # ==========================================
                # CPU Numpy Path (Fallback)
                # ==========================================
                edt_sampled = scipy.ndimage.map_coordinates(
                    edge_res.edt, coords, order=1, cval=100.0
                ).reshape((M, N))
                
                edges_sampled = scipy.ndimage.map_coordinates(
                    edge_res.edges, coords, order=0, cval=0.0
                ).reshape((M, N))
                
                angles_sampled = scipy.ndimage.map_coordinates(
                    edge_res.orientation, coords, order=1, cval=0.0
                ).reshape((M, N))
                
                mag_map = getattr(edge_res, "magnitude", None)
                if mag_map is not None:
                    mag_sampled = scipy.ndimage.map_coordinates(
                        mag_map, coords, order=1, cval=0.0
                    ).reshape((M, N))
                else:
                    mag_sampled = np.ones((M, N), dtype=np.float32)

            p_dt = np.exp(-edt_sampled / sigma_dt)
            gx = np.cos(angles_sampled)
            gy = np.sin(angles_sampled)
            nx = np.asarray(boundary.normals, dtype=np.float32)[:, 0]
            ny = np.asarray(boundary.normals, dtype=np.float32)[:, 1]
            p_grad = np.abs(gx * nx + gy * ny) * mag_sampled

            p_dt[~valid_mask] = 0.0
            edges_sampled[~valid_mask] = 0.0
            p_grad[~valid_mask] = 0.0

            score_dt_raw = compute_robust_score_vectorized(p_dt)
            score_cov_raw = compute_robust_score_vectorized(edges_sampled)
            score_grad_arr = compute_robust_score_vectorized(p_grad)

            score_cont_arr = np.zeros(M, dtype=np.float32)
            for idx_c in range(M):
                score_cont_arr[idx_c] = compute_single_continuity_score(edges_sampled[idx_c] > 0.5, k=5)

            edge_density = float(np.mean(edge_res.edges))
            mean_dt_patch = float(np.mean(np.exp(-edge_res.edt / sigma_dt)))

            denom_dt = max(1.0 - mean_dt_patch, 0.1)
            score_dt_arr = np.clip((score_dt_raw - mean_dt_patch) / denom_dt, 0.0, 1.0).astype(np.float32)

            denom_cov = max(1.0 - edge_density, 0.1)
            score_cov_arr = np.clip((score_cov_raw - edge_density) / denom_cov, 0.0, 1.0).astype(np.float32)

            score_area = 1.0
            if recorded_area_m2 is not None and map_area_m2 is not None and recorded_area_m2 > 0:
                area_ratio = map_area_m2 / recorded_area_m2
                lambda_area = getattr(self.config, "lambda_area", 1.0)
                score_area = float(np.exp(-lambda_area * abs(np.log(area_ratio))))

            score_shape = 1.0

            score_neigh_arr = np.ones(M, dtype=np.float32)
            if neighbor_shift is not None:
                ndx, ndy = neighbor_shift
                neigh_dists = np.hypot(dxs - ndx, dys - ndy)
                sigma_neigh = max(float(getattr(self.config, "neighbor_consistency_sigma_m", 3.0)), 1e-6)
                score_neigh_arr = np.exp(-0.5 * (neigh_dists / sigma_neigh) ** 2).astype(np.float32)

            ex_dx, ex_dy = expected_drift if expected_drift is not None else (0.0, 0.0)
            shift_dists = np.hypot(dxs - ex_dx, dys - ex_dy)
            lambda_smooth = max(float(getattr(self.config, "lambda_smooth", 0.04)), 1e-6)
            
            area_m2 = map_area_m2 if map_area_m2 is not None else (recorded_area_m2 if recorded_area_m2 is not None else 1000.0)
            d_eq = 2.0 * np.sqrt(area_m2 / np.pi)
            d_eq = max(float(d_eq), 15.0)
            
            score_smooth_arr = np.exp(-lambda_smooth * shift_dists - 1.0 * (shift_dists / d_eq)**2).astype(np.float32)

            # Combine image evidence (boundary hint intentionally omitted to avoid duplication).
            w = self.config.scoring_weights
            w_dt = float(w.get("distance_transform", 0.45))
            w_cov = float(w.get("contour_similarity", 0.20))
            w_cont = float(w.get("contour_continuity", max(0.08, 0.5 * w_cov)))
            w_grad = float(w.get("gradient_agreement", 0.10))
            w_sum_image = w_dt + w_cov + w_cont + w_grad

            if w_sum_image > 0:
                S_image_arr = (
                    w_dt * score_dt_arr
                    + w_cov * score_cov_arr
                    + w_cont * score_cont_arr
                    + w_grad * score_grad_arr
                ) / w_sum_image
            else:
                S_image_arr = np.zeros(M, dtype=np.float32)

            gate_smooth_arr = 0.5 + 0.5 * score_smooth_arr
            gate_shape_arr = 0.7 + 0.3 * score_shape
            gate_area_arr = 0.7 + 0.3 * score_area
            gate_neighbor_arr = 0.6 + 0.4 * score_neigh_arr

            total_scores_arr = (
                S_image_arr
                * gate_smooth_arr
                * gate_shape_arr
                * gate_area_arr
                * gate_neighbor_arr
            )

            scored_candidates = []
            for i, cand in enumerate(candidates):
                feature_scores = {
                    "distance_transform": float(score_dt_arr[i]),
                    "boundary_hint": 0.0,
                    "contour_similarity": float(score_cov_arr[i]),
                    "contour_continuity": float(score_cont_arr[i]),
                    "gradient_agreement": float(score_grad_arr[i]),
                    "area_consistency": float(score_area),
                    "translation_smoothness": float(score_smooth_arr[i]),
                    "shape_preservation": float(score_shape),
                    "neighbor_consistency": float(score_neigh_arr[i]),
                }

                scored = ScoredCandidate(
                    candidate=cand,
                    total_score=float(total_scores_arr[i]),
                    feature_scores=feature_scores,
                    metadata={"point_scores": p_dt[i]},
                )

                if self.config.debug_visualize:
                    save_score_alignment_debug(
                        boundary.plot_number,
                        boundary,
                        edge_res,
                        cand,
                        transformer,
                        patch_transform,
                        p_dt[i],
                        self.config.debug_out_dir,
                    )

                scored_candidates.append(scored)

        # ------------------------------------------------------------------
        # Spatial ambiguity and peak sharpness metadata
        # ------------------------------------------------------------------
        M = len(candidates)
        if M > 1:
            total_scores_arr = np.array([sc.total_score for sc in scored_candidates], dtype=np.float32)
            coords_m = np.array([[c.dx, c.dy] for c in candidates], dtype=np.float32)

            second_best_scores = np.zeros(M, dtype=np.float32)

            if M <= 6000:
                dists = np.hypot(
                    coords_m[:, np.newaxis, 0] - coords_m[np.newaxis, :, 0],
                    coords_m[:, np.newaxis, 1] - coords_m[np.newaxis, :, 1],
                )

                rival_mask = dists >= 3.0
                masked_scores = np.where(rival_mask, total_scores_arr[np.newaxis, :], -np.inf)
                second_best_scores = np.max(masked_scores, axis=1)
                second_best_scores[second_best_scores == -np.inf] = 0.0

                # Estimate local step size from nearest non-zero neighbor distances.
                nearest = np.min(np.where(dists > 0.01, dists, np.inf), axis=1)
                finite_nearest = nearest[np.isfinite(nearest)]
                step_size = float(np.median(finite_nearest)) if finite_nearest.size > 0 else 1.0
                if not np.isfinite(step_size) or step_size <= 0:
                    step_size = 1.0

                neighbor_mask = (dists > 0.01) & (dists <= 1.5 * step_size)
                masked_neighbor_scores = np.where(neighbor_mask, total_scores_arr[np.newaxis, :], -np.inf)

                for idx_c in range(M):
                    row_scores = masked_neighbor_scores[idx_c]
                    valid_neighbor_scores = row_scores[row_scores != -np.inf]
                    sharpness = (
                        total_scores_arr[idx_c] - float(np.mean(valid_neighbor_scores))
                        if valid_neighbor_scores.size > 0
                        else 0.0
                    )
                    scored_candidates[idx_c].metadata["peak_sharpness"] = float(sharpness)
            else:
                # Fallback for very large grids
                for idx_c in range(M):
                    dists = np.hypot(
                        coords_m[idx_c, 0] - coords_m[:, 0],
                        coords_m[idx_c, 1] - coords_m[:, 1],
                    )

                    non_zero_dists = dists[dists > 0.01]
                    step_size = float(np.min(non_zero_dists)) if non_zero_dists.size > 0 else 1.0
                    if not np.isfinite(step_size) or step_size <= 0:
                        step_size = 1.0

                    rival_scores = total_scores_arr[dists >= 3.0]
                    second_best_scores[idx_c] = float(np.max(rival_scores)) if rival_scores.size > 0 else 0.0

                    neighbor_scores = total_scores_arr[(dists > 0.01) & (dists <= 1.5 * step_size)]
                    sharpness = (
                        total_scores_arr[idx_c] - float(np.mean(neighbor_scores))
                        if neighbor_scores.size > 0
                        else 0.0
                    )
                    scored_candidates[idx_c].metadata["peak_sharpness"] = float(sharpness)

            for idx_c, sc in enumerate(scored_candidates):
                sc.metadata["best_score"] = float(total_scores_arr[idx_c])
                sc.metadata["second_score"] = float(second_best_scores[idx_c])
                sc.metadata["score_gap"] = float(total_scores_arr[idx_c] - second_best_scores[idx_c])
                if total_scores_arr[idx_c] > 1e-9:
                    sc.metadata["ambiguity_index"] = float(second_best_scores[idx_c] / total_scores_arr[idx_c])
                else:
                    sc.metadata["ambiguity_index"] = 1.0

                sc.candidate.metadata["best_score"] = sc.metadata["best_score"]
                sc.candidate.metadata["second_score"] = sc.metadata["second_score"]
                sc.candidate.metadata["score_gap"] = sc.metadata["score_gap"]
                sc.candidate.metadata["ambiguity_index"] = sc.metadata["ambiguity_index"]
                if "peak_sharpness" in sc.metadata:
                    sc.candidate.metadata["peak_sharpness"] = sc.metadata["peak_sharpness"]
        else:
            for sc in scored_candidates:
                sc.metadata["best_score"] = float(sc.total_score)
                sc.metadata["second_score"] = 0.0
                sc.metadata["score_gap"] = float(sc.total_score)
                sc.metadata["ambiguity_index"] = 0.0
                sc.metadata["peak_sharpness"] = 0.0

                sc.candidate.metadata["best_score"] = sc.metadata["best_score"]
                sc.candidate.metadata["second_score"] = sc.metadata["second_score"]
                sc.candidate.metadata["score_gap"] = sc.metadata["score_gap"]
                sc.candidate.metadata["ambiguity_index"] = sc.metadata["ambiguity_index"]
                sc.candidate.metadata["peak_sharpness"] = sc.metadata["peak_sharpness"]

        return scored_candidates