import json

import httpx
import pytest

from immich_pet_recognition.immich import ImmichClient, ImmichError


def test_current_cursor_search_contract_and_permissions():
    calls = []

    def handler(request):
        assert request.headers["x-api-key"] == "secret"
        assert request.url.query == b""
        if request.url.path == "/api/server/version":
            return httpx.Response(200, json={"major": 3, "minor": 3, "patch": 1})
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["filter"]["type"] == {"eq": "IMAGE"}
        assert payload["filter"]["trashedAt"] == {"eq": None}
        assert "page" not in payload
        result = {
            "items": [{"id": "b" if "cursor" in payload else "a"}],
            "nextCursor": None if "cursor" in payload else "next",
        }
        return httpx.Response(200, json={"assets": result})

    client = ImmichClient("http://immich/api/", "secret", httpx.MockTransport(handler))
    assert [a["id"] for a in client.assets("2026-10-11T00:00:00Z")] == ["a", "b"]
    assert calls[1]["cursor"] == "next"
    assert calls[0]["filter"]["updatedAt"] == {"gte": "2026-10-11T00:00:00Z"}
    client.close()


def test_repeated_cursor_fails_instead_of_infinite_scan():
    def handler(request):
        if request.url.path.endswith("version"):
            return httpx.Response(200, json={"major": 3, "minor": 3, "patch": 1})
        return httpx.Response(200, json={"assets": {"items": [], "nextCursor": "same"}})

    client = ImmichClient("http://immich", "secret", httpx.MockTransport(handler))
    with pytest.raises(ImmichError, match="repeated"):
        list(client.assets())


@pytest.mark.parametrize(
    "success,error,should_raise",
    [
        (True, None, False),
        (False, "duplicate", False),
        (False, "no_permission", True),
    ],
)
def test_tag_write_checks_bulk_result(success, error, should_raise):
    def handler(request):
        if request.url.path == "/api/tags":
            assert json.loads(request.content) == {"tags": ["Pets/Mochi"]}
            return httpx.Response(200, json=[{"id": "tag-id", "value": "Pets/Mochi"}])
        assert json.loads(request.content) == {"ids": ["asset-id"]}
        return httpx.Response(200, json=[{"id": "asset-id", "success": success, "error": error}])

    client = ImmichClient("http://immich", "secret", httpx.MockTransport(handler))
    if should_raise:
        with pytest.raises(ImmichError, match="tag.asset"):
            client.tag("asset-id", "Pets/Mochi")
    else:
        client.tag("asset-id", "Pets/Mochi")


def test_error_does_not_expose_remote_response_or_credentials():
    client = ImmichClient(
        "http://immich",
        "secret",
        httpx.MockTransport(lambda r: httpx.Response(401, json={"message": "secret"})),
    )
    with pytest.raises(ImmichError) as error:
        client.version()
    assert "secret" not in str(error.value)


def test_old_server_version_rejected():
    client = ImmichClient(
        "http://immich",
        "secret",
        httpx.MockTransport(
            lambda r: httpx.Response(200, json={"major": 2, "minor": 7, "patch": 0})
        ),
    )
    with pytest.raises(ImmichError, match="3.2"):
        client.version()


@pytest.mark.parametrize("error", [None, "not_found", "no_permission"])
def test_untag_checks_bulk_result_and_verifies_the_tag_is_gone(error):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            assert request.url.path == "/api/tags/butter-tag/assets"
            assert json.loads(request.content) == {"ids": ["asset-id"]}
            return httpx.Response(
                200, json=[{"id": "asset-id", "success": error is None, "error": error}]
            )
        return httpx.Response(200, json={"tags": [{"id": "rusty-tag", "value": "Pets/Rusty"}]})

    client = ImmichClient("http://immich", "secret", httpx.MockTransport(handler))
    asset = {"tags": [{"id": "butter-tag", "value": "Pets/Butter"}]}
    if error == "no_permission":
        with pytest.raises(ImmichError, match="tag.asset"):
            client.untag("asset-id", "Pets/Butter", asset)
    else:
        client.untag("asset-id", "Pets/Butter", asset)
        assert calls[-1] == ("GET", "/api/assets/asset-id")


def test_untag_refuses_missing_tag_metadata_and_detects_unverified_removal():
    client = ImmichClient(
        "http://immich",
        "secret",
        httpx.MockTransport(lambda request: httpx.Response(200, json={"tags": []})),
    )
    with pytest.raises(ImmichError, match="include asset tags"):
        client.untag("asset-id", "Pets/Butter", {})
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "DELETE":
            return httpx.Response(200, json=[{"id": "asset-id", "success": True}])
        return httpx.Response(200, json={"tags": [{"id": "tag-id", "value": "Pets/Butter"}]})

    client = ImmichClient("http://immich", "secret", httpx.MockTransport(handler))
    with pytest.raises(ImmichError, match="not verified"):
        client.untag(
            "asset-id", "Pets/Butter", {"tags": [{"id": "tag-id", "value": "Pets/Butter"}]}
        )
    assert calls == ["DELETE", "GET"]
