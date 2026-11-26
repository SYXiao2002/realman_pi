# Suction contact depth regression inputs

`suction_contact_depth_offsets.npz` contains three cropped, aligned depth/mask
observations and the corresponding camera-frame contact poses from the
2026-11-26 `eu_plug_1124` captures. Intrinsics are translated into each crop.
No RGB, robot connection or GPU is required to replay them.

- `contact_front`: selected object's intended contact surface is 2–4 mm in
  front of the fitted mesh contact plane. It must remain clear.
- `contact_top`: another intended target contact surface with a 2–2.6 mm offset.
  It must remain clear.
- `neighbor_obstacle`: another object occupies the outward suction corridor.
  It must remain a collision.

Each prefix has `_depth_m`, `_mask`, `_K`, `_contact` and `_radius_m` arrays.
The crops originally reproduced false collisions in a 0–40 mm corridor with
a 2 mm target contact tolerance. The current detector checks 10–40 mm only
and does not read the mask or contact-region radius. Tests replay the same
depth with absent, original, all-zero and all-one masks and require identical
results, including retaining the real neighbor obstacle.
