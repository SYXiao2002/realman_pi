"""Separate latest feedback freshness from the duration of stationary history."""

import numpy as np


def settled_joints(samples, *, now, now_ns, freshness_sec, settle_sec, max_speed):
    if len(samples) < 2:
        raise ValueError("fewer than two valid six-joint samples")
    latest = samples[-1]
    age = max(now - latest[0], (now_ns - latest[3]) * 1e-9)
    if age > freshness_sec or latest[3] > now_ns + 10_000_000:
        raise ValueError(f"latest joint feedback age {age:.3f} s (limit {freshness_sec:.3f} s)")

    # Include one sample just before the start of the settle interval. Filtering
    # every sample by freshness would make settle_sec > freshness_sec impossible.
    start_ns = latest[3] - round(settle_sec * 1e9)
    first = 0
    for index, sample in enumerate(samples):
        if sample[3] <= start_ns:
            first = index
        else:
            break
    window = samples[first:]
    dt = np.diff(np.asarray([sample[3] - window[0][3] for sample in window], dtype=float)) * 1e-9
    span = (latest[3] - window[0][3]) * 1e-9
    if span + 1e-9 < settle_sec:
        raise ValueError(f"stationary history spans {span:.3f} s (need {settle_sec:.3f} s)")
    if not len(dt) or np.any(dt <= 0) or np.any(dt > freshness_sec):
        raise ValueError("joint feedback has duplicate/out-of-order stamps or a feedback gap")
    positions = np.asarray([sample[1] for sample in window])
    speed = float(np.max(np.abs(np.diff(positions, axis=0) / dt[:, None])))
    reported = max((float(np.max(np.abs(sample[2]))) for sample in window if sample[2] is not None), default=0.)
    if max(speed, reported) > max_speed:
        raise ValueError(f"joint speed {max(speed, reported):.5f} rad/s (limit {max_speed:.5f} rad/s)")
    return latest[1].copy()
