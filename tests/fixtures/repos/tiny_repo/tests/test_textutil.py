from tiny.textutil import slugify


def test_slugify_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_collapses_separators():
    assert slugify("a   b---c") == "a-b-c"
