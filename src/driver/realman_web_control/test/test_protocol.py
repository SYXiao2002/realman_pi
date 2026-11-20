import json

import pytest

from realman_web_control.protocol import ProtocolError, parse_message


def test_keyboard_state_uses_l_r_physical_codes_and_safe_sequence():
    message = {"type": "keyboard_state", "arm": "l", "keys": ["KeyW", "KeyD"], "sequence": 42}
    assert parse_message(json.dumps(message)) == message


def test_keyboard_state_accepts_full_arm_and_gripper_set_but_not_arbitrary_codes():
    keys = ["KeyW", "KeyS", "KeyA", "KeyD", "KeyR", "KeyF", "KeyQ", "KeyE",
            "KeyZ", "KeyC", "KeyX", "KeyV", "Digit1", "Digit2"]
    message = {"type": "keyboard_state", "arm": "l", "keys": keys, "sequence": 4}
    assert parse_message(json.dumps(message)) == message
    for invalid in (["1"], ["Numpad1"], ["Digit1", "Digit1"], ["ControlLeft"]):
        with pytest.raises(ProtocolError):
            parse_message(json.dumps({**message, "keys": invalid}))


@pytest.mark.parametrize("message", [
    {"type": "keyboard_state", "arm": "m", "keys": [], "sequence": 1},
    {"type": "keyboard_state", "arm": "l", "keys": ["w"], "sequence": 1},
    {"type": "keyboard_state", "arm": "l", "keys": ["KeyW", "KeyW"], "sequence": 1},
    {"type": "keyboard_state", "arm": "r", "keys": [], "sequence": -1},
])
def test_keyboard_state_rejects_unsafe_values(message):
    with pytest.raises(ProtocolError):
        parse_message(json.dumps(message))


def test_select_input_mode_is_global_and_validates_syntax_only():
    assert parse_message(
        '{"type":"select_input_mode","request_id":"mode-1","mode_id":"policy"}'
    ) == {"type": "select_input_mode", "request_id": "mode-1", "mode_id": "policy"}
    for mode in ("web", "futuremode2"):
        assert parse_message(json.dumps({
            "type": "select_input_mode", "request_id": "mode-2", "mode_id": mode,
        }))["mode_id"] == mode


def test_stop_behavior_tree_is_global_and_needs_only_a_request_id():
    assert parse_message('{"type":"stop_behavior_tree","request_id":"bt-1"}') == {
        "type": "stop_behavior_tree", "request_id": "bt-1",
    }
    # No arm: the executor owns every arm of the tree it runs.
    assert "arm" not in parse_message('{"type":"stop_behavior_tree","request_id":"bt-1","arm":"l"}')
    with pytest.raises(ProtocolError):
        parse_message('{"type":"stop_behavior_tree"}')


def test_plan_grasp_is_read_only_and_armless():
    message = parse_message(json.dumps({
        "type": "plan_grasp", "request_id": "grasp-1",
        "profile_name": "right_wrist", "target_label": "eu", "backend": "geometry",
    }))
    assert message == {
        "type": "plan_grasp", "request_id": "grasp-1",
        "profile_name": "right_wrist", "target_label": "eu", "backend": "geometry", "cgn_context": "mask",
    }
    message["cgn_context"] = "box"
    assert parse_message(json.dumps(message))["cgn_context"] == "box"
    message["cgn_context"] = "invalid"
    with pytest.raises(ProtocolError, match="CGN context"):
        parse_message(json.dumps(message))
    with pytest.raises(ProtocolError, match="request_id"):
        parse_message('{"type":"plan_grasp","profile_name":"right_wrist","target_label":"eu"}')
    with pytest.raises(ProtocolError, match="profile_name"):
        parse_message(json.dumps({"type": "plan_grasp", "request_id": "x", "profile_name": "p" * 65,
                                  "target_label": "eu"}))


def test_tool_power_commands_have_strict_fields_and_boolean_value():
    assert parse_message('{"type":"tool_power_status","request_id":"power-read"}') == {
        "type": "tool_power_status", "request_id": "power-read",
    }
    assert parse_message('{"type":"tool_power_set","request_id":"power-set","enabled":true}') == {
        "type": "tool_power_set", "request_id": "power-set", "enabled": True,
    }
    for message in (
        {"type": "tool_power_status", "request_id": "power-read", "enabled": False},
        {"type": "tool_power_set", "request_id": "power-set", "enabled": 1},
        {"type": "tool_power_set", "request_id": "power-set", "enabled": False, "arm": "l"},
    ):
        with pytest.raises(ProtocolError):
            parse_message(json.dumps(message))


