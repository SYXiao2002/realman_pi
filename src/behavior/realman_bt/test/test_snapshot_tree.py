from pathlib import Path
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).parents[4]
sys.path.insert(0, str(ROOT / "src/behavior/realman_bt/scripts"))
from tree_metadata import read_metadata  # noqa: E402


def test_snapshot_tree_saves_the_wrist_cameras_through_the_snapshot_service():
    root = ET.parse(ROOT / "config/behavior-trees/snapshot.xml").getroot()
    assert root.attrib["main_tree_to_execute"] == "MainTree"
    tree = root.find("BehaviorTree")
    assert tree is not None and tree.attrib["ID"] == "MainTree"
    sequence = tree.find("Sequence")
    assert sequence is not None
    save = sequence.find("SaveCameraFrames")
    assert save is not None
    # The leaf carries the full camera topics and the destination folder directly.
    topics = save.attrib["image_topics"].split(",")
    assert topics == [
        "/camera_left/color/image_raw",
        "/camera_right/color/image_raw",
    ]
    assert save.attrib["directory"].startswith("/")
    assert save.attrib["service_name"] == "/camera_snapshot/save"
    assert save.attrib["dry_run"] == "{dry_run}"
    assert len(list(sequence)) == 1


def test_snapshot_tree_declares_the_arm_move_one_shot_launcher_contract():
    # arm_move is the only launcher that starts camera_snapshot_server, and the
    # metadata schema needs a non-empty arm/action list, so this camera-only tree
    # still rides the arm_move one-shot contract.
    assert read_metadata(ROOT / "config/behavior-trees/snapshot.xml") == (
        "r", "r", "execute_motion", "arm_move.launch.py", "true", "true"
    )


def test_save_camera_frames_is_registered_and_installed():
    executor = (
        ROOT / "src/behavior/realman_bt/src/realman_bt_executor_node.cpp"
    ).read_text()
    cmake = (ROOT / "src/behavior/realman_bt/CMakeLists.txt").read_text()
    assert 'registerNodeType<SaveCameraFramesNode>("SaveCameraFrames")' in executor
    # The leaf, the Python service, its config and the demo tree all ship together.
    assert "add_library(camera_snapshot_nodes src/save_camera_frames_node.cpp)" in cmake
    assert "scripts/camera_snapshot_server.py" in cmake
    assert "config/behavior-trees/snapshot.xml" in cmake
    assert "config/ros/camera_snapshot.yaml" in cmake
