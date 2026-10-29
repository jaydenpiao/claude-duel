from hypothesis import settings


def test_property_tests_have_no_deadline() -> None:
    assert settings().deadline is None
