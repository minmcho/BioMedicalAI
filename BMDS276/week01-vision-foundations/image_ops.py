"""Core image operations from scratch (nested lists, no libraries).

An image is a list of rows; each row is a list of pixels; each pixel is a
list of channel values (numbers). Medical-imaging hygiene note: patches
from the same slide/patient must never straddle a train/val split.
"""

def shape(image):
    h = len(image)
    w = len(image[0]) if h else 0
    c = len(image[0][0]) if w else 0
    return h, w, c

def extract_patches(image, patch_size, stride):
    """Return list of patches (each a patch_size x patch_size sub-image)."""
    if patch_size <= 0 or stride <= 0:
        raise ValueError("patch_size and stride must be positive")
    h, w, _ = shape(image)
    patches = []
    for y in range(0, h - patch_size + 1, stride):
        for x in range(0, w - patch_size + 1, stride):
            patch = [row[x:x + patch_size] for row in image[y:y + patch_size]]
            patches.append(patch)
    return patches

def channel_means(image):
    h, w, c = shape(image)
    totals = [0.0] * c
    for row in image:
        for px in row:
            for i, v in enumerate(px):
                totals[i] += v
    n = h * w
    return [t / n for t in totals] if n else totals

def normalize(image, means, stds):
    return [[[ (v - means[i]) / stds[i] for i, v in enumerate(px)]
             for px in row] for row in image]

def split_by_group(items, group_of, val_groups):
    """Split items so every item of a validation group stays in validation."""
    train, val = [], []
    for it in items:
        (val if group_of(it) in val_groups else train).append(it)
    return train, val
