import pytest

from filtering import box_blur, convolve2d, sobel_magnitude


def test_identity_kernel():
    img = [[1, 2], [3, 4]]
    assert convolve2d(img, [[0, 0, 0], [0, 1, 0], [0, 0, 0]]) == img


def test_even_kernel_rejected():
    with pytest.raises(ValueError):
        convolve2d([[1]], [[1, 0], [0, 1]])


def test_box_blur_smooths_impulse():
    img = [[0, 0, 0], [0, 9, 0], [0, 0, 0]]
    blurred = box_blur(img, size=3)
    assert blurred[1][1] == pytest.approx(1.0)  # 9 spread over 9 cells
    assert blurred[0][0] == pytest.approx(1.0)


def test_sobel_flat_image_has_no_edges():
    # interior pixels of a flat image have zero gradient; zero padding
    # creates an artificial edge at the border, so only the interior is checked
    mag = sobel_magnitude([[5, 5, 5, 5]] * 4)
    assert mag[1][1] == 0.0 and mag[2][2] == 0.0


def test_sobel_detects_vertical_edge():
    img = [[0, 0, 10], [0, 0, 10], [0, 0, 10]]
    mag = sobel_magnitude(img)
    assert mag[1][1] > 0  # boundary column responds
    assert mag[1][0] == pytest.approx(0.0)
