"""Exercise identity isolation and restart handling without a ROS installation."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from realman_camera_calibration.config import load_config
from realman_camera_calibration.session import content_hash, restore_session, session_identity, write_manifest


CONFIG = Path(__file__).parents[4] / "config/ros/camera_calibration.yaml"


@pytest.fixture
def saved_session(tmp_path):
    config = load_config(CONFIG)
    intrinsics = {"width": 640, "height": 480, "k": [1.0] * 9}
    manifest = {"schema_version": 1, "identity": session_identity(config, "r"),
                "intrinsics": intrinsics, "intrinsics_sha256": content_hash(intrinsics)}
    write_manifest(tmp_path, manifest)
    directory = tmp_path / "batches" / "batch-1"
    directory.mkdir(parents=True)
    sample = {
        "sample_id": "r-0000-batch-1", "arm_id": "r", "identity": manifest["identity"],
        "intrinsics_sha256": manifest["intrinsics_sha256"], "base_to_tool": [[1, 0, 0, 0]] * 4,
        "target_to_camera": [[1, 0, 0, 0]] * 4,
    }
    (directory / "sample.json").write_text(json.dumps(sample))
    return tmp_path, config, sample


def test_right_session_restores_without_left_camera(saved_session):
    directory, config, sample = saved_session
    manifest, samples = restore_session(directory, config, "r")
    assert manifest["identity"]["arm_id"] == "r"
    assert samples == [sample]


def test_arm_change_requires_another_session(saved_session):
    directory, config, _ = saved_session
    with pytest.raises(ValueError, match="identity changed"):
        restore_session(directory, config, "l")


@pytest.mark.parametrize("field", ["serial", "mount_id", "optical_frame"])
def test_changed_camera_installation_invalidates_session(saved_session, field):
    directory, config, _ = saved_session
    config = deepcopy(config)
    config["cameras"]["right"][field] = "changed"
    with pytest.raises(ValueError, match="identity changed"):
        restore_session(directory, config, "r")


def test_mixed_samples_are_rejected(saved_session):
    directory, config, sample = saved_session
    sample["arm_id"] = "l"
    (directory / "batches/batch-1/sample.json").write_text(json.dumps(sample))
    with pytest.raises(ValueError, match="different arm"):
        restore_session(directory, config, "r")


def test_changed_sample_intrinsics_are_rejected(saved_session):
    directory, config, sample = saved_session
    sample["intrinsics_sha256"] = "wrong"
    (directory / "batches/batch-1/sample.json").write_text(json.dumps(sample))
    with pytest.raises(ValueError, match="intrinsics"):
        restore_session(directory, config, "r")


def test_legacy_multi_arm_session_is_not_silently_imported(tmp_path):
    with pytest.raises(ValueError, match="legacy session"):
        restore_session(tmp_path, load_config(CONFIG), "r")


def test_uncommitted_batch_is_not_restored(saved_session):
    directory, config, sample = saved_session
    staging = directory / "batches/.uncommitted.tmp"
    staging.mkdir()
    sample["arm_id"] = "l"
    (staging / "sample.json").write_text(json.dumps(sample))
    _, samples = restore_session(directory, config, "r")
    assert len(samples) == 1


def test_changed_manifest_intrinsics_are_rejected(saved_session):
    directory, config, _ = saved_session
    path = directory / "session.json"
    manifest = json.loads(path.read_text())
    manifest["intrinsics"]["width"] = 1280
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="intrinsics hash"):
        restore_session(directory, config, "r")


def test_malformed_json_root_is_rejected_with_a_validation_error(saved_session):
    directory, config, _ = saved_session
    (directory / "batches/batch-1/sample.json").write_text("[]")
    with pytest.raises(ValueError, match="metadata must be an object"):
        restore_session(directory, config, "r")
    (directory / "session.json").write_text("[]")
    with pytest.raises(ValueError, match="manifest must be an object"):
        restore_session(directory, config, "r")
