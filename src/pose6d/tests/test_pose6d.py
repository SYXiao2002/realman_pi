from pathlib import Path

import numpy as np
import pytest
import threading
import time
import yaml

from grasp_bridge.geometry import from_pose
from pose6d.geometry import configured_transform, rigid, suction_pose, tcp_target
from pose6d.jog import JogSession
from pose6d.observation import ObservationStore
from pose6d.motion import MotionController, validate_config
from pose6d.path import screen_workspace


def test_contact_transform_and_signed_distance():
    T = np.array([[0, -1, 0, .1], [1, 0, 0, .2], [0, 0, 1, .3], [0, 0, 0, 1.]])
    region = {"center_obj": [.01, 0, 0], "normal_obj": [1, 0, 0], "tangent_ref_obj": [0, 1, 0]}
    contact, normal = suction_pose(T, region)
    np.testing.assert_allclose(contact[:3, 3], [.1, .21, .3])
    np.testing.assert_allclose(normal, [0, 1, 0])
    offset = np.eye(4)
    offset[2, 3] = -.02
    for distance in (.04, 0, -.005):
        target = tcp_target(contact, normal, distance, offset)
        np.testing.assert_allclose(target[:3, 3], [.1, .21 + distance + .02, .3])
        rigid(target)


def test_configured_transform_accepts_xyz_rpy_mapping():
    transform = configured_transform({"xyz_m": [.012, -.003, .02], "rpy_rad": [0, 0, np.pi / 2]})
    np.testing.assert_allclose(transform[:3, 3], [.012, -.003, .02])
    np.testing.assert_allclose(transform[:3, :3], [[0, -1, 0], [1, 0, 0], [0, 0, 1]], atol=1e-7)
    np.testing.assert_allclose(configured_transform(transform.tolist()), transform)
    with pytest.raises(ValueError, match="three finite"):
        configured_transform({"xyz_m": [0, 0, 0], "rpy_rad": [0, np.nan, 0]})


def test_motion_uses_a_registered_tool_and_valid_contact_transform():
    root = Path(__file__).resolve().parents[3]
    coordinates = yaml.safe_load((root / "config/ros/realman_coordinates.yaml").read_text())
    settings = yaml.safe_load((root / "config/ros/pose6d_bridge.yaml").read_text())
    motion = settings["motion"]
    tools = coordinates["robots"]["r"]["tools"]
    # Installation dimensions are operator configuration, not a fixed test
    # fixture. Validate the actual registered tool without pinning its length.
    validate_config(motion, settings["suction"])
    tool = tools[motion["tool_name"]]
    assert tool["controller_name"] == motion["tool_name"]
    pose = tool["pose"]
    rigid(from_pose(pose["xyz_m"], pose["quaternion_wxyz"]))
    configured_transform(motion["T_contact_tcp"])


def test_reject_invalid_geometry():
    with pytest.raises(ValueError):
        rigid(np.diag([-1, 1, 1, 1]))
    with pytest.raises(ValueError):
        suction_pose(np.eye(4), {"center_obj": [0, 0, 0], "normal_obj": [1, 0, 0], "tangent_ref_obj": [1, 0, 0]})






def test_jog_crosses_zero_and_locks_on_uncertain_motion():
    contact = np.diag([1., -1., -1., 1.])
    session = JogSession(contact, [0, 0, 1], np.eye(4), min_m=-.01, max_m=.05,
                         max_step_m=.05, position_tolerance_m=.001, angular_tolerance_rad=.01)
    ready = session.prepare()
    assert ready[2, 3] == pytest.approx(.04)
    with pytest.raises(ValueError, match="not ready"):
        session.jog(-.01, ready)
    session.finish(True, ready)
    target = session.jog(-.045, ready)
    assert target[2, 3] == pytest.approx(-.005)
    session.finish(True, target)
    assert session.confirmed_m == pytest.approx(-.005)
    target = session.jog(.005, target)
    session.finish(False)
    assert session.state == "blocked"
    with pytest.raises(ValueError, match="not ready"):
        session.jog(.001, target)


