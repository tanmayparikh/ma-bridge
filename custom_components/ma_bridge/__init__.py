"""Music Assistant Bridge.

Exposes two things Home Assistant's Music Assistant integration doesn't:

* ``ma_bridge/recommendations``: the rows on Music Assistant's home page (Quick picks, Listen
  again, mixes...), with each item's artwork as a URL a browser can load.
* An image proxy. Music Assistant serves artwork from its own server, usually plain HTTP on the
  local network, which a dashboard served over HTTPS (or from outside the house) can't load.
  The bridge hands out signed ``/api/ma_bridge/image/...`` links that Home Assistant fetches
  on the browser's behalf. ``ma_bridge/images`` signs URLs a dashboard already has.

It reuses the Music Assistant integration's own connection; it has no settings of its own.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any
from urllib.parse import quote, urlsplit

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DATA_SECRET, DOMAIN, IMAGE_PATH, IMAGE_SIZE, MA_DOMAIN

_LOGGER = logging.getLogger(__name__)

try:  # ships with Home Assistant's Music Assistant integration
    from music_assistant_models.helpers import media_from_dict
except ImportError:  # pragma: no cover
    media_from_dict = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the websocket commands and the image view (once per Home Assistant run)."""
    first = DOMAIN not in hass.data
    hass.data[DOMAIN] = {DATA_SECRET: entry.data[DATA_SECRET]}
    if first:
        websocket_api.async_register_command(hass, ws_recommendations)
        websocket_api.async_register_command(hass, ws_images)
        hass.http.register_view(ImageView(hass))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Commands and views can't be unregistered; they answer with an error until set up again."""
    hass.data.pop(DOMAIN, None)
    return True


# --- Music Assistant access -------------------------------------------------------------------


def _mass(hass: HomeAssistant, entry_id: str | None = None):
    """The Music Assistant integration's client, from a loaded config entry."""
    entries = [
        e for e in hass.config_entries.async_entries(MA_DOMAIN)
        if e.state is ConfigEntryState.LOADED and (entry_id is None or e.entry_id == entry_id)
    ]
    if not entries:
        return None
    data = getattr(entries[0], "runtime_data", None)
    return getattr(data, "mass", None)


def _server_urls(mass) -> list[str]:
    """Where Music Assistant serves from: the address HA connects to and the one it advertises
    (on an add-on install these differ: an internal hostname versus the LAN IP)."""
    urls = [getattr(mass, "server_url", None), getattr(getattr(mass, "server_info", None), "base_url", None)]
    return [u.rstrip("/") for u in urls if u]


# --- Image signing ----------------------------------------------------------------------------


def _token(hass: HomeAssistant, url: str) -> str:
    secret = hass.data[DOMAIN][DATA_SECRET].encode()
    return hmac.new(secret, url.encode(), hashlib.sha256).hexdigest()[:40]


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


@callback
def browser_url(hass: HomeAssistant, mass, url: str | None) -> str | None:
    """A URL the browser can load: HTTPS as is, Music Assistant's own server through the proxy.

    Only Music Assistant's server is proxied, so the proxy can't be pointed anywhere else.
    """
    if not url:
        return None
    if url.startswith("https://") or url.startswith("data:"):
        return url
    if any(_same_origin(url, server) for server in _server_urls(mass)):
        return f"{IMAGE_PATH}/{_token(hass, url)}?url={quote(url, safe='')}"
    return None


def _image(hass: HomeAssistant, mass, raw: dict[str, Any]) -> str | None:
    """Artwork for a media item dict, using the client's own image URL helper."""
    url = None
    if media_from_dict is not None:
        try:
            item = media_from_dict(raw)
            try:
                url = mass.get_media_item_image_url(item, size=IMAGE_SIZE)
            except TypeError:
                url = mass.get_media_item_image_url(item)
        except Exception:  # noqa: BLE001 - an item without artwork shouldn't break the row
            _LOGGER.debug("No image for %s", raw.get("uri"), exc_info=True)
    if not url:
        # Fall back to a remote image the item already carries.
        images = list((raw.get("metadata") or {}).get("images") or [])
        if isinstance(raw.get("image"), dict):
            images.insert(0, raw["image"])
        for img in images:
            if img and img.get("remotely_accessible") and str(img.get("path", "")).startswith("http"):
                url = img["path"]
                break
    return browser_url(hass, mass, url)


