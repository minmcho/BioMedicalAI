import pytest

from image_basics import histogram, intensity_stats, normalize_minmax, threshold_binary, validate


IMG = [[0, 128], [255, 64]]


def test_validate_rejects_ragged():
    with pytest.raises(ValueError):
        validate([[1, 2], [3]])


def test_intensity_stats():
    lo, hi, mean = intensity_stats(IMG)
    assert (lo, hi) == (0, 255)
    assert mean == pytest.approx((0 + 128 + 255 + 64) / 4)


def test_histogram_bins():
    counts = histogram(IMG, bins=4)
    assert counts == [1, 1, 1, 1]  # 0, 64, 128, 255 fall one per quarter bin


def test_normalize_minmax():
    assert normalize_minmax([[10, 20], [30, 10]]) == [[0, 128], [255, 0]]


def test_normalize_flat_image():
    assert normalize_minmax([[7, 7], [7, 7]]) == [[0, 0], [0, 0]]


def test_threshold_binary():
    assert threshold_binary(IMG, 128) == [[0, 1], [1, 0]]