def test_jog_blocks_if_actual_tcp_drifted():
    session = JogSession(np.diag([1., -1., -1., 1.]), [0, 0, 1], np.eye(4),
                         min_m=-.01, max_m=.05, max_step_m=.01,
                         position_tolerance_m=.001, angular_tolerance_rad=.01)
    ready = session.prepare()
    session.finish(True, ready)
    ready[0, 3] += .01
    with pytest.raises(ValueError, match="actual TCP"):
        session.jog(-.001, ready)
    assert session.state == "blocked"


def test_observation_freezes_data_and_rejects_stale_result():
    store = ObservationStore()
    rgb = np.zeros((2, 2, 3), np.uint8)
    depth = np.ones((2, 2), np.float32)
    oid = store.freeze(rgb=rgb, depth_m=depth, K=np.eye(3), timestamp_ns=1, camera_frame="optical")
    rgb[0, 0, 0] = 255
    assert store.get(oid)["rgb"][0, 0, 0] == 0
    store.set_mask(oid, np.full((2, 2), 255, np.uint8))
    generation, request = store.request(oid, object_id="eu_plug", model_sha256="hash")
    result = {key: request[key] for key in ("request_id", "observation_id", "object_id", "model_sha256")}
    result.update(type="result", T_camera_object=np.eye(4).tolist())
    store.set_mask(oid, np.ones((2, 2), np.uint8))
    with pytest.raises(ValueError, match="changed"):
        store.accept_result(generation, request, result)
    generation, request = store.request(oid, object_id="eu_plug", model_sha256="hash")
    result = {key: request[key] for key in ("request_id", "observation_id", "object_id", "model_sha256")}
    result.update(type="result", T_camera_object=np.eye(4).tolist())
    store.request(oid, object_id="eu_plug", model_sha256="hash")
    with pytest.raises(ValueError, match="changed"):
        store.accept_result(generation, request, result)
    new_id = store.freeze(rgb=rgb, depth_m=depth, K=np.eye(3), timestamp_ns=2, camera_frame="optical")
    assert new_id != oid
    with pytest.raises(ValueError, match="replaced"):
        store.get(oid)


def test_selected_sam_instance_is_the_exact_foundationpose_mask_and_is_observation_bound():
    store = ObservationStore()
    rgb = np.zeros((4, 4, 3), np.uint8)
    depth = np.ones((4, 4), np.float32)
    oid = store.freeze(rgb=rgb, depth_m=depth, K=np.eye(3), timestamp_ns=1, camera_frame="optical")
    mask = np.zeros((4, 4), np.uint8)
    mask[1:3, 1:3] = 1
    store.set_instances(oid, {f"{oid}:0": {"status": "ok", "mask": mask}}, {"sam_model_sha256": "sam"})
    store.select_instance(oid, f"{oid}:0")
    _, request = store.request(oid, object_id="eu_plug", model_sha256="mesh")
    np.testing.assert_array_equal(request["mask"], mask)
    with pytest.raises(ValueError, match="unavailable"):
        store.select_instance(oid, "another-observation:0")


def test_observation_store_clear_invalidates_cached_instances():
    store = ObservationStore()
    oid = store.freeze(rgb=np.zeros((4, 4, 3), np.uint8), depth_m=np.ones((4, 4), np.float32),
                       K=np.eye(3), timestamp_ns=1, camera_frame="optical")
    store.clear()
    with pytest.raises(ValueError, match="unavailable"):
        store.get(oid)


