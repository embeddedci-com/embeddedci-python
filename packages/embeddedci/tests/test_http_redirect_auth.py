"""Credentials stay on their own origin across a redirect."""

import urllib.request

from embeddedci._http import _DropAuthOnRedirect


class _Hdrs(dict):
    def get_all(self, *_a):  # what HTTPRedirectHandler may ask of the headers
        return None


def _redirect(from_url, to_url):
    req = urllib.request.Request(from_url, headers={"Authorization": "ApiKey eci_x", "X-Benchpod-Lease": "l1", "Accept": "a"})
    return _DropAuthOnRedirect().redirect_request(req, None, 302, "Found", _Hdrs(), to_url)


def test_a_redirect_to_another_host_drops_the_credentials():
    new = _redirect("https://embeddedci.com/api/x", "https://bucket.s3.example/x")
    names = {k.lower() for k in new.headers}
    assert "authorization" not in names and "x-benchpod-lease" not in names
    assert "accept" in names


def test_a_redirect_on_the_same_origin_keeps_them():
    new = _redirect("https://embeddedci.com/api/x", "https://embeddedci.com/api/y")
    assert {k.lower() for k in new.headers} >= {"authorization", "x-benchpod-lease"}


def test_a_downgrade_to_http_drops_them():
    new = _redirect("https://embeddedci.com/api/x", "http://embeddedci.com/api/x")
    assert "authorization" not in {k.lower() for k in new.headers}
