import numpy as np
import pytest

from pose6d.feedback import settled_joints


def readings():
    return [(9.3 + i * .05, np.ones(6), None, round((9.3 + i * .05) * 1e9)) for i in range(15)]


def check(samples, **kwargs):
    return settled_joints(samples, now=10., now_ns=10_000_000_000,
                          freshness_sec=.25, settle_sec=.5, max_speed=.01, **kwargs)


def test_half_second_settle_window_can_exceed_latest_feedback_age_limit():
    np.testing.assert_allclose(check(readings()), np.ones(6))


@pytest.mark.parametrize("failure", ["stale", "moving", "reported speed", "gap", "duplicate", "short"])
def test_stationary_check_still_rejects_invalid_feedback(failure):
    samples = readings()
    if failure == "stale":
        samples = [(t - 1, p, v, ns - 1_000_000_000) for t, p, v, ns in samples]
    elif failure == "moving":
        t, p, v, ns = samples[-1]; samples[-1] = (t, p + .01, v, ns)
    elif failure == "reported speed":
        t, p, v, ns = samples[-1]; samples[-1] = (t, p, np.ones(6) * .02, ns)
    elif failure == "gap":
        samples = samples[:5] + samples[-1:]
    elif failure == "duplicate":
        samples.insert(-1, samples[-2])
    else:
        samples = samples[-3:]
    with pytest.raises(ValueError):
        check(samples)