def test_instance_cache_rejects_non_binary_wrong_size_and_unbounded_masks():
    store = ObservationStore()
    oid = store.freeze(rgb=np.zeros((4, 4, 3), np.uint8), depth_m=np.ones((4, 4), np.float32),
                       K=np.eye(3), timestamp_ns=1, camera_frame="optical")
    with pytest.raises(ValueError, match="matching binary"):
        store.set_instances(oid, {"i": {"status": "ok", "mask": np.ones((2, 2), np.uint8)}}, {})
    with pytest.raises(ValueError, match="matching binary"):
        store.set_instances(oid, {"i": {"status": "ok", "mask": np.full((4, 4), 255, np.uint8)}}, {})
    with pytest.raises(ValueError, match="cache limit"):
        store.set_instances(oid, {"i": {"status": "ok", "mask": np.ones((4, 4), np.uint8)}}, {}, max_bytes=4)


def motion_setup(*, execute_error=None, unknown=False, clock=time.monotonic):
    T_contact_tcp = np.eye(4)
    T_contact_tcp[:3, :3] = [[0, 0, 1], [0, 1, 0], [-1, 0, 0]]
    T_contact_tcp[:3, 3] = [.012, -.003, .02]
    config = {
        "enabled": True, "tool_name": "suction_tcp", "T_contact_tcp": T_contact_tcp.tolist(),
        "min_distance_m": -.02, "max_distance_m": .08, "max_step_m": .1,
        "velocity_percent": 10, "prepare_distance_m": .04, "second_prepare_distance_m": .005,
        "lift_height_m": .05, "prepare_velocity_percent": 10, "approach_velocity_percent": 20,
        "contact_velocity_percent": 5, "lift_velocity_percent": 10,
        "timeout_sec": 2., "position_tolerance_m": .001,
        "angular_tolerance_rad": .01, "workspace_m": [[-1, -1, -1], [1, 1, 1]],
        "cartesian_sample_step_m": .01, "angular_sample_step_rad": .1,
        "collision_margin_m": .001,
        "allow_partial_scene": True,
        "lease_ttl_sec": 6., "service_timeout_sec": .1,
    }
    suction = {"cup_diameter_m": .01, "body_radius_m": .008, "body_length_m": .04}

    class Port:
        def __init__(self):
            self.pose = np.eye(4)
            self.pose[:3, 3] = [.2, -.1, .3]
            self.busy = False
            self.interrupted = 0
            self.released = []
            self.targets = []
            self.commands = []
            self.velocities = []
            self.unknown = unknown

        def context(self): return {"tool": "suction_tcp", "motion_allowed": True}
        def acquire(self, owner, ttl): self.busy = True; return "token"
        def renew(self, token, ttl): assert token == "token"
        def validate(self, token): assert token == "token"
        def release(self, token):
            if self.unknown: raise RuntimeError("command still unknown")
            self.busy = False; self.released.append(token)
        def current(self): return self.pose.copy()
        def check_path(self, actual, target, session, guard):
            guard(); self.targets.append((actual.copy(), target.copy()))
        def execute(self, target, token, cancel, feedback, *, command="movel", velocity_percent=None):
            if execute_error: raise RuntimeError(execute_error)
            if cancel.is_set(): raise RuntimeError("canceled")
            self.commands.append(command)
            self.velocities.append(velocity_percent)
            self.pose = target.copy()
        def interrupt(self): self.interrupted += 1; self.unknown = False; self.busy = False
        def lease_uncertain(self): return self.unknown

    port = Port()
    controller = MotionController(port, config, suction, clock=clock)
    obs = {"observation_id": "obs", "captured_at": clock(), "T_base_camera": np.eye(4),
           "result": {"T_camera_object": np.eye(4).tolist(), "screening": {"status": "pass_screening"}}}
    grasps = [{"grasp_id": "g1", "status": "reliable", "contact_region": {
        "center_obj": [0, 0, 0], "normal_obj": [0, 0, 1], "tangent_ref_obj": [1, 0, 0]}}]
    assert controller.load("client", obs, grasps, "result")[0] is True
    return controller, port, obs, config


