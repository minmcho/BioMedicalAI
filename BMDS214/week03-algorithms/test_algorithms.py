from algorithms import (
    binary_search,
    insertion_sort,
    linear_search,
    merge_sort,
    quicksort,
    selection_sort,
)


DATA = [9, 2, 7, 1, 5, 5, 3]
EXPECTED = [1, 2, 3, 5, 5, 7, 9]


def test_linear_search_found():
    assert linear_search(DATA, 7) == 2


def test_linear_search_missing():
    assert linear_search(DATA, 100) == -1


def test_binary_search_found():
    sorted_data = sorted(DATA)
    assert sorted_data[binary_search(sorted_data, 7)] == 7


def test_binary_search_missing():
    assert binary_search(sorted(DATA), 100) == -1


def test_insertion_sort():
    assert insertion_sort(DATA) == EXPECTED


def test_selection_sort():
    assert selection_sort(DATA) == EXPECTED


def test_merge_sort():
    assert merge_sort(DATA) == EXPECTED


def test_quicksort():
    assert quicksort(DATA) == EXPECTED


def test_empty_inputs():
    assert insertion_sort([]) == []
    assert selection_sort([]) == []
    assert merge_sort([]) == []
    assert quicksort([]) == []
    assert linear_search([], 1) == -1
    assert binary_search([], 1) == -1


def test_duplicates():
    assert merge_sort([3, 3, 1, 2, 1]) == [1, 1, 2, 3, 3]
