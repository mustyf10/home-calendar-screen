from app.dishes.search import search_dishes


def titles(dishes):
    return [d.title for d in dishes]


def test_matches_ingredients(session, make_dish):
    make_dish("Saag", ingredients=["500g spinach", "200g paneer"])
    make_dish("Dal", ingredients=["lentils"])
    assert titles(search_dishes(session, "paneer")) == ["Saag"]


def test_hash_word_matches_tag(session, make_dish):
    make_dish("Sheer khurma", type="dessert", tags=["eid"])
    make_dish("Dal")
    assert titles(search_dishes(session, "#eid")) == ["Sheer khurma"]


def test_all_words_required(session, make_dish):
    make_dish("Chicken rice", tags=["chicken"])
    make_dish("Chicken curry", tags=["chicken"])
    make_dish("Egg fried rice")
    assert titles(search_dishes(session, "chicken rice")) == ["Chicken rice"]


def test_words_may_match_different_fields(session, make_dish):
    make_dish("Biryani", tags=["chicken"], ingredients=["basmati rice"])
    assert titles(search_dishes(session, "chicken rice")) == ["Biryani"]


def test_type_filter(session, make_dish):
    make_dish("Chicken samosa", type="snack")
    make_dish("Chicken karahi")
    assert titles(search_dishes(session, "chicken", type="snack")) == ["Chicken samosa"]


def test_tag_filter(session, make_dish):
    make_dish("A", tags=["fish"])
    make_dish("B", tags=["chicken"])
    assert titles(search_dishes(session, tag="fish")) == ["A"]
    assert titles(search_dishes(session, tag="#Fish")) == ["A"]


def test_archived_excluded_unless_requested(session, make_dish):
    make_dish("Old pie", archived=True)
    make_dish("New pie")
    assert titles(search_dishes(session, "pie")) == ["New pie"]
    assert titles(search_dishes(session, "pie", archived=True)) == ["Old pie"]


def test_title_matches_first_then_alphabetical(session, make_dish):
    make_dish("Zesty fish", description="")
    make_dish("Aloo gobi", notes="good with fish")
    make_dish("Baked fish")
    assert titles(search_dishes(session, "fish")) == ["Baked fish", "Zesty fish", "Aloo gobi"]


def test_like_wildcards_are_escaped(session, make_dish):
    make_dish("100% beef burger")
    make_dish("Plain toast")
    assert titles(search_dishes(session, "%")) == ["100% beef burger"]
    assert titles(search_dishes(session, "_")) == []