def test_motion_controller_prepare_jog_crosses_zero_with_rotated_tcp():
    controller, port, _obs, _config = motion_setup()
    feedback = lambda _event: None
    prepared = controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                                   "grasp_id": "g1", "review_confirmed": True}, feedback)
    assert prepared["state"] == "ready" and prepared["confirmed_mm"] == pytest.approx(40)
    np.testing.assert_allclose(port.targets[0][1][:3, 3], [.012, .003, .02], atol=1e-9)
    expected_offsets = [20, 0, -10, 10]
    for delta, final in [(-20, 20), (-20, 0), (-10, -10), (20, 10)]:
        state = controller.jog({"client_id": "client", "session_id": prepared["session_id"],
                                "step_mm": delta}, feedback)
        assert state["state"] == "ready"
        assert state["confirmed_mm"] == pytest.approx(final)
    assert len(port.targets) == 5
    assert port.commands == ["movej_p", "movel", "movel", "movel", "movel"]
    assert port.released == []
    assert controller.stop("client", prepared["session_id"])["lease_retained"] is False
    assert port.released == ["token"]


def test_automatic_suction_uses_configured_approach_contact_and_base_vertical_lift():
    controller, port, _obs, _config = motion_setup()
    feedback = lambda _event: None
    prepared = controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                                   "grasp_id": "g1", "review_confirmed": True}, feedback)
    assert prepared["auto_suction_ready"] is True
    with pytest.raises(ValueError, match="externally"):
        controller.auto_suction({"client_id": "client", "session_id": prepared["session_id"]}, feedback)
    assert controller.status()["state"] == "ready"

    complete = controller.auto_suction({"client_id": "client", "session_id": prepared["session_id"],
                                        "vacuum_confirmed": True}, feedback)
    assert complete["state"] == "lifted"
    assert complete["confirmed_mm"] is None and complete["auto_suction_ready"] is False
    assert port.commands == ["movej_p", "movel", "movel", "movel"]
    assert port.velocities == [10, 20, 5, 10]
    first, contact, lift = [target for _actual, target in port.targets[1:]]
    assert np.linalg.norm(first[:3, 3] - port.targets[0][1][:3, 3]) == pytest.approx(.035)
    assert np.linalg.norm(contact[:3, 3] - first[:3, 3]) == pytest.approx(.005)
    np.testing.assert_allclose(lift[:3, 3] - contact[:3, 3], [0, 0, .05], atol=1e-9)
    with pytest.raises(ValueError, match="signed travel"):
        controller.jog({"client_id": "client", "session_id": prepared["session_id"], "step_mm": -1}, feedback)


def test_automatic_suction_failure_blocks_session_without_retrying():
    controller, port, _obs, _config = motion_setup()
    feedback = lambda _event: None
    prepared = controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                                   "grasp_id": "g1", "review_confirmed": True}, feedback)
    execute = port.execute

    def fail_at_contact(target, token, cancel, callback, *, command="movel", velocity_percent=None):
        if velocity_percent == 5:
            raise RuntimeError("contact segment failed")
        execute(target, token, cancel, callback, command=command, velocity_percent=velocity_percent)

    port.execute = fail_at_contact
    request = {"client_id": "client", "session_id": prepared["session_id"], "vacuum_confirmed": True}
    with pytest.raises(RuntimeError, match="contact segment failed"):
        controller.auto_suction(request, feedback)
    assert controller.status()["state"] == "blocked"
    assert port.commands == ["movej_p", "movel"]
    with pytest.raises(ValueError, match="preparation position"):
        controller.auto_suction(request, feedback)
    assert port.commands == ["movej_p", "movel"]


