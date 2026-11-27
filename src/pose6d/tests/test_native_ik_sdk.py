"""Native RealMan SDK IK binding tests without a controller or installed SDK."""

import ctypes
from types import SimpleNamespace

import numpy as np
import pytest

from pose6d.geometry import configured_transform, matrix_to_xyzrpy
from pose6d_bridge.native_ik import NativeIkSdkPort, tcp_targets


class FakeFrame(ctypes.Structure):
    _fields_ = [("value", ctypes.c_int)]

    def __init__(self, **_kwargs):
        super().__init__(0)


class FakeParams:
    def __init__(self, q_in, q_pose, flag):
        self.q_in = list(q_in)
        self.q_pose = list(q_pose)
        self.flag = flag


class FakeSdkApi:
    rm_robot_arm_model_e = SimpleNamespace(RM_MODEL_RM_65_E=65)
    rm_force_type_e = SimpleNamespace(RM_MODEL_RM_B_E=0)
    rm_frame_t = FakeFrame
    rm_inverse_kinematics_params_t = FakeParams

    def __init__(self, result=0, num=8):
        self.result = result
        self.num = num
        self.initialized = []
        self.installation_angles = []
        self.workframes = []
        self.toolframes = []
        self.calls = []

    def rm_algo_init_sys_data(self, model, force):
        self.initialized.append((model, force))

    def rm_algo_set_angle(self, *angles):
        self.installation_angles.append(angles)

    def rm_algo_set_workframe(self, frame):
        self.workframes.append(frame)

    def rm_algo_set_toolframe(self, frame):
        self.toolframes.append(frame)

    def rm_algo_inverse_kinematics_all(self, handle, params):
        self.calls.append((handle, params))
        return SimpleNamespace(result=self.result, num=self.num)


def test_native_full_solve_uses_base_flange_pose_and_capture_joint_seed():
    sdk = FakeSdkApi(num=6)
    port = NativeIkSdkPort(sdk_api=sdk)
    base_camera = configured_transform({"xyz_m": [.2, -.1, .3], "rpy_rad": [.1, .2, -.4]})
    camera_tcp = configured_transform({"xyz_m": [.03, .04, .5], "rpy_rad": [-.2, .3, .7]})
    flange_tcp = configured_transform({"xyz_m": [0, 0, .14], "rpy_rad": [0, .1, 0]})
    seed_degrees = [10, -20, 30, -40, 50, -60]

    result = port.check(base_camera, camera_tcp, flange_tcp, seed_degrees)

    assert sdk.initialized == [(65, 0)]
    assert sdk.installation_angles == [(0.0, 0.0, 0.0)]
    assert len(sdk.workframes) == len(sdk.toolframes) == 1
    assert result == {"checked": True, "reachable": True,
                      "valid_solution_count": 6, "reason": None}
    handle, params = sdk.calls[0]
    assert handle is None  # Strictly offline SDK call; no robot handle is created.
    assert params.q_in == seed_degrees + [0.0]
    assert params.flag == 1  # SDK Euler-angle pose input.
    target_flange = base_camera @ camera_tcp @ np.linalg.inv(flange_tcp)
    np.testing.assert_allclose(params.q_pose, matrix_to_xyzrpy(target_flange), atol=1e-6)


@pytest.mark.parametrize(
    ("status", "num", "expected"),
    [
        (0, 0, {"checked": True, "reachable": False, "valid_solution_count": 0,
                "reason": "SDK_NO_SOLUTION"}),
        (1, 0, {"checked": True, "reachable": False, "valid_solution_count": 0,
                "reason": "SDK_NO_SOLUTION"}),
        (-1, 0, {"checked": False, "reachable": None, "valid_solution_count": None,
                 "reason": "SDK_INVALID_REFERENCE_JOINTS"}),
        (-2, 0, {"checked": False, "reachable": None, "valid_solution_count": None,
                 "reason": "SDK_INVALID_ORIENTATION"}),
    ],
)
def test_sdk_result_codes_keep_no_solution_distinct_from_unknown(status, num, expected):
    port = NativeIkSdkPort(sdk_api=FakeSdkApi(result=status, num=num))
    assert port.check(np.eye(4), np.eye(4), np.eye(4)) == expected


@pytest.mark.parametrize(
    ("base", "tcp", "reason"),
    [
        (None, np.eye(4), "MISSING_BASE_CAMERA"),
        (np.eye(4), None, "MISSING_TCP_TARGET"),
        (np.eye(4), np.zeros((4, 4)), "INVALID_POSE"),
    ],
)
def test_invalid_or_missing_pose_is_unknown(base, tcp, reason):
    sdk = FakeSdkApi()
    result = NativeIkSdkPort(sdk_api=sdk).check(base, tcp, np.eye(4))
    assert result == {"checked": False, "reachable": None,
                      "valid_solution_count": None, "reason": reason}
    assert sdk.calls == []


def test_invalid_capture_joint_reference_is_unknown():
    sdk = FakeSdkApi()
    result = NativeIkSdkPort(sdk_api=sdk).check(np.eye(4), np.eye(4), np.eye(4), [0] * 5)
    assert result["reason"] == "INVALID_REFERENCE_JOINTS"
    assert sdk.calls == []


def test_missing_vendor_sdk_leaves_pose_review_available(monkeypatch):
    def unavailable():
        raise ImportError("SDK not installed")

    monkeypatch.setattr("pose6d_bridge.native_ik._load_sdk_api", unavailable)
    port = NativeIkSdkPort()
    assert port.check(np.eye(4), np.eye(4), np.eye(4))["reason"] == "SDK_UNAVAILABLE"


def test_xyzrpy_converter_and_tcp_display():
    transform = configured_transform({"xyz_m": [.1, .2, .3], "rpy_rad": [.3, -.4, .7]})
    np.testing.assert_allclose(matrix_to_xyzrpy(transform), [.1, .2, .3, .3, -.4, .7])
    display = tcp_targets(transform, np.eye(4), None, "r/base_link")
    assert display["frame"] == "r/base_link"
    np.testing.assert_allclose(display["suction"]["xyz_mm"], [100, 200, 300])
    np.testing.assert_allclose(display["suction"]["rpy_deg"], np.rad2deg([.3, -.4, .7]))
