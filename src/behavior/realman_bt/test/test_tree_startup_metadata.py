from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).parents[4]
sys.path.insert(0, str(ROOT / "src/behavior/realman_bt/scripts"))
from tree_metadata import read_metadata  # noqa: E402
from tree_metadata import runtime_metadata  # noqa: E402


def test_canonical_tree_metadata_declares_its_runtime_contract():
    assert read_metadata(ROOT / "config/behavior-trees/move.xml") == (
        "r", "r", "execute_motion", "arm_move.launch.py", "true", "true"
    )
    assert read_metadata(ROOT / "config/behavior-trees/three.xml") == (
        "r", "l,m,r", "execute_motion", "arm_move.launch.py", "true", "true"
    )
    assert read_metadata(ROOT / "config/behavior-trees/control.xml") == (
        "r", "l,m,r", "execute_motion", "control_router.launch.py", "false", "false"
    )
    assert read_metadata(ROOT / "config/behavior-trees/tool_x.xml") == (
        "l", "l", "cartesian_velocity", "arm_move.launch.py", "true", "true"
    )


def test_tree_metadata_defaults_to_safe_single_arm_one_shot(tmp_path):
    tree = tmp_path / "custom.xml"
    tree.write_text('<root main_tree_to_execute="MainTree"/>', encoding="utf-8")

    assert read_metadata(tree) == (
        "r", "r", "execute_motion", "arm_move.launch.py", "true", "true"
    )


def test_grasp_preflight_tracks_selected_wrist_profile(tmp_path):
    import shutil
    sys.path.insert(0, str(ROOT / "src/grasp/grasp_bridge"))
    config = tmp_path / "config"
    for filename in ("ros/grasp_task.yaml", "ros/grasp_profiles.yaml", "ros/camera_extrinsics.yaml",
                     "ros/cameras_ros2.yaml", "ros/realman_coordinates.yaml", "ros/gripper.yaml",
                     "grasp/gripper_specs.yaml"):
        target = config / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "config" / filename, target)
    tree = ROOT / "config/behavior-trees/grasp.xml"
    assert runtime_metadata(tree, config)[:3] == ("r", "r", "execute_motion")
    (config / "ros/grasp_task.local.yaml").write_text("profile: left_wrist\n")
    assert runtime_metadata(tree, config)[:3] == ("l", "l", "execute_motion")
    assert runtime_metadata(tree, config, "right_wrist")[:3] == ("r", "r", "execute_motion")


@pytest.mark.parametrize(
    "attribute, value",
    [
        ("realman_arm_id", "x"),
        ("realman_required_arms", "l,r,l"),
        ("realman_required_actions", "execute_motion,shell"),
        ("realman_launch", "shell"),
        ("realman_stop_on_terminal", "yes"),
        ("realman_exit_on_terminal", "no"),
    ],
)
def test_tree_metadata_rejects_unsafe_or_unknown_runtime_values(tmp_path, attribute, value):
    tree = tmp_path / "invalid.xml"
    tree.write_text(
        f'<root main_tree_to_execute="MainTree" {attribute}="{value}"/>',
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        read_metadata(tree)
