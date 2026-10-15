"""Segmentation from scratch: Otsu thresholding, masks, and Dice scoring."""


def otsu_threshold(image, bins=256):
    """Return the intensity threshold maximizing between-class variance.

    Classic Otsu method over the image histogram: for each candidate split,
    score = w0 * w1 * (mean0 - mean1)^2; return the best split's intensity.
    """
    counts = [0] * bins
    total = 0
    for row in image:
        for v in row:
            counts[min(bins - 1, max(0, int(v)))] += 1
            total += 1
    if total == 0:
        raise ValueError("image must be non-empty")
    sum_total = sum(i * counts[i] for i in range(bins))
    best_score, best_t = -1.0, 0
    w0, sum0 = 0, 0
    for t in range(bins):
        w0 += counts[t]
        sum0 += t * counts[t]
        w1 = total - w0
        if w0 == 0 or w1 == 0:
            continue
        mean0 = sum0 / w0
        mean1 = (sum_total - sum0) / w1
        score = w0 * w1 * (mean0 - mean1) ** 2
        if score > best_score:
            best_score, best_t = score, t
    return best_t


def segment(image):
    """Binary mask (0/1) using the Otsu threshold for this image."""
    t = otsu_threshold(image)
    return [[1 if v > t else 0 for v in row] for row in image]


def dice_score(mask_a, mask_b):
    """Dice overlap of two equal-shaped 0/1 masks; two empty masks score 1."""
    if len(mask_a) != len(mask_b) or any(
        len(ra) != len(rb) for ra, rb in zip(mask_a, mask_b)
    ):
        raise ValueError("masks must have the same shape")
    intersection = sum(
        1
        for ra, rb in zip(mask_a, mask_b)
        for a, b in zip(ra, rb)
        if a == 1 and b == 1
    )
    size_a = sum(v for row in mask_a for v in row)
    size_b = sum(v for row in mask_b for v in row)
    if size_a + size_b == 0:
        return 1.0
    return 2 * intersection / (size_a + size_b)
