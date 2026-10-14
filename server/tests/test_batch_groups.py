from app.ingest.batch import normalize_groups


def test_plan_example_n5():
    raw = {
        "recipes": [{"pages": [1, 2], "title_hint": "Daal"}, {"pages": [2]}, {"pages": [9]}],
        "unreadable_pages": [4],
    }
    groups, unreadable = normalize_groups(raw, 5)
    assert groups == [([1, 2], "Daal"), ([2], ""), ([3], ""), ([5], "")]
    assert unreadable == [4]


def test_out_of_range_and_duplicate_pages_within_group():
    raw = {
        "recipes": [
            {"pages": [3, 0, 3, 2, -1, 7], "title_hint": " Cake "},
            {"pages": [1, 1], "title_hint": "Soup"},
        ],
        "unreadable_pages": [6, 0],
    }
    groups, unreadable = normalize_groups(raw, 3)
    assert groups == [([1], "Soup"), ([2, 3], "Cake")]
    assert unreadable == []


def test_groups_ordered_by_first_page_and_pages_shared():
    raw = {
        "recipes": [{"pages": [3], "title_hint": "B"}, {"pages": [1, 3], "title_hint": "A"}],
        "unreadable_pages": [],
    }
    groups, _ = normalize_groups(raw, 3)
    assert groups == [([1, 3], "A"), ([2], ""), ([3], "B")]


def test_unreadable_sorted_deduped_and_not_also_grouped():
    raw = {"recipes": [{"pages": [2], "title_hint": "X"}], "unreadable_pages": [4, 1, 4, 2]}
    groups, unreadable = normalize_groups(raw, 4)
    assert groups == [([2], "X"), ([3], "")]
    assert unreadable == [1, 4]


def test_malformed_input_never_loses_pages():
    groups, unreadable = normalize_groups({"recipes": [{"pages": ["2", "x", None]}, "junk"]}, 2)
    assert groups == [([1], ""), ([2], "")]
    assert unreadable == []
    assert normalize_groups({}, 1) == ([([1], "")], [])
