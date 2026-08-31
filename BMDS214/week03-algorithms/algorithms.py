"""Searching and sorting algorithms for BMDS 214 Week 3."""


def linear_search(items, target):
    """Return the first index containing target, or -1."""
    for index, item in enumerate(items):
        if item == target:
            return index
    return -1


def binary_search(items, target):
    """Search a sorted sequence and return the target index, or -1."""
    left, right = 0, len(items) - 1
    while left <= right:
        middle = (left + right) // 2
        if items[middle] == target:
            return middle
        if items[middle] < target:
            left = middle + 1
        else:
            right = middle - 1
    return -1


def insertion_sort(items):
    """Return a sorted copy using insertion sort."""
    result = list(items)
    for i in range(1, len(result)):
        key = result[i]
        j = i - 1
        while j >= 0 and result[j] > key:
            result[j + 1] = result[j]
            j -= 1
        result[j + 1] = key
    return result


def selection_sort(items):
    """Return a sorted copy using selection sort."""
    result = list(items)
    for i in range(len(result)):
        minimum = i
        for j in range(i + 1, len(result)):
            if result[j] < result[minimum]:
                minimum = j
        result[i], result[minimum] = result[minimum], result[i]
    return result


def merge_sort(items):
    """Return a sorted copy using merge sort."""
    if len(items) <= 1:
        return list(items)

    middle = len(items) // 2
    left = merge_sort(items[:middle])
    right = merge_sort(items[middle:])
    return _merge(left, right)


def _merge(left, right):
    result = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1
    result.extend(left[i:])
    result.extend(right[j:])
    return result


def quicksort(items):
    """Return a sorted copy using recursive quicksort."""
    if len(items) <= 1:
        return list(items)

    pivot = items[len(items) // 2]
    less = [x for x in items if x < pivot]
    equal = [x for x in items if x == pivot]
    greater = [x for x in items if x > pivot]
    return quicksort(less) + equal + quicksort(greater)