def _item(hass: HomeAssistant, mass, raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("uri") or raw.get("media_type") in (None, "folder"):
        return None
    album = raw.get("album")
    return {
        "media_type": raw.get("media_type"),
        "uri": raw.get("uri"),
        "name": raw.get("name"),
        "version": raw.get("version") or "",
        "artists": [{"name": a.get("name")} for a in raw.get("artists") or [] if isinstance(a, dict)],
        "album": {"name": album.get("name")} if isinstance(album, dict) else None,
        "image": _image(hass, mass, raw),
    }


# --- Websocket commands -----------------------------------------------------------------------


def _plain(obj: Any) -> Any:
    """Music Assistant models as plain dicts (the client may hand back either)."""
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


def _shape(folders: Any) -> dict[str, Any]:
    """What Music Assistant returned, without the bulk: for working out why rows come back empty."""
    out: dict[str, Any] = {"type": type(folders).__name__}
    if not isinstance(folders, list):
        out["keys"] = sorted(folders.keys()) if isinstance(folders, dict) else None
        return out
    out["folders"] = []
    for f in folders[:20]:
        info: dict[str, Any] = {"type": type(f).__name__}
        f = _plain(f)
        if isinstance(f, dict):
            items = f.get("items") or []
            info.update({k: f.get(k) for k in ("name", "item_id", "provider", "media_type")})
            info["keys"] = sorted(f.keys())
            info["items"] = len(items)
            info["sample"] = []
            for i in items[:2]:
                kind = type(i).__name__
                i = _plain(i)
                info["sample"].append(
                    {"type": kind, "keys": sorted(i.keys()), **{k: i.get(k) for k in ("media_type", "uri", "name")}}
                    if isinstance(i, dict) else {"type": kind}
                )
        out["folders"].append(info)
    return out


@websocket_api.websocket_command({
    vol.Required("type"): "ma_bridge/recommendations",
    vol.Optional("config_entry_id"): str,
    vol.Optional("debug", default=False): bool,
})
@websocket_api.async_response
async def ws_recommendations(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Music Assistant's home-page rows, each with playable items."""
    if DOMAIN not in hass.data:
        connection.send_error(msg["id"], "not_loaded", "Music Assistant Bridge isn't set up")
        return
    mass = _mass(hass, msg.get("config_entry_id"))
    if mass is None:
        connection.send_error(msg["id"], "not_found", "Music Assistant isn't loaded")
        return
    try:
        folders = await mass.send_command("music/recommendations")
    except Exception as err:  # noqa: BLE001 - report Music Assistant's error to the caller
        connection.send_error(msg["id"], "music_assistant_error", str(err))
        return
    rows = []
    for folder in folders or []:
        folder = _plain(folder)
        if not isinstance(folder, dict):
            continue
        items = [i for i in (_item(hass, mass, _plain(raw)) for raw in folder.get("items") or []) if i]
        if not items:
            continue
        rows.append({
            "id": folder.get("item_id"),
            "provider": folder.get("provider"),
            "name": folder.get("name"),
            "subtitle": folder.get("subtitle"),
            "translation_key": folder.get("translation_key"),
            "icon": folder.get("icon"),
            "items": items,
        })
    result: dict[str, Any] = {"rows": rows}
    if msg["debug"]:
        result["debug"] = _shape(folders)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): "ma_bridge/images",
    vol.Required("urls"): [vol.Any(str, None)],
})
@websocket_api.async_response
async def ws_images(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Browser-loadable versions of artwork URLs (None where an image can't be proxied)."""
    if DOMAIN not in hass.data:
        connection.send_error(msg["id"], "not_loaded", "Music Assistant Bridge isn't set up")
        return
    mass = _mass(hass)
    connection.send_result(msg["id"], {"urls": [browser_url(hass, mass, u) if mass else None for u in msg["urls"]]})


# --- Image proxy ------------------------------------------------------------------------------


class ImageView(HomeAssistantView):
    """Serves Music Assistant artwork. No login needed: each link carries its own signature."""

    url = IMAGE_PATH + "/{token}"
    name = "api:ma_bridge:image"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        hass = self.hass
        url = request.query.get("url")
        if DOMAIN not in hass.data or not url or not hmac.compare_digest(token, _token(hass, url)):
            return web.Response(status=403)
        try:
            async with async_get_clientsession(hass).get(url, timeout=ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return web.Response(status=resp.status)
                body = await resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
        except (ClientError, TimeoutError):
            return web.Response(status=504)
        # Same URL, same image: let browsers keep it for a week.
        return web.Response(body=body, content_type=ctype.split(";")[0], headers={"Cache-Control": "public, max-age=604800, immutable"})
