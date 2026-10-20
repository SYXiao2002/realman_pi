import pytest

from realman_recording.canonical_features import materialize_canonical_frames, validate_command_frame
from realman_recording.lerobot_align import AlignedFrame
from realman_recording.lerobot_schema import schema_from_parameters


class _Kinematics:
    def pose(self, positions):
        return (positions[0], 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _schema(grippers=("left", "mid", "right"), arms=("l", "m", "r")):
    return schema_from_parameters(
        repo_id="realman/canonical", fps=10.0, arms=list(arms),
        arm_action_topics=[f"/{arm}/cartesian_velocity/command" for arm in arms],
        base_frames=[f"{arm}/base_link" for arm in arms],
        ee_links=["link_6"] * len(arms),
        cartesian_command_frames=[f"{arm}/base_link" for arm in arms],
        gripper_position_topics=[f"/gripper_{name}/position" for name in grippers],
        gripper_action_topics=[], camera_ids=["front"],
    )


@pytest.mark.parametrize(
    ("arms", "grippers", "expected_positions"),
    [
        (("l", "m", "r"), ("left", "mid", "right"), (0.1, 0.2, 0.3)),
        (("l", "m", "r"), ("left", "right"), (0.1, 0.3)),
        (("l", "r"), ("left", "right"), (0.1, 0.3)),
    ],
)
def test_materializes_physical_features_and_derived_velocity(arms, grippers, expected_positions):
    schema = _schema(grippers, arms)
    values = {}
    source_times = {}
    for arm in arms:
        offset = {"l": 0.0, "m": 10.0, "r": 20.0}[arm]
        topic = f"/{arm}/joint_states"
        values[topic] = tuple(offset + index for index in range(6))
        source_times[topic] = 0
        action_topic = f"/{arm}/cartesian_velocity/command"
        values[action_topic] = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
        source_times[action_topic] = 0
    for name in grippers:
        topic = f"/gripper_{name}/position"
        values[topic] = {"left": 0.1, "mid": 0.2, "right": 0.3}[name]
        source_times[topic] = 0
    next_values = {key: (tuple(value + 1.0 for value in item) if isinstance(item, tuple) else item)
                   for key, item in values.items()}
    frames = [
        AlignedFrame(0, values, source_times),
        AlignedFrame(1_000_000_000, next_values, {key: 1_000_000_000 for key in source_times}),
    ]
    result = materialize_canonical_frames(frames, schema, (_Kinematics(),) * len(arms), {"front": [0, 1_000_000_000]})
    assert result[0].joint_position[:6] == (0.0, 1.0, 2.0, 3.0, 4.0, 5.0)
    assert result[0].joint_position[-6:] == (20.0, 21.0, 22.0, 23.0, 24.0, 25.0)
    assert result[0].joint_velocity == (1.0,) * (6 * len(arms))
    assert result[1].ee_pose_base[:7] == (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    assert result[0].ee_velocity_base[:6] == (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert result[0].gripper_position == expected_positions
    assert result[0].command_cartesian_velocity == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0) * len(arms)
    assert result[0].sync_error_ns == (0,) * len(schema.sync_source_ids)


def test_rejects_wrong_joint_width_instead_of_silently_exporting_it():
    schema = _schema()
    frame = AlignedFrame(0, {"/l/joint_states": (0.0,)}, {"/l/joint_states": 0})
    try:
        materialize_canonical_frames([frame, frame], schema, (_Kinematics(),) * 3, {"front": [0, 1]})
    except ValueError as error:
        assert "joint" in str(error)
    else:
        raise AssertionError("wrong JointState width was accepted")


def test_rejects_cartesian_command_in_unconfigured_frame():
    assert validate_command_frame(
        "/l/cartesian_velocity/command", "l/base_link",
        ("/l/cartesian_velocity/command",), ("l/base_link",),
    ) is None
    try:
        validate_command_frame(
            "/l/cartesian_velocity/command", "l/tool/tcpgrip",
            ("/l/cartesian_velocity/command",), ("l/base_link",),
        )
    except ValueError as error:
        assert "frame mismatch" in str(error)
    else:
        raise AssertionError("mixed command frames were accepted")
