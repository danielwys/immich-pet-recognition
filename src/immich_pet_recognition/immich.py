import hashlib
from collections.abc import Iterator
from typing import Any

import httpx


def asset_revision(asset: dict[str, Any]) -> str:
    # Tagging changes updatedAt. It must not cause an inference/writeback loop.
    value = "|".join(str(asset.get(k, "")) for k in ("checksum", "fileModifiedAt", "thumbhash"))
    return hashlib.sha256(value.encode()).hexdigest()[:24]


class ImmichError(RuntimeError):
    pass


class ImmichClient:
    def __init__(self, url: str, api_key: str, transport=None):
        self.web_url = url.rstrip("/").removesuffix("/api")
        self.client = httpx.Client(
            base_url=self.web_url + "/api/",
            headers={"x-api-key": api_key},
            timeout=60,
            follow_redirects=False,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as error:
            # Do not surface server response bodies, URLs, or credentials in the review UI.
            raise ImmichError(
                f"Immich {method} {path}: HTTP {error.response.status_code}"
            ) from None
        except httpx.RequestError:
            raise ImmichError("Cannot reach Immich; check its URL and network connection") from None

    def version(self) -> dict:
        version = self.request("GET", "server/version").json()
        if (version["major"], version["minor"]) < (3, 2):
            raise ImmichError("This adapter requires Immich 3.2 or newer; tested contract: 3.3.1")
        return version

    def assets(self, updated_after: str | None = None) -> Iterator[dict]:
        self.version()
        filters: dict[str, Any] = {
            "type": {"eq": "IMAGE"},
            "visibility": {"in": ["timeline", "archive"]},
            "isOffline": {"eq": False},
            "trashedAt": {"eq": None},
        }
        if updated_after:
            filters["updatedAt"] = {"gte": updated_after}
        payload: dict[str, Any] = {"filter": filters, "size": 250}
        seen_cursors: set[str] = set()
        while True:
            result = self.request("POST", "search/metadata", json=payload).json()["assets"]
            yield from result["items"]
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise ImmichError("Immich returned a repeated search cursor")
            seen_cursors.add(cursor)
            payload["cursor"] = cursor

    def asset(self, asset_id: str) -> dict:
        return self.request("GET", f"assets/{asset_id}").json()

    def preview(self, asset_id: str) -> bytes:
        return self.request(
            "GET", f"assets/{asset_id}/thumbnail", params={"size": "preview", "edited": "false"}
        ).content

    def tag(self, asset_id: str, name: str):
        tags = self.request("PUT", "tags", json={"tags": [name]}).json()
        tag = next((t for t in tags if t["value"] == name), None)
        if tag is None:
            raise ImmichError("Immich did not return the requested tag")
        results = self.request("PUT", f"tags/{tag['id']}/assets", json={"ids": [asset_id]}).json()
        result = next((r for r in results if r["id"] == asset_id), None)
        if result is None or (not result["success"] and result.get("error") != "duplicate"):
            raise ImmichError("Immich could not attach this tag; check tag.asset permission")

    def untag(self, asset_id: str, name: str, asset: dict):
        tags = asset.get("tags")
        if not isinstance(tags, list):
            raise ImmichError("Immich did not include asset tags; cannot verify tag removal")
        tag = next((tag for tag in tags if tag["value"] == name), None)
        if tag is None:
            return
        results = self.request(
            "DELETE", f"tags/{tag['id']}/assets", json={"ids": [asset_id]}
        ).json()
        result = next((item for item in results if item["id"] == asset_id), None)
        if result is None or (not result["success"] and result.get("error") != "not_found"):
            raise ImmichError("Immich could not remove this tag; check tag.asset permission")
        remaining = self.asset(asset_id).get("tags")
        if not isinstance(remaining, list) or any(tag["value"] == name for tag in remaining):
            raise ImmichError("Tag removal was not verified in Immich; retry this change")
