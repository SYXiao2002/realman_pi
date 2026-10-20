"""Missing or changed runtime weights must never produce model readiness."""

import hashlib

import pytest

from test_grasp_protocol import observation

from grasp_server.backends import model_identity, verify_model
from grasp_server.server import load_service


def test_model_hash_and_mount_path_are_enforced(tmp_path):
    path = tmp_path / "fixture.bin"
    path.write_bytes(b"fixture-model")
    model = {"file": "fixture.bin", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert verify_model(model, tmp_path) == path
    path.write_bytes(b"different-model")
    with pytest.raises(ValueError, match="SHA-256"):
        verify_model(model, tmp_path)
    model["file"] = "../fixture.bin"
    with pytest.raises(ValueError, match="relative"):
        verify_model(model, tmp_path)


def test_missing_weights_are_unavailable_and_mock_requires_explicit_selection(tmp_path):
    from pathlib import Path

    config = Path(__file__).resolve().parents[3] / "config/grasp/server.yaml"
    service, _ = load_service(config, models_root_override=tmp_path)
    assert service.hello()["ready"] is False
    assert "geometry" in service.hello()["unavailable"]
    assert "mock" not in service.backends
    diagnostic, _ = load_service(config, backend_override="mock", models_root_override=tmp_path)
    assert diagnostic.hello()["backends"] == ["mock"]
    _, details = diagnostic.backends["mock"].infer(observation())
    assert details["diagnostic_only"] is True


def test_inference_configuration_is_part_of_model_identity():
    original = model_identity({"sha256": "a"}, {"width": 0.03}, {"confidence": 0.5})
    assert original != model_identity({"sha256": "b"}, {"width": 0.03}, {"confidence": 0.5})
    assert original != model_identity({"sha256": "a"}, {"width": 0.04}, {"confidence": 0.5})
    assert original != model_identity({"sha256": "a"}, {"width": 0.03}, {"confidence": 0.7})