def test_tool_power_set_is_blocked_by_read_only_policy():
    from realman_web_control.protocol import reject_if_read_only

    with pytest.raises(ProtocolError, match="read-only"):
        reject_if_read_only("tool_power_set", True, "power-set")
    reject_if_read_only("tool_power_status", True, "power-read")


def test_pose6d_motion_protocol_accepts_only_ids_review_and_signed_step():
    select_tool = {"type": "pose6d_select_tool", "request_id": "t1", "tool_name": "tcpsuck"}
    assert parse_message(json.dumps(select_tool)) == select_tool
    prepare = {"type": "pose6d_prepare", "request_id": "p1", "observation_id": "o1", "result_id": "r1",
               "grasp_id": "left", "review_confirmed": True, "warning_confirmed": False}
    assert parse_message(json.dumps(prepare)) == prepare
    jog = {"type": "pose6d_jog", "request_id": "j1", "session_id": "s1", "step_mm": -2.5}
    assert parse_message(json.dumps(jog)) == jog
    auto = {"type": "pose6d_auto_suction", "request_id": "a1", "session_id": "s1"}
    assert parse_message(json.dumps(auto)) == auto
    heartbeat = {"type": "pose6d_heartbeat", "request_id": "h1", "session_id": "s1"}
    assert parse_message(json.dumps(heartbeat)) == heartbeat
    assert parse_message('{"type":"pose6d_close","request_id":"c1"}') == {
        "type": "pose6d_close", "request_id": "c1", "session_id": "",
    }
    for invalid in (
        {**jog, "pose_position_m": [0, 0, 0]},
        {**jog, "step_mm": 0},
        {**jog, "step_mm": float("nan")},
        {**prepare, "review_confirmed": 1},
        {**auto, "vacuum_confirmed": 1},
        {**auto, "speed_percent": 10},
    ):
        with pytest.raises(ProtocolError):
            parse_message(json.dumps(invalid))


def test_pose6d_automatic_segmentation_selects_only_a_bridge_owned_instance():
    segment = {"type": "pose6d_segment", "request_id": "s1", "observation_id": "o1"}
    assert parse_message(json.dumps(segment)) == segment
    preview = {"type": "pose6d_preview", "request_id": "p1", "observation_id": "o1",
               "instance_id": "o1:0"}
    assert parse_message(json.dumps(preview)) == preview
    infer = {"type": "pose6d_infer", "request_id": "i1", "observation_id": "o1", "instance_id": "o1:0"}
    assert parse_message(json.dumps(infer)) == infer
    for invalid in (
        {"type": "pose6d_infer", "request_id": "i1", "observation_id": "o1"},
        {**infer, "mask_png": "encoded"},
        {**preview, "mask_png": "encoded"},
        {**segment, "instance_id": "o1:0"},
    ):
        with pytest.raises(ProtocolError):
            parse_message(json.dumps(invalid))
    preview = {"type": "pose6d_preview", "request_id": "p1", "observation_id": "o1", "instance_id": "o1:0"}
    assert parse_message(json.dumps(preview)) == preview
    infer = {"type": "pose6d_infer", "request_id": "i1", "observation_id": "o1", "instance_id": "o1:0"}
    assert parse_message(json.dumps(infer)) == infer
    manual = {"type": "pose6d_infer", "request_id": "i2", "observation_id": "o1", "mask_png": "abc"}
    assert parse_message(json.dumps(manual)) == manual
    for invalid in (
        {**infer, "mask_png": "abc"},
        {"type": "pose6d_preview", "request_id": "p2", "observation_id": "o1"},
        {**infer, "mask": [1]},
    ):
        with pytest.raises(ProtocolError):
            parse_message(json.dumps(invalid))


@pytest.mark.parametrize("kind", ["clients", "pose6d_clients", "client_connected", "client_disconnected"])
def test_browser_cannot_spoof_internal_socket_membership(kind):
    with pytest.raises(ProtocolError):
        parse_message(json.dumps({"type": kind, "request_id": "spoof", "client_ids": []}))


