"""Synthetic metric geometry checks independent of neural models or ROS."""

import copy

import numpy as np

from test_grasp_protocol import observation

from grasp_protocol import validate_candidate
from grasp_server.backends import adapt_cgn_candidate
from grasp_server.geometry import geometric_candidates


def candidates(request):
    return geometric_candidates(request, request["depth_m"] > 0, object_id="1", class_name="adapter",
                                policy={"min_object_points": 20})


def test_contact_center_and_axes_have_no_robot_wrist_offset():
    request = observation("geometry")
    values, diagnostics = candidates(request)
    assert values and diagnostics["reason"] == "ok"
    for candidate in values:
        validate_candidate(candidate, request)
        T = candidate["T_camera_grasp"]
        assert abs(T[2, 3] - 0.5) < 1e-8
        assert np.allclose(T[:3, 2], [0, 0, 1])
        assert 0.02 < candidate["width_m"] < 0.06


def test_arm_names_do_not_change_algorithm_candidates():
    right, left = observation("geometry"), observation("geometry")
    right["arm_id"], left["arm_id"] = "r", "l"
    r, _ = candidates(right)
    l, _ = candidates(left)
    assert len(r) == len(l)
    for a, b in zip(r, l):
        assert np.array_equal(a["T_camera_grasp"], b["T_camera_grasp"])


def test_rejects_unverified_wrist_view_and_impossible_opening():
    request = observation("geometry")
    request["up_camera"] = [1, 0, 0]
    values, diagnostic = candidates(request)
    assert values == [] and diagnostic["reason"] == "unsupported_view"
    request = observation("geometry")
    request["gripper"]["max_opening_m"] = 0.015
    values, _ = candidates(request)
    assert values == []


def test_metric_width_is_independent_of_pixel_resolution():
    request = observation("geometry")
    values, _ = candidates(request)
    zoom = copy.deepcopy(request)
    zoom["depth_m"] = np.repeat(np.repeat(request["depth_m"], 2, 0), 2, 1)
    zoom["color_bgr"] = np.repeat(np.repeat(request["color_bgr"], 2, 0), 2, 1)
    zoom["K"][:2] *= 2
    higher, _ = candidates(zoom)
    assert values and higher
    assert abs(values[0]["width_m"] - higher[0]["width_m"]) < 0.001


def test_tilted_camera_keeps_closing_axis_and_width_in_support_plane():
    request = observation("geometry")
    angle = np.deg2rad(20)
    up = np.array([np.sin(angle), 0., -np.cos(angle)])
    # Intersect calibrated camera rays with a physical rectangle in a tilted
    # plane, rather than treating camera XY distances as object dimensions.
    y, x = np.indices((96, 128))
    K = np.array([[400., 0., 64.], [0., 400., 48.], [0., 0., 1.]])
    rays = np.stack(((x-64.)/400., (y-48.)/400., np.ones_like(x)), axis=-1)
    center = np.array([0., 0., 0.5])
    scale = np.dot(up, center)/(rays @ up)
    points = rays * scale[..., None]
    along = np.array([np.cos(angle), 0., np.sin(angle)])
    mask = (np.abs(points[..., 1]) <= .015) & (np.abs((points-center) @ along) <= .030)
    request.update(color_bgr=np.zeros((96, 128, 3), dtype=np.uint8),
                   depth_m=np.where(mask, scale, 0).astype(np.float32), K=K, up_camera=up)
    values, diagnostics = candidates(request)
    assert values and diagnostics["reason"] == "ok"
    best = values[0]
    assert abs(best["width_m"]-.030) < .002
    assert np.allclose(best["T_camera_grasp"][:3, 2], -up)
    assert abs(np.dot(best["T_camera_grasp"][:3, 0], up)) < 1e-8
    assert abs(np.dot(best["T_camera_grasp"][:3, 3]-center, up)) < 1e-6


def test_cgn_native_origin_is_adapted_without_tcp_compensation():
    native = np.eye(4)
    native[:3, 3] = [0.02, -0.03, 0.5 - 0.1034]
    common = adapt_cgn_candidate(native, hand_depth_m=0.1034)
    assert np.allclose(common[:3, 3], [0.02, -0.03, 0.5])
    assert np.allclose(native[:3, 3], [0.02, -0.03, 0.3966])
