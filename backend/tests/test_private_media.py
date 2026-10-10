from urllib.parse import parse_qs, urlencode, urlsplit

from starlette.requests import Request

from app import private_media


def request(url, method="GET"):
    parsed = urlsplit(url)
    return Request({"type": "http", "method": method, "path": parsed.path,
                    "query_string": parsed.query.encode(), "headers": [], "scheme": "https",
                    "server": ("test", 443)})


def test_avatar_capability_is_bound_to_one_resource_and_expiry(monkeypatch):
    monkeypatch.setattr(private_media, "_signing_key", lambda: b"test-only-secret")
    now = 1_000_000
    monkeypatch.setattr(private_media.time, "time", lambda: now)
    link = private_media.staff_avatar_url("U0123456789")
    assert private_media.valid_private_media_request(request(link))
    assert private_media.valid_private_media_request(request(link, "HEAD"))
    assert not private_media.valid_private_media_request(request(link, "POST"))
    assert not private_media.valid_private_media_request(request(link.replace("U0123456789", "U9876543210")))
    assert not private_media.valid_private_media_request(request(link.replace("user-avatar/U0123456789", "avatar/account")))
    params = parse_qs(urlsplit(link).query)
    params["expires"] = [str(now + 10)]
    tampered = urlsplit(link).path + "?" + urlencode(params, doseq=True)
    assert not private_media.valid_private_media_request(request(tampered))
    assert not private_media.valid_private_media_request(request(link + "&signature=" + "0" * 64))
    monkeypatch.setattr(private_media.time, "time", lambda: now + 3600)
    assert not private_media.valid_private_media_request(request(link))


def test_missing_private_credential_never_issues_or_accepts_a_public_link(monkeypatch):
    monkeypatch.setattr(private_media, "_signing_key", lambda: None)
    assert private_media.staff_avatar_url("U0123456789") == ""
    assert not private_media.valid_private_media_request(request("/api/dashboard/user-avatar/U0123456789"))


def test_malformed_avatar_links_are_rejected(monkeypatch):
    monkeypatch.setattr(private_media, "_signing_key", lambda: b"test-only-secret")
    assert private_media.staff_avatar_url("../other") == ""
    for query in ("", "expires=nope&signature=" + "0" * 64,
                  "expires=1&signature=bad", "expires=1&expires=2&signature=" + "0" * 64):
        assert not private_media.valid_private_media_request(request("/api/dashboard/user-avatar/U0123456789?" + query))
