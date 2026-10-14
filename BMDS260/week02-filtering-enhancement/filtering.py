"""Spatial filtering from scratch: convolution, blur, and Sobel edges."""


def convolve2d(image, kernel):
    """Convolve image with an odd-sized square kernel (zero padding).

    Returns an image of the same size. The kernel is flipped, as in true
    convolution; for symmetric kernels (blur) and antisymmetric ones used in
    gradient pairs this matches the usual textbook results.
    """
    size = len(kernel)
    if size % 2 == 0 or any(len(row) != size for row in kernel):
        raise ValueError("kernel must be square with an odd size")
    pad = size // 2
    height, width = len(image), len(image[0])
    flipped = [row[::-1] for row in kernel[::-1]]
    out = [[0.0] * width for _ in range(height)]
    for y in range(height):
        for x in range(width):
            total = 0.0
            for ky in range(size):
                for kx in range(size):
                    iy, ix = y + ky - pad, x + kx - pad
                    if 0 <= iy < height and 0 <= ix < width:
                        total += image[iy][ix] * flipped[ky][kx]
            out[y][x] = total
    return out


def box_blur(image, size=3):
    """Average each pixel's neighborhood (a size x size all-ones kernel)."""
    weight = 1.0 / (size * size)
    kernel = [[weight] * size for _ in range(size)]
    return convolve2d(image, kernel)


SOBEL_X = [[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]]
SOBEL_Y = [[-1, -2, -1], [0, 0, 0], [1, 2, 1]]


def sobel_magnitude(image):
    """Per-pixel gradient magnitude from the Sobel operator pair."""
    gx = convolve2d(image, SOBEL_X)
    gy = convolve2d(image, SOBEL_Y)
    return [
        [(gx[y][x] ** 2 + gy[y][x] ** 2) ** 0.5 for x in range(len(image[0]))]
        for y in range(len(image))
    ]