@pytest.mark.parametrize("mode", ["", "Policy", "pika!", "a-b", "a_b", "2policy", "策略", "a" * 97])
def test_select_input_mode_rejects_invalid_identifiers(mode):
    with pytest.raises(ProtocolError, match="mode_id"):
        parse_message(json.dumps({
            "type": "select_input_mode", "request_id": "mode-1", "mode_id": mode,
        }))


@pytest.mark.parametrize("browser_id", [None, "", "x" * 97])
def test_select_input_mode_requires_bounded_browser_request_id(browser_id):
    message = {"type": "select_input_mode", "mode_id": "policy"}
    if browser_id is not None:
        message["request_id"] = browser_id
    with pytest.raises(ProtocolError, match="request_id"):
        parse_message(json.dumps(message))


def valid_motion():
    return {
        "type": "execute_motion",
        "request_id": "move-1",
        "arm": "l",
        "goal": {
            "command": 0,
            "reference_type": 0,
            "reference_name": "base",
            "joint_degrees": [0, 1, 2, 3, 4, 5],
            "pose_position_m": [0, 0, 0],
            "pose_quaternion_wxyz": [1, 0, 0, 0],
            "velocity_percent": 20,
            "blend_radius_percent": 0,
            "timeout_sec": 5,
        },
    }


def test_motion_message_is_normalized_and_finite():
    parsed = parse_message(json.dumps(valid_motion()))
    assert parsed["arm"] == "l"
    assert parsed["goal"]["joint_degrees"] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


def test_nan_and_wrong_arm_are_rejected():
    message = valid_motion()
    message["goal"]["joint_degrees"][0] = float("nan")
    with pytest.raises(ProtocolError, match="finite"):
        parse_message(json.dumps(message, allow_nan=True))
    message = valid_motion()
    message["arm"] = "center"
    with pytest.raises(ProtocolError, match="arm"):
        parse_message(json.dumps(message))


def test_unknown_messages_and_oversized_payloads_are_rejected():
    with pytest.raises(ProtocolError, match="unsupported"):
        parse_message('{"type":"launch_missiles","arm":"l"}')
    with pytest.raises(ProtocolError, match="exceeds"):
        parse_message("x" * 32, max_bytes=16)


def test_calibration_messages_require_one_supported_arm():
    capture = parse_message(
        '{"type":"capture_calibration_sample","request_id":"cap-1",'
        '"session_id":"","start_new_session":true,"arm_id":"r"}'
    )
    assert capture["arm_id"] == "r"
    assert capture["purpose"] == ""
    solved = parse_message(
        '{"type":"solve_calibration","request_id":"solve-1","session_id":"session-1","arm_id":"r"}'
    )
    assert solved == {
        "type": "solve_calibration",
        "request_id": "solve-1",
        "session_id": "session-1",
        "arm_id": "r",
    }
    with pytest.raises(ProtocolError, match="arm_id"):
        parse_message(
            '{"type":"capture_calibration_sample","request_id":"cap-2",'
            '"arm_ids":["l"]}'
        )
    for arm in ("m", "", ["l", "r"]):
        with pytest.raises(ProtocolError, match="arm_id"):
            parse_message(json.dumps({"type": "solve_calibration", "request_id": "s", "arm_id": arm}))


def test_calibration_capture_purpose_is_constrained():
    verify = parse_message(
        '{"type":"capture_calibration_sample","request_id":"cap-v",'
        '"session_id":"","start_new_session":true,"arm_id":"r","purpose":"verify"}'
    )
    assert verify["purpose"] == "verify"
    with pytest.raises(ProtocolError, match="purpose"):
        parse_message(
            '{"type":"capture_calibration_sample","request_id":"cap-x",'
            '"session_id":"","start_new_session":true,"arm_id":"r","purpose":"calibrate"}'
        )


def test_verify_calibration_message_is_normalized():
    parsed = parse_message(
        '{"type":"verify_calibration","request_id":"ver-1","arm_id":"r",'
        '"baseline_session_id":"session-a","verify_session_id":"session-b"}'
    )
    assert parsed == {
        "type": "verify_calibration",
        "request_id": "ver-1",
        "arm_id": "r",
        "baseline_session_id": "session-a",
        "verify_session_id": "session-b",
    }
    with pytest.raises(ProtocolError, match="baseline_session_id"):
        parse_message(
            '{"type":"verify_calibration","request_id":"ver-2","arm_id":"r","verify_session_id":"session-b"}'
        )


