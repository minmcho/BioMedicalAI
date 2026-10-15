"""Grayscale image representation from scratch (nested lists, no libraries).

An image is a list of rows; each row is a list of intensity values (numbers,
typically 0-255). All rows must have the same length.
"""


def validate(image):
    """Raise ValueError unless image is a non-empty rectangular matrix."""
    if not image or not image[0]:
        raise ValueError("image must be non-empty")
    width = len(image[0])
    if any(len(row) != width for row in image):
        raise ValueError("all rows must have the same length")
    return len(image), width


def intensity_stats(image):
    """Return (minimum, maximum, mean) intensity over the whole image."""
    validate(image)
    values = [v for row in image for v in row]
    return min(values), max(values), sum(values) / len(values)


def histogram(image, bins=256):
    """Count pixels per intensity bin; values are clipped into range."""
    validate(image)
    counts = [0] * bins
    for row in image:
        for v in row:
            idx = min(bins - 1, max(0, int(v * bins / 256)))
            counts[idx] += 1
    return counts


def normalize_minmax(image):
    """Rescale intensities linearly to 0-255; a flat image maps to zeros."""
    lo, hi, _ = intensity_stats(image)
    if hi == lo:
        return [[0 for _ in row] for row in image]
    return [[round((v - lo) * 255 / (hi - lo)) for v in row] for row in image]


def threshold_binary(image, threshold):
    """Return a 0/1 mask: 1 where intensity >= threshold, else 0."""
    validate(image)
    return [[1 if v >= threshold else 0 for v in row] for row in image]
