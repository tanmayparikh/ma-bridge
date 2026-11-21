"""Music Assistant Bridge.

Exposes two things Home Assistant's Music Assistant integration doesn't:

* ``ma_bridge/recommendations``: the rows on Music Assistant's home page (Quick picks, Listen
  again, mixes...), with each item's artwork as a URL a browser can load.
* The play queue beyond the next track (``ma_bridge/queue_items``), playing or removing an item
  in it, and the queue's Autoplay and Crossfade switches (``ma_bridge/queue``, ``queue_set``).
* An image proxy. Music Assistant serves artwork from its own server, usually plain HTTP on the
  local network, which a dashboard served over HTTPS (or from outside the house) can't load.
  The bridge hands out signed ``/api/ma_bridge/image/...`` links that Home Assistant fetches
  on the browser's behalf. ``ma_bridge/images`` signs URLs a dashboard already has.

It reuses the Music Assistant integration's own connection; it has no settings of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import quote, urlsplit

import voluptuous as vol
from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CACHE_SECONDS, DATA_SECRET, DOMAIN, IMAGE_PATH, IMAGE_SIZE, MA_DOMAIN, ROW_LIMIT

_LOGGER = logging.getLogger(__name__)
REGISTERED = f"{DOMAIN}_registered"

try:  # ships with Home Assistant's Music Assistant integration
    from music_assistant_models.helpers import media_from_dict
except ImportError:  # pragma: no cover
    media_from_dict = None
try:
    from music_assistant_models.media_items import MediaItemImage
except ImportError:  # pragma: no cover
    MediaItemImage = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the websocket commands and the image view (once per Home Assistant run: a
    reloaded entry must not register the image route twice)."""
    hass.data[DOMAIN] = {DATA_SECRET: entry.data[DATA_SECRET]}
    if not hass.data.get(REGISTERED):
        hass.data[REGISTERED] = True
        for command in (ws_recommendations, ws_images, ws_queue, ws_queue_items, ws_queue_play, ws_queue_remove, ws_queue_set):
            websocket_api.async_register_command(hass, command)
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
    if not url and isinstance(raw.get("image"), dict) and MediaItemImage is not None:
        # A bare image record (queue items carry one): let the client build its URL.
        try:
            image = MediaItemImage.from_dict(raw["image"])
            try:
                url = mass.get_image_url(image, size=IMAGE_SIZE)
            except TypeError:
                url = mass.get_image_url(image)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("No URL for image %s", raw["image"], exc_info=True)
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
    # Filling every row takes a call per row, some to streaming services: reuse it for a while.
    cache = hass.data[DOMAIN].setdefault("cache", {})
    key = msg.get("config_entry_id")
    hit = cache.get(key)
    if hit and not msg["debug"] and time.monotonic() - hit[0] < CACHE_SECONDS:
        connection.send_result(msg["id"], hit[1])
        return
    try:
        folders = await mass.send_command("music/recommendations")
    except Exception as err:  # noqa: BLE001 - report Music Assistant's error to the caller
        connection.send_error(msg["id"], "music_assistant_error", str(err))
        return
    folders = [f for f in (_plain(f) for f in folders or []) if isinstance(f, dict)]
    # Rows can arrive empty; each is filled with its own call, all at once.
    contents = await asyncio.gather(*(_folder_items(mass, f) for f in folders), return_exceptions=True)
    rows = []
    errors = []
    for folder, raw_items in zip(folders, contents):
        if isinstance(raw_items, BaseException):
            errors.append({"folder": folder.get("item_id"), "path": folder.get("path"), "error": repr(raw_items)})
            continue
        items = [i for i in (_item(hass, mass, _plain(raw)) for raw in raw_items) if i][:ROW_LIMIT]
        if not items:
            continue
        rows.append({
            "id": folder.get("item_id"),
            "provider": folder.get("provider"),
            "name": folder.get("name"),
            "subtitle": folder.get("subtitle"),
            "translation_key": folder.get("translation_key"),
            "icon": folder.get("icon"),
            "enabled_by_default": folder.get("enabled_by_default", True),
            "items": items,
        })
    result: dict[str, Any] = {"rows": rows}
    if rows:
        cache[key] = (time.monotonic(), result)
    if msg["debug"]:
        result["debug"] = _shape(folders) | {
            "paths": [{"id": f.get("item_id"), "path": f.get("path"), "uri": f.get("uri"), "enabled_by_default": f.get("enabled_by_default")} for f in folders],
            "errors": errors,
        }
    connection.send_result(msg["id"], result)


async def _folder_items(mass, folder: dict[str, Any]) -> list[Any]:
    """A row's items. Older Music Assistant versions send them with the row; 2.10+ sends rows
    empty and serves each row's items from ``music/recommendations/items``."""
    if folder.get("items"):
        return list(folder["items"])
    provider, item_id = folder.get("provider"), folder.get("item_id")
    if not provider or not item_id:
        return []
    try:
        result = await mass.send_command("music/recommendations/items", provider=provider, item_id=item_id)
    except Exception:  # noqa: BLE001 - versions without the command: the provider's browse route
        _LOGGER.debug("music/recommendations/items failed for %s; browsing instead", item_id, exc_info=True)
        result = await mass.send_command("music/browse", path=f"{provider}://recommendations/{item_id}")
    if isinstance(result, dict):  # paged results
        result = result.get("items") or []
    return list(result or [])


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


# --- Queue ------------------------------------------------------------------------------------
# A queue's id is its player's id; Home Assistant shows it as the player's `active_queue`.


