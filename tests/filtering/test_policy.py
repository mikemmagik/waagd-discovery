from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path

import pytest

from waagd_discovery.filtering import FilterPolicy, assess, load_policy, select_candidates
from waagd_discovery.models import DiscoveryError
from waagd_discovery.storage import Repository


def test_default_policy_and_frozen_config(tmp_path: Path) -> None:
    default = FilterPolicy()
    assert default.max_age_hours == 72
    assert default.future_tolerance_minutes == 15
    assert default.min_description_chars == 40
    assert default.require_publisher is False
    with pytest.raises(FrozenInstanceError):
        default.require_publisher = True  # type: ignore[misc]
    path = tmp_path / "policy.yaml"
    path.write_text("filter: {}", encoding="utf-8")
    assert load_policy(path) == default
    path.write_text(
        "filter:\n  max_age_hours: 1.5\n  future_tolerance_minutes: 0\n"
        "  min_description_chars: 0\n  require_publisher: true\n",
        encoding="utf-8",
    )
    assert load_policy(path) == FilterPolicy(1.5, 0, 0, True)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_age_hours", 0),
        ("max_age_hours", -1),
        ("max_age_hours", True),
        ("max_age_hours", "72"),
        ("max_age_hours", float("nan")),
        ("max_age_hours", float("inf")),
        ("max_age_hours", 10**1000),
        ("future_tolerance_minutes", -1),
        ("future_tolerance_minutes", False),
        ("future_tolerance_minutes", float("-inf")),
        ("min_description_chars", 1.5),
        ("min_description_chars", -1),
        ("min_description_chars", True),
        ("require_publisher", 1),
        ("require_publisher", "false"),
    ],
)
def test_strict_policy_values(key: str, value: object) -> None:
    with pytest.raises(DiscoveryError, match="^filter_policy_invalid$"):
        FilterPolicy(**{key: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "content",
    [
        "",
        "[]",
        "filter: null",
        "filter: []",
        "other: {}",
        "filter: {}\nother: true",
        "filter: {keywords: [politics]}",
        "filter: {require_publisher: 1}",
        "filter: {max_age_hours: .nan}",
        "filter: {max_age_hours: .inf}",
        "filter: {min_description_chars: 1.2}",
        "filter: {max_age_hours: '72'}",
        "filter: {max_age_hours: 24, max_age_hours: 72}",
        "filter: {}\nfilter: {}",
        "filter: {true: 5}",
        "filter: {require_publisher: !!python/object:object {}}",
    ],
)
def test_invalid_yaml_fails_with_safe_code(tmp_path: Path, content: str) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(DiscoveryError) as error:
        load_policy(path)
    assert error.value.code in {"filter_policy_invalid", "filter_policy_unreadable"}
    assert str(path) not in str(error.value)


def test_oversized_and_unreadable_policy(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="filter_policy_unreadable"):
        load_policy(tmp_path / "missing")
    path = tmp_path / "large.yaml"
    path.write_text("x" * 65_537, encoding="utf-8")
    with pytest.raises(DiscoveryError, match="filter_policy_too_large"):
        load_policy(path)


def test_invalid_reference_and_limit_are_fixed_errors() -> None:
    with Repository(":memory:") as repository:
        with pytest.raises(DiscoveryError, match="filter_reference_time_invalid"):
            assess(repository, FilterPolicy(), datetime(2026, 10, 17))
        for invalid in (-1, True, 1.5):
            with pytest.raises(DiscoveryError, match="filter_limit_invalid"):
                select_candidates(repository, FilterPolicy(), limit=invalid)  # type: ignore[arg-type]
