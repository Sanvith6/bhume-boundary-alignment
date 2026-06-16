"""Loader module for the cadastral boundary correction pipeline.

This module provides functions to load the village data, perform Coordinate
Reference System (CRS) transformations, and construct the topological
neighbor graph from plot geometries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from bhume.io import Village, load


def load_village(village_dir: str | Path) -> Village:
    """Wrapper to load a village bundle using the starter kit's standard loader.

    Args:
        village_dir: Path to the directory containing village inputs.

    Returns:
        A loaded Village dataclass instance.
    """
    return load(village_dir)


class CoordinateTransformer:
    """Handles coordinate transformations between EPSG:4326 (WGS84 lon/lat),

    EPSG:3857 (web-mercator meters), and pixel coordinates for a given raster.
    """

    def __init__(self, src_raster) -> None:
        """Initialize transformer using an opened rasterio source.

        Args:
            src_raster: An open rasterio dataset reader.
        """
        self.crs = src_raster.crs
        self.transform = src_raster.transform
        self.inv_transform = ~self.transform

        # pyproj transformers (always_xy=True ensures lon,lat/x,y order)
        self.transformer_to_3857 = Transformer.from_crs(
            "EPSG:4326", self.crs, always_xy=True
        )
        self.transformer_from_3857 = Transformer.from_crs(
            self.crs, "EPSG:4326", always_xy=True
        )

    def lonlat_to_crs(self, lon: float, lat: float) -> Tuple[float, float]:
        """Convert a lon/lat point to CRS coordinates (meters)."""
        x, y = self.transformer_to_3857.transform(lon, lat)
        return float(x), float(y)

    def crs_to_lonlat(self, x: float, y: float) -> Tuple[float, float]:
        """Convert CRS coordinates (meters) to lon/lat."""
        lon, lat = self.transformer_from_3857.transform(x, y)
        return float(lon), float(lat)

    def geom_to_crs(self, geom_4326: BaseGeometry) -> BaseGeometry:
        """Reproject a lon/lat geometry into the raster CRS (EPSG:3857)."""
        return shp_transform(
            lambda x, y, z=None: self.transformer_to_3857.transform(x, y),
            geom_4326,
        )

    def geom_to_lonlat(self, geom_crs: BaseGeometry) -> BaseGeometry:
        """Reproject a raster CRS geometry back to EPSG:4326 (lon/lat)."""
        return shp_transform(
            lambda x, y, z=None: self.transformer_from_3857.transform(x, y),
            geom_crs,
        )

    def crs_to_pixel(self, x: float, y: float) -> Tuple[float, float]:
        """Map CRS coordinates to fractional pixel coordinates (col, row)."""
        col, row = self.inv_transform * (x, y)
        return float(col), float(row)

    def pixel_to_crs(self, col: float, row: float) -> Tuple[float, float]:
        """Map fractional pixel coordinates (col, row) to CRS coordinates."""
        x, y = self.transform * (col, row)
        return float(x), float(y)


def build_neighbor_graph(
    plots: gpd.GeoDataFrame, target_crs: str = "EPSG:3857"
) -> Dict[str, Dict[str, float]]:
    """Construct a topological neighborhood graph from plot geometries,

    measuring shared boundary lengths in meters.

    Args:
        plots: GeoDataFrame of official plot polygons (typically indexed by
          plot_number).
        target_crs: A meter-based CRS projection to compute physical lengths.

    Returns:
        A nested dictionary structure:
        {plot_number: {neighbor_plot_number: shared_boundary_length_in_meters}}
    """
    # Project to target_crs if needed
    if plots.crs is None:
        plots_projected = plots.set_crs("EPSG:4326").to_crs(target_crs)
    else:
        plots_projected = plots.to_crs(target_crs)

    graph: Dict[str, Dict[str, float]] = {}
    plot_numbers = plots_projected.index.astype(str).tolist()
    geoms = plots_projected.geometry.values
    sindex = plots_projected.sindex

    for idx_i, pn_i in enumerate(plot_numbers):
        geom_i = geoms[idx_i]
        if geom_i is None or geom_i.is_empty:
            graph[pn_i] = {}
            continue

        neighbors_i: Dict[str, float] = {}

        # Query spatial index for geometries intersecting bounding box of geom_i
        candidate_indices = sindex.query(geom_i)

        for idx_j in candidate_indices:
            if idx_i == idx_j:
                continue

            pn_j = plot_numbers[idx_j]
            geom_j = geoms[idx_j]

            if geom_j is None or geom_j.is_empty:
                continue

            # Check actual spatial intersection
            if not geom_i.intersects(geom_j):
                continue

            # Extract intersection geometry
            inter = geom_i.intersection(geom_j)

            # If they touch along a boundary line
            if inter.geom_type in ("LineString", "MultiLineString"):
                length = inter.length
                if length > 0.01:  # at least 1cm of shared boundary
                    neighbors_i[pn_j] = float(length)
            elif inter.geom_type in (
                "GeometryCollection",
                "Polygon",
                "MultiPolygon",
            ):
                # Fallback in case of overlaps: intersect their boundaries
                boundary_inter = geom_i.boundary.intersection(geom_j.boundary)
                length = boundary_inter.length
                if length > 0.01:
                    neighbors_i[pn_j] = float(length)

        graph[pn_i] = neighbors_i

    return graph
