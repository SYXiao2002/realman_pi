"""Reference-adapter frame conversion; algorithm parity is checked separately."""

import numpy as np
from grasp_server.backends import adapt_native_candidate, adapt_cgn_candidate


def test_origin_conversion_preserves_raw_matrix_and_rotation():
    native = np.eye(4)
    native[:3, 3] = [.02, -.03, .5 - .09]
    common = adapt_native_candidate(native, offset_m=.09)
    np.testing.assert_allclose(common[:3, 3], [.02, -.03, .5])
    np.testing.assert_array_equal(common[:3, :3], native[:3, :3])
    assert native[2, 3] == .5 - .09


def test_cgn_native_origin_is_adapted_without_tcp_compensation():
    native = np.eye(4)
    native[:3, 3] = [.02, -.03, .5 - .1034]
    common = adapt_cgn_candidate(native, hand_depth_m=.1034)
    np.testing.assert_allclose(common[:3, 3], [.02, -.03, .5])
    assert native[2, 3] == .5 - .1034
