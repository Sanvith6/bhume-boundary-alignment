import numpy as np
import scipy.ndimage
import torch
import torch.nn.functional as F

M = 5
N = 100
H, W = 200, 200

edges = np.random.rand(H, W)
edt = np.random.rand(H, W) * 10

# FORCE IN-BOUNDS COORDINATES ONLY
pixel_cols = np.random.rand(M, N) * (W - 2) + 1
pixel_rows = np.random.rand(M, N) * (H - 2) + 1

# CPU Path
flat_rows = pixel_rows.ravel()
flat_cols = pixel_cols.ravel()
coords = np.stack([flat_rows, flat_cols], axis=0)

edt_sampled_cpu = scipy.ndimage.map_coordinates(
    edt, coords, order=1, cval=100.0
).reshape((M, N))

# GPU Path
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
stacked_imgs = np.stack([edt.astype(np.float32)], axis=0)
img_tensor = torch.from_numpy(stacked_imgs).unsqueeze(0).to(device)

x_norm = (pixel_cols / (W - 1)) * 2.0 - 1.0
y_norm = (pixel_rows / (H - 1)) * 2.0 - 1.0
grid_np = np.stack([x_norm, y_norm], axis=-1).astype(np.float32)
grid_tensor = torch.from_numpy(grid_np).unsqueeze(0).to(device)

sampled_bilinear = F.grid_sample(
    img_tensor, 
    grid_tensor, 
    mode='bilinear', 
    padding_mode='zeros', 
    align_corners=True
).squeeze(0)

edt_sampled_gpu = sampled_bilinear[0].cpu().numpy()

# Compare
print("EDT diff max (in-bounds):", np.max(np.abs(edt_sampled_cpu - edt_sampled_gpu)))
print("Are they identical?", np.allclose(edt_sampled_cpu, edt_sampled_gpu, atol=1e-5))

# Also test nearest
edges_sampled_cpu = scipy.ndimage.map_coordinates(
    edges, coords, order=0, cval=0.0
).reshape((M, N))

sampled_nearest = F.grid_sample(
    torch.from_numpy(edges).unsqueeze(0).unsqueeze(0).float().to(device), 
    grid_tensor, 
    mode='nearest', 
    padding_mode='zeros', 
    align_corners=True
).squeeze(0)
edges_sampled_gpu = sampled_nearest[0].cpu().numpy()

print("Edges diff max (in-bounds):", np.max(np.abs(edges_sampled_cpu - edges_sampled_gpu)))
