import pytest

from segmentation import dice_score, otsu_threshold, segment


def test_otsu_separates_two_clusters():
    img = [[10, 12], [200, 210]]
    t = otsu_threshold(img)
    assert 12 <= t < 200  # split point may sit on the last low-cluster value


def test_segment_bimodal_image():
    img = [[10, 12], [200, 210]]
    assert segment(img) == [[0, 0], [1, 1]]


def test_dice_identical_masks():
    assert dice_score([[1, 0], [0, 1]], [[1, 0], [0, 1]]) == 1.0


def test_dice_no_overlap():
    assert dice_score([[1, 0]], [[0, 1]]) == 0.0


def test_dice_partial_overlap():
    assert dice_score([[1, 1]], [[1, 0]]) == pytest.approx(2 / 3)


def test_dice_both_empty():
    assert dice_score([[0, 0]], [[0, 0]]) == 1.0


def test_dice_shape_mismatch():
    with pytest.raises(ValueError):
        dice_score([[1]], [[1, 0]])