def _ready(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]):
    """The Music Assistant client, or None after sending the caller an error."""
    if DOMAIN not in hass.data:
        connection.send_error(msg["id"], "not_loaded", "Music Assistant Bridge isn't set up")
        return None
    mass = _mass(hass)
    if mass is None:
        connection.send_error(msg["id"], "not_found", "Music Assistant isn't loaded")
    return mass


def _queue_state(queue: Any) -> dict[str, Any] | None:
    queue = _plain(queue)
    if not isinstance(queue, dict):
        return None
    keys = ("queue_id", "display_name", "items", "current_index", "shuffle_enabled", "repeat_mode", "crossfade_enabled")
    state = {k: queue.get(k) for k in keys}
    # Called dont_stop_the_music_enabled before Music Assistant 2.10.
    state["autoplay_enabled"] = queue.get("autoplay_enabled", queue.get("dont_stop_the_music_enabled"))
    return state


def _queue_item(hass: HomeAssistant, mass, raw: Any) -> dict[str, Any] | None:
    raw = _plain(raw)
    if not isinstance(raw, dict):
        return None
    media = _plain(raw.get("media_item"))
    track = _item(hass, mass, media) if isinstance(media, dict) else None
    image = (track or {}).get("image")
    if not image and isinstance(raw.get("image"), dict):
        image = _image(hass, mass, {"image": raw["image"]})
    return {
        "queue_item_id": raw.get("queue_item_id"),
        "index": raw.get("index"),
        "name": raw.get("name"),
        "duration": raw.get("duration"),
        "available": raw.get("available", True),
        "track": track,
        "image": image,
    }


async def _send(connection: websocket_api.ActiveConnection, msg: dict[str, Any], mass, command: str, **kwargs) -> tuple[bool, Any]:
    """Run a Music Assistant command; on failure tell the caller and return (False, None)."""
    try:
        return True, await mass.send_command(command, **kwargs)
    except Exception as err:  # noqa: BLE001 - report Music Assistant's error to the caller
        connection.send_error(msg["id"], "music_assistant_error", str(err))
        return False, None


@websocket_api.websocket_command({vol.Required("type"): "ma_bridge/queue", vol.Required("queue_id"): str})
@websocket_api.async_response
async def ws_queue(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A queue's settings and position: length, current index, shuffle, repeat, autoplay, crossfade."""
    if (mass := _ready(hass, connection, msg)) is None:
        return
    ok, queue = await _send(connection, msg, mass, "player_queues/get", queue_id=msg["queue_id"])
    if ok:
        connection.send_result(msg["id"], {"queue": _queue_state(queue)})


@websocket_api.websocket_command({
    vol.Required("type"): "ma_bridge/queue_items",
    vol.Required("queue_id"): str,
    vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
    vol.Optional("limit", default=20): vol.All(int, vol.Range(min=1, max=100)),
})
@websocket_api.async_response
async def ws_queue_items(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A page of the queue, in play order."""
    if (mass := _ready(hass, connection, msg)) is None:
        return
    ok, items = await _send(connection, msg, mass, "player_queues/items", queue_id=msg["queue_id"], limit=msg["limit"], offset=msg["offset"])
    if ok:
        out = []
        for n, raw in enumerate(items or []):
            if item := _queue_item(hass, mass, raw):
                item["index"] = msg["offset"] + n  # Music Assistant leaves QueueItem.index at 0
                out.append(item)
        connection.send_result(msg["id"], {"items": out})


ITEM = vol.Any(str, int)  # a queue_item_id, or an index in the queue


@websocket_api.websocket_command({vol.Required("type"): "ma_bridge/queue_play", vol.Required("queue_id"): str, vol.Required("item"): ITEM})
@websocket_api.async_response
async def ws_queue_play(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Jump to an item in the queue."""
    if (mass := _ready(hass, connection, msg)) is None:
        return
    ok, _ = await _send(connection, msg, mass, "player_queues/play_index", queue_id=msg["queue_id"], index=msg["item"])
    if ok:
        connection.send_result(msg["id"], {})


@websocket_api.websocket_command({vol.Required("type"): "ma_bridge/queue_remove", vol.Required("queue_id"): str, vol.Required("item"): ITEM})
@websocket_api.async_response
async def ws_queue_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Take an item out of the queue."""
    if (mass := _ready(hass, connection, msg)) is None:
        return
    ok, _ = await _send(connection, msg, mass, "player_queues/delete_item", queue_id=msg["queue_id"], item_id_or_index=msg["item"])
    if ok:
        connection.send_result(msg["id"], {})


@websocket_api.websocket_command({
    vol.Required("type"): "ma_bridge/queue_set",
    vol.Required("queue_id"): str,
    vol.Optional("autoplay"): bool,
    vol.Optional("crossfade"): bool,
})
@websocket_api.async_response
async def ws_queue_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Switch the queue's Autoplay ("don't stop the music") and Crossfade; returns the queue."""
    if (mass := _ready(hass, connection, msg)) is None:
        return
    queue_id = msg["queue_id"]
    if "autoplay" in msg:
        try:
            await mass.send_command("player_queues/autoplay", queue_id=queue_id, autoplay_enabled=msg["autoplay"])
        except Exception:  # noqa: BLE001 - Music Assistant before 2.10 only has the old name
            ok, _ = await _send(connection, msg, mass, "player_queues/dont_stop_the_music", queue_id=queue_id, dont_stop_the_music_enabled=msg["autoplay"])
            if not ok:
                return
    if "crossfade" in msg:
        ok, _ = await _send(connection, msg, mass, "player_queues/crossfade", queue_id=queue_id, crossfade_enabled=msg["crossfade"])
        if not ok:
            return
    ok, queue = await _send(connection, msg, mass, "player_queues/get", queue_id=queue_id)
    if ok:
        connection.send_result(msg["id"], {"queue": _queue_state(queue)})


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
