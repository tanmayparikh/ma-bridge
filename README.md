# Music Assistant Bridge

A small Home Assistant integration that gives dashboards two things the built-in
Music Assistant integration doesn't:

- **Home-page rows.** Music Assistant's home page shows rows such as Quick picks, Listen again
  and mixes, which come from your music providers. Home Assistant doesn't pass them through;
  this does, as a websocket command.
- **Artwork that loads anywhere.** Music Assistant serves artwork from its own server, usually
  plain HTTP on the local network. Dashboards served over HTTPS, or opened away from home,
  can't load it. The bridge proxies it through Home Assistant with signed links.

It reuses the Music Assistant integration's connection, so it has no settings.

## Install

1. Copy `custom_components/ma_bridge` into your Home Assistant `config/custom_components`
   folder (or in HACS: Custom repositories → `https://github.com/tanmayparikh/ma-bridge`, type Integration).
2. Restart Home Assistant.
3. Settings → Devices & services → Add integration → **Music Assistant Bridge**.

Needs the Music Assistant integration set up and loaded.

## Websocket API

Any logged-in user can call these.

### `ma_bridge/recommendations`

```json
{ "type": "ma_bridge/recommendations" }
```

Optional `config_entry_id` picks a Music Assistant instance; the first loaded one is used
otherwise. `"debug": true` adds a `debug` key describing what Music Assistant sent back (folder
names, item counts and a couple of sample items), for when rows come back empty. Returns:

```json
{
  "rows": [
    {
      "id": "listen_again", "provider": "ytmusic--abc", "name": "Listen again",
      "subtitle": null, "translation_key": null, "icon": null,
      "items": [
        {
          "media_type": "playlist", "uri": "ytmusic--abc://playlist/PL123", "name": "Car Jams",
          "version": "", "artists": [], "album": null,
          "image": "/api/ma_bridge/image/<signature>?url=..."
        }
      ]
    }
  ]
}
```

Play an item with `music_assistant.play_media` using its `uri` and `media_type`. Folders that
can only be browsed (not played) are left out, and so are rows that end up empty.

### `ma_bridge/images`

```json
{ "type": "ma_bridge/images", "urls": ["http://192.168.1.5:8095/imageproxy/..."] }
```

Returns `{ "urls": [...] }` in the same order: HTTPS URLs unchanged, Music Assistant server
URLs as proxied links, and `null` for anything else. Only Music Assistant's own server is ever
proxied.

### Queue

A queue's id is its player's id, which Home Assistant shows as the player's `active_queue`
attribute (for a Music Assistant group, the group's id).

| Command | Fields | Returns |
|---|---|---|
| `ma_bridge/queue` | `queue_id` | `{ queue }`: `items` (length), `current_index`, `shuffle_enabled`, `repeat_mode`, `autoplay_enabled`, `crossfade_enabled` |
| `ma_bridge/queue_items` | `queue_id`, `offset` (0), `limit` (20, max 100) | `{ items }`: `queue_item_id`, `index`, `name`, `duration`, `available`, `track` (as in recommendations), `image` |
| `ma_bridge/queue_play` | `queue_id`, `item` (queue_item_id or index) | `{}` |
| `ma_bridge/queue_remove` | `queue_id`, `item` (queue_item_id or index) | `{}` |
| `ma_bridge/queue_set` | `queue_id`, `autoplay` (bool, optional), `crossfade` (bool, optional) | `{ queue }`, as above |

Autoplay is Music Assistant's "don't stop the music": when the queue runs out it keeps going
with similar tracks.

## Image proxy

`GET /api/ma_bridge/image/<signature>?url=<encoded url>` needs no login: the signature (an
HMAC with a secret stored in the config entry) is what allows it, so links can sit in `<img>`
tags. Responses are cached by browsers for a week.