def test_motion_rejects_wrong_owner_and_out_of_range_without_poisoning_ready_session():
    controller, _port, _obs, _config = motion_setup()
    feedback = lambda _event: None
    prepared = controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                                   "grasp_id": "g1", "review_confirmed": True}, feedback)
    with pytest.raises(ValueError, match="identity"):
        controller.jog({"client_id": "other", "session_id": prepared["session_id"], "step_mm": -1}, feedback)
    with pytest.raises(ValueError, match="signed travel"):
        controller.jog({"client_id": "client", "session_id": prepared["session_id"], "step_mm": -61}, feedback)
    assert controller.status()["state"] == "ready"
    with pytest.raises(ValueError, match="existing motion session"):
        controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                            "grasp_id": "g1", "review_confirmed": True}, feedback)
    assert controller.status()["state"] == "ready"


def test_motion_action_failure_blocks_and_retains_unknown_lease_until_stop():
    controller, port, _obs, _config = motion_setup(execute_error="result lost", unknown=True)
    with pytest.raises(RuntimeError, match="result lost"):
        controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                            "grasp_id": "g1", "review_confirmed": True}, lambda _event: None)
    status = controller.status()
    assert status["state"] == "blocked" and status["lease_retained"] is True
    assert port.released == []
    closed = controller.stop("client", status["session_id"])
    assert closed["session_id"] == "" and closed["lease_retained"] is False
    assert port.interrupted == 1


def test_motion_does_not_stop_or_poison_another_owner_when_lease_is_refused():
    controller, port, _obs, _config = motion_setup()
    port.acquire = lambda *_args: (_ for _ in ()).throw(ValueError("arm already has another owner"))
    with pytest.raises(ValueError, match="another owner"):
        controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                            "grasp_id": "g1", "review_confirmed": True}, lambda _event: None)
    assert port.interrupted == 0
    status = controller.status()
    controller.stop("client", status["session_id"])
    assert port.interrupted == 0


def test_motion_requires_explicit_confirmation_for_screening_warning():
    controller, port, obs, _config = motion_setup()
    obs["result"]["screening"]["status"] = "insufficient_evidence"
    with pytest.raises(ValueError, match="warning"):
        controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                            "grasp_id": "g1", "review_confirmed": True}, lambda _event: None)
    assert port.targets == []


def test_motion_lease_renews_without_browser_heartbeat():
    now = [10.]
    controller, port, _obs, _config = motion_setup(clock=lambda: now[0])
    prepared = controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                                   "grasp_id": "g1", "review_confirmed": True}, lambda _event: None)
    now[0] += 2.1
    controller.tick()
    assert controller.status()["state"] == "ready"
    assert controller.status()["lease_retained"] is True
    assert port.interrupted == 0


def test_disconnect_does_not_interrupt_active_prepare_and_releases_after_completion():
    controller, port, _obs, _config = motion_setup()
    entered, finish = threading.Event(), threading.Event()
    original_execute = port.execute

    def slow_execute(target, token, cancel, feedback, *, command="movel", velocity_percent=None):
        entered.set()
        assert finish.wait(3)
        original_execute(target, token, cancel, feedback, command=command, velocity_percent=velocity_percent)

    port.execute = slow_execute
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(controller.prepare(
        {"client_id": "client", "observation_id": "obs", "result_id": "result",
         "grasp_id": "g1", "review_confirmed": True}, lambda _event: None)))
    worker.start()
    try:
        assert entered.wait(2)
        controller.detach("client")
        controller.tick()
        assert not controller.cancel.is_set()
        assert port.interrupted == 0 and port.released == []
    finally:
        finish.set()
        worker.join(3)
    assert not worker.is_alive() and outcome[0]["state"] == "ready"
    controller.tick()
    assert port.released == ["token"] and port.interrupted == 0
    assert controller.status()["session_id"] == ""


def test_detached_session_keeps_lease_when_release_is_uncertain():
    controller, port, _obs, _config = motion_setup(unknown=True)
    controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                        "grasp_id": "g1", "review_confirmed": True}, lambda _event: None)
    controller.detach("client")
    controller.tick()
    assert controller.status()["lease_retained"] is True
    assert controller.status()["session_id"] and port.interrupted == 0


