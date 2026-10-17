import pytest

from waagd_discovery.normalization import normalize_url


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (
            "HTTPS://EXAMPLE.COM:443/News/?utm_source=rss&id=3#part",
            "https://example.com/News/?id=3",
        ),
        ("http://Example.COM:80", "http://example.com"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://BÜCHER.example/straße", "https://xn--bcher-kva.example/straße"),
        ("https://faß.de/a", "https://xn--fa-hia.de/a"),
        ("https://[2001:0db8::1]:443/x", "https://[2001:db8::1]/x"),
        (
            "https://example.com/a//b/../c/%2f?b=2&a=1&a=3&blank=&flag&token=a%2Bb+z",
            "https://example.com/a//b/../c/%2f?b=2&a=1&a=3&blank=&flag&token=a%2Bb+z",
        ),
        ("https://example.com/?%75tm_Source=x&id=3&FBCLID=x", "https://example.com/?id=3"),
        ("https://example.com/?source=rss&ref=home", "https://example.com/?source=rss&ref=home"),
        ("https://example.com/?a=1&&b=2&", "https://example.com/?a=1&&b=2&"),
    ],
)
def test_conservative_normalization(original: str, expected: str) -> None:
    assert normalize_url(original) == expected
    assert normalize_url(expected) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https:///missing",
        "ftp://example.com/a",
        "//example.com/a",
        "https://user:password@example.com/a",
        "https://:password@example.com/a",
        "https://@example.com/a",
        "https://example.com:99999/a",
        "https://example.com:0/a",
        "https://example.com:/a",
        "https://exa mple.com/a",
        "https://example.com/a\nb",
        "https://example.com/\\evil",
        "https://example.com/%ZZ",
        "https://example..com/a",
        "https://-example.com/a",
        "https://example_.com/a",
        "https://[fe80::1%25eth0]/a",
        "https://[bad]/a",
        "https://example.com/" + "a" * 4096,
    ],
)
def test_reject_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError, match="^invalid_url$"):
        normalize_url(url)


def test_slashes_remain_distinct() -> None:
    assert normalize_url("https://example.com/item") != normalize_url("https://example.com/item/")
    assert normalize_url("https://example.com") != normalize_url("https://example.com/")


def test_encoded_delimiters_do_not_change_query_structure() -> None:
    url = "https://example.com/?a=x%26utm_source%3Dkeep&token=%3F%23%2B&flag=&flag"
    assert normalize_url(url) == url


def test_modern_idna_does_not_merge_distinct_hosts() -> None:
    assert normalize_url("https://faß.de/a") != normalize_url("https://fass.de/a")
