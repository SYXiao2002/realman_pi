from pathlib import Path

import pytest
import yaml

from realman_camera_calibration.config import load_config


CONFIG = Path(__file__).parents[4] / "config" / "ros" / "camera_calibration.yaml"


def test_project_calibration_config_has_independent_wrist_inputs():
    config = load_config(CONFIG)
    assert list(config["cameras"]) == ["left", "right"]
    assert config["sampling"]["arm_order"] == ["r", "l"]
    assert config["sampling"]["default_arm"] == "r"
    assert config["board"]["square_length_m"] > 0
    assert config["board"]["type"] == "charuco"
    assert config["board"]["dictionary"] == "DICT_5X5_100"
    assert config["board"]["squares_x"] == 12
    assert config["board"]["squares_y"] == 9
    assert config["board"]["square_length_m"] == 0.015
    assert config["board"]["marker_length_m"] == 0.01125
    assert config["sampling"]["minimum_samples_per_arm"] == 30


def test_invalid_calibration_config_is_rejected(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("cameras: {left: {image_topic: /image}}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="camera_info_topic"):
        load_config(path)


def test_duplicate_camera_arm_mapping_is_rejected(tmp_path):
    source = CONFIG.read_text(encoding="utf-8")
    path = tmp_path / "invalid-arms.yaml"
    path.write_text(source.replace("arm_id: r", "arm_id: l", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one input"):
        load_config(path)


def test_right_arm_only_configuration_is_supported(tmp_path):
    config = yaml.safe_load(CONFIG.read_text())
    del config["cameras"]["left"]
    del config["robots"]["l"]
    config["sampling"]["arm_order"] = ["r"]
    path = tmp_path / "right.yaml"
    path.write_text(yaml.safe_dump(config))
    assert set(load_config(path)["robots"]) == {"r"}


def test_middle_camera_is_not_supported(tmp_path):
    config = yaml.safe_load(CONFIG.read_text())
    config["cameras"]["left"]["arm_id"] = "m"
    path = tmp_path / "middle.yaml"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="l or r"):
        load_config(path)