def test_observation_has_no_time_expiry_before_prepare_or_during_jog():
    now = [10.]
    controller, port, obs, _config = motion_setup(clock=lambda: now[0])
    obs["captured_at"] = -10000.
    now[0] += 3600
    assert controller.readiness()[0]
    command = {"client_id": "client", "observation_id": "obs", "result_id": "result",
               "grasp_id": "g1", "review_confirmed": True}
    prepared = controller.prepare(command, lambda _: None)
    now[0] += 3600
    moved = controller.jog({"client_id": "client", "session_id": prepared["session_id"], "step_mm": -1}, lambda _: None)
    assert moved["confirmed_mm"] == pytest.approx(39)


def test_motion_context_change_blocks_session():
    controller, port, _obs, _config = motion_setup()
    prepared = controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                                   "grasp_id": "g1", "review_confirmed": True}, lambda _event: None)
    port.context = lambda: {"tool": "other", "motion_allowed": True}
    with pytest.raises(ValueError, match="context changed"):
        controller.jog({"client_id": "client", "session_id": prepared["session_id"], "step_mm": -1}, lambda _event: None)
    assert controller.status()["state"] == "blocked"


def test_motion_rejects_second_jog_while_action_is_in_flight():
    controller, port, _obs, _config = motion_setup()
    prepared = controller.prepare({"client_id": "client", "observation_id": "obs", "result_id": "result",
                                   "grasp_id": "g1", "review_confirmed": True}, lambda _event: None)
    entered, finish = threading.Event(), threading.Event()
    original_execute = port.execute

    def slow_execute(target, token, cancel, feedback, *, command="movel", velocity_percent=None):
        entered.set()
        finish.wait(1)
        original_execute(target, token, cancel, feedback, command=command, velocity_percent=velocity_percent)

    port.execute = slow_execute
    outcome = []
    thread = threading.Thread(target=lambda: outcome.append(controller.jog(
        {"client_id": "client", "session_id": prepared["session_id"], "step_mm": -1}, lambda _event: None)))
    thread.start()
    assert entered.wait(1)
    with pytest.raises(ValueError, match="not queued"):
        controller.jog({"client_id": "client", "session_id": prepared["session_id"], "step_mm": -1}, lambda _event: None)
    finish.set(); thread.join(1)
    assert not thread.is_alive() and outcome[0]["confirmed_mm"] == pytest.approx(39)


def test_suction_workspace_checks_body_and_tcp():
    _controller, _port, _obs, config = motion_setup()
    config["T_contact_tcp"] = np.eye(4).tolist()
    suction = {"cup_diameter_m": .01, "body_radius_m": .008, "body_length_m": .04}
    pose = np.eye(4)
    screen_workspace([pose], config, suction)
    config["workspace_m"][0][2] = -.04
    with pytest.raises(ValueError, match="workspace"):
        screen_workspace([pose], config, suction)
    config["workspace_m"][0][2] = -1
    pose[0, 3] = 1.01
    with pytest.raises(ValueError, match="workspace"):
        screen_workspace([pose], config, suction)


def test_history_request_carries_capture_identity_and_instance_metadata():
    store = ObservationStore()
    oid = store.freeze(rgb=np.zeros((2, 2, 3), np.uint8), depth_m=np.ones((2, 2), np.float32),
                       K=np.eye(3), timestamp_ns=1794470462051315677, camera_frame='right_optical')
    store.set_mask(oid, np.ones((2, 2), np.uint8))
    _, request = store.request(oid, object_id='plug', model_sha256='hash')
    assert request['capture_timestamp_ns'] == '1794470462051315677'
    assert request['camera_frame'] == 'right_optical'
    assert request['mask_source'] == 'manual'
    assert request['instance_id'] is None
