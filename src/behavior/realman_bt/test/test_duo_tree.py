from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).parents[4]
TREE = ROOT / "config/behavior-trees/duo.xml"
THREE = ROOT / "config/behavior-trees/three.xml"


def test_duo_tree_moves_l_and_r_without_m():
    root = ET.parse(TREE).getroot()
    tree = root.find("BehaviorTree")
    sequence = tree.find("Sequence")

    assert root.attrib["main_tree_to_execute"] == "MainTree"
    assert tree is not None and tree.attrib["ID"] == "MainTree"
    assert sequence is not None and sequence.attrib["name"] == "two_arm_staged_move"
    assert root.attrib["realman_required_arms"] == "l,r"

    moves = sequence.findall("ThreeArmMoveJ")
    assert [move.attrib["name"] for move in moves] == ["all_zero", "requested_pose"]
    three_moves = ET.parse(THREE).getroot().findall(".//ThreeArmMoveJ")
    for move, three_move in zip(moves, three_moves):
        assert move.attrib["arms"] == "l,r"
        assert move.attrib["dry_run"] == three_move.attrib["dry_run"]
        assert move.attrib["velocity_percent"] == three_move.attrib["velocity_percent"]
        assert move.attrib["blend_radius_percent"] == three_move.attrib["blend_radius_percent"]
        assert move.attrib["timeout_sec"] == three_move.attrib["timeout_sec"]

    assert moves[0].attrib["l_joint_degrees"] == "0,0,0,0,0,0"
    assert moves[0].attrib["r_joint_degrees"] == "0,0,0,0,0,0"
    assert moves[1].attrib["l_joint_degrees"] == "0.229,30.124,82.19,-2.241,70.44,174.223"
    assert moves[1].attrib["r_joint_degrees"] == "2.583,30.02,93.644,2.532,56.142,-0.285"
