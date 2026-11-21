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
otherwise. Returns:

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

## Image proxy

`GET /api/ma_bridge/image/<signature>?url=<encoded url>` needs no login: the signature (an
HMAC with a secret stored in the config entry) is what allows it, so links can sit in `<img>`
tags. Responses are cached by browsers for a week.
