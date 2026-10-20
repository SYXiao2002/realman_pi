"""Admission must stay exclusive through inference, expiry, and command drain."""

import concurrent.futures

import pytest

from realman_robot_driver.motion_coordinator import ArmOwnership


def test_legacy_commands_remain_compatible_until_task_acquires_arm():
    ownership = ArmOwnership()
    assert ownership.acquire("r")
    with pytest.raises(ValueError, match="already"):
        ownership.acquire_task("r", "grasp-1", 10.0)
    ownership.release("r")
    lease = ownership.acquire_task("r", "grasp-1", 10.0)
    assert not ownership.acquire("r")
    assert not ownership.acquire("r", "wrong-token")
    assert ownership.acquire("l")
    assert ownership.acquire("r", lease.token)
    with pytest.raises(ValueError, match="not drained"):
        ownership.release_task("r", lease.token)
    ownership.release("r")
    assert not ownership.acquire("r")
    ownership.release_task("r", lease.token)
    assert ownership.acquire("r")


def test_expiry_revokes_token_and_never_enables_takeover():
    now = [0.0]
    ownership = ArmOwnership(monotonic=lambda: now[0])
    lease = ownership.acquire_task("r", "grasp-1", 5.0)
    now[0] = 4.0
    renewed = ownership.renew_task("r", lease.token, 5.0)
    assert renewed.expires_at == 9.0
    assert ownership.acquire("r", lease.token)
    now[0] = 10.0
    assert ownership.expired_task("r")
    assert not ownership.acquire("r")
    with pytest.raises(ValueError, match="not drained"):
        ownership.release_task("r", lease.token)
    ownership.release("r")
    assert not ownership.acquire("r", lease.token)
    assert not ownership.acquire("r")
    with pytest.raises(ValueError):
        ownership.acquire_task("r", "grasp-2", 5.0)
    with pytest.raises(ValueError, match="expired"):
        ownership.renew_task("r", lease.token, 5.0)
    ownership.release_task("r", lease.token)
    assert not ownership.acquire("r", lease.token)
    assert ownership.acquire("r")


def test_simultaneous_task_claims_have_exactly_one_winner():
    ownership = ArmOwnership()

    def claim(index):
        try:
            return ownership.acquire_task("r", f"task-{index}", 10.0)
        except ValueError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(claim, range(32)))
    assert sum(result is not None for result in results) == 1


def test_gripper_reservation_excludes_motion_and_task_acquisition():
    ownership = ArmOwnership()
    command_id = ownership.acquire_external("r")
    assert not ownership.acquire("r")
    with pytest.raises(ValueError):
        ownership.acquire_task("r", "grasp", 10.0)
    with pytest.raises(ValueError):
        ownership.release_external("r", "wrong")
    assert ownership.is_busy("r")
    ownership.release_external("r", command_id)
    lease = ownership.acquire_task("r", "grasp", 10.0)
    with pytest.raises(ValueError):
        ownership.acquire_external("r")
    owned_command = ownership.acquire_external("r", lease.token)
    with pytest.raises(ValueError, match="not drained"):
        ownership.release_task("r", lease.token)
    ownership.release_external("r", owned_command)
    ownership.release_task("r", lease.token)


@pytest.mark.parametrize("ttl_sec", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_ttl_does_not_reserve_arm(ttl_sec):
    ownership = ArmOwnership()
    with pytest.raises(ValueError, match="ttl_sec"):
        ownership.acquire_task("r", "task", ttl_sec)
    assert ownership.acquire("r")


def test_operator_stop_revokes_inference_lease_without_releasing_ownership():
    ownership = ArmOwnership(monotonic=lambda: 42.0)
    lease = ownership.acquire_task("r", "grasp", 10.0)
    assert ownership.revoke_task("r")
    assert ownership.expired_task("r")
    assert not ownership.acquire("r", lease.token)
    assert not ownership.acquire("r")
    with pytest.raises(ValueError):
        ownership.renew_task("r", lease.token, 10.0)
    ownership.release_task("r", lease.token)
    assert ownership.acquire("r")
    assert not ownership.revoke_task("l")


def test_gripper_busy_is_not_an_arm_motion_for_the_lease_watchdog():
    ownership = ArmOwnership()
    command = ownership.acquire_external("r")
    assert ownership.is_busy("r") and not ownership.motion_is_busy("r")
    ownership.release_external("r", command)
    assert ownership.acquire("r")
    assert ownership.motion_is_busy("r")
