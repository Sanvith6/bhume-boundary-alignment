import sys
import numpy as np
from bhume import load, patch_for_plot
from bhume.geo import open_imagery, geom_to_imagery_crs

def main():
    village = load('data/village_Malatavadi')
    pn = village.plots.index[0]
    geom = village.plot(pn)
    
    with open_imagery(village.imagery_path) as src:
        patch = patch_for_plot(src, geom, pad_m=30)
        geom_crs = geom_to_imagery_crs(src, geom)
        
    print("Patch image shape:", patch.image.shape)
    print("Patch bounds:", patch.bounds)
    print("Patch transform:", patch.transform)
    
    # Let's test the inverse transform
    inv_transform = ~patch.transform
    print("Inverse transform:", inv_transform)
    
    # Get exterior coords
    if geom_crs.geom_type == 'Polygon':
        ext_coords = list(geom_crs.exterior.coords)
    else:
        ext_coords = []
        for poly in geom_crs.geoms:
            ext_coords.extend(list(poly.exterior.coords))
    print("Exterior coords (CRS):", ext_coords[:3])
    
    # Map to pixel coords
    pixel_coords = [inv_transform * pt for pt in ext_coords]
    print("Pixel coords:", pixel_coords[:3])

if __name__ == '__main__':
    main()
