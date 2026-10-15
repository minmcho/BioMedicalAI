from image_ops import shape, extract_patches, channel_means, normalize, split_by_group

IMG = [[[float(x + y), 1.0, 0.0] for x in range(4)] for y in range(4)]

def test_shape():
    assert shape(IMG) == (4, 4, 3)

def test_patches():
    p = extract_patches(IMG, 2, 2)
    assert len(p) == 4 and shape(p[0]) == (2, 2, 3)

def test_channel_means_and_normalize():
    m = channel_means(IMG)
    assert abs(m[1] - 1.0) < 1e-9
    out = normalize(IMG, m, [1.0, 1.0, 1.0])
    assert abs(channel_means(out)[1]) < 1e-9

def test_split_by_group_no_leakage():
    items = list(range(10))
    train, val = split_by_group(items, lambda i: i % 2, {1})
    assert all(i % 2 == 1 for i in val) and all(i % 2 == 0 for i in train)