def test_kinematics_messages_are_normalized():
    current = parse_message(
        '{"type":"get_current_pose","request_id":"pose-1","arm":"m"}'
    )
    assert current == {
        "type": "get_current_pose",
        "request_id": "pose-1",
        "arm": "m",
    }
    solved = parse_message(
        json.dumps(
            {
                "type": "solve_ik",
                "request_id": "ik-1",
                "arm": "m",
                "goal": {
                    "reference_type": 1,
                    "reference_name": "cell",
                    "seed_joint_degrees": [0, 1, 2, 3, 4, 5],
                    "pose_position_m": [0.1, 0.2, 0.3],
                    "pose_quaternion_wxyz": [1, 0, 0, 0],
                },
            }
        )
    )
    assert solved["goal"]["seed_joint_degrees"] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]


def test_joint_record_messages_are_normalized():
    listed = parse_message(
        '{"type":"list_joint_records","request_id":"list-1","arm":"l"}'
    )
    saved = parse_message(
        '{"type":"save_joint_record","request_id":"save-1","arm":"m","label":"ready pose"}'
    )
    applied = parse_message(
        '{"type":"apply_joint_record","request_id":"apply-1","arm":"r","record_id":"ready-pose","command":2}'
    )
    deleted = parse_message(
        '{"type":"delete_joint_record","request_id":"delete-1","arm":"l","record_id":"ready-pose"}'
    )

    assert listed == {
        "type": "list_joint_records",
        "request_id": "list-1",
        "arm": "l",
    }
    assert saved == {
        "type": "save_joint_record",
        "request_id": "save-1",
        "arm": "m",
        "label": "ready pose",
    }
    assert applied == {
        "type": "apply_joint_record",
        "request_id": "apply-1",
        "arm": "r",
        "record_id": "ready-pose",
        "command": 2,
    }
    assert deleted == {
        "type": "delete_joint_record",
        "request_id": "delete-1",
        "arm": "l",
        "record_id": "ready-pose",
    }


def test_joint_record_messages_reject_missing_fields():
    with pytest.raises(ProtocolError, match="label"):
        parse_message('{"type":"save_joint_record","request_id":"save-1","arm":"l"}')
    with pytest.raises(ProtocolError, match="command"):
        parse_message(
            '{"type":"apply_joint_record","request_id":"apply-1","arm":"l","record_id":"ready","command":4}'
        )


def test_kinematics_messages_require_complete_vectors():
    with pytest.raises(ProtocolError, match="seed_joint_degrees"):
        parse_message(
            json.dumps(
                {
                    "type": "solve_ik",
                    "request_id": "ik-1",
                    "arm": "l",
                    "goal": {
                        "reference_type": 0,
                        "reference_name": "base",
                        "seed_joint_degrees": [0, 1],
                        "pose_position_m": [0, 0, 0],
                        "pose_quaternion_wxyz": [1, 0, 0, 0],
                    },
                }
            )
        )


def test_connected_trajectory_and_recovery_messages_are_normalized():
    motion = valid_motion()["goal"]
    parsed = parse_message(
        json.dumps(
            {
                "type": "execute_trajectory",
                "request_id": "trajectory-1",
                "arm": "r",
                "goal": {
                    "reference_type": 0,
                    "reference_name": "base",
                    "waypoints": [motion, motion],
                    "timeout_sec": 12,
                },
            }
        )
    )
    recovery = parse_message(
        '{"type":"recover_motion","request_id":"recover-1","arm":"r"}'
    )

    assert parsed["goal"]["timeout_sec"] == 12.0
    assert len(parsed["goal"]["waypoints"]) == 2
    assert parsed["goal"]["waypoints"][0]["joint_degrees"][1] == 1.0
    assert recovery == {
        "type": "recover_motion",
        "request_id": "recover-1",
        "arm": "r",
    }


def test_connected_trajectory_rejects_short_or_invalid_waypoint_lists():
    message = {
        "type": "execute_trajectory",
        "request_id": "trajectory-1",
        "arm": "l",
        "goal": {
            "reference_type": 0,
            "reference_name": "base",
            "waypoints": [valid_motion()["goal"]],
            "timeout_sec": 5,
        },
    }
    with pytest.raises(ProtocolError, match="2 through 256"):
        parse_message(json.dumps(message))

    message["goal"]["waypoints"] = [
        valid_motion()["goal"],
        {**valid_motion()["goal"], "velocity_percent": 0},
    ]
    with pytest.raises(ProtocolError, match="velocity_percent"):
        parse_message(json.dumps(message))
