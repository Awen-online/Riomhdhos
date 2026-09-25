# Later: Instagram, TikTok, and the vertical path

Deferred on purpose, 2026-09-25. Horizontal (YouTube / Twitch / Facebook) works first and
gets boring before any of this starts.

## Why these two are not just two more entries in `keys.env`

Adding a horizontal destination is three lines in `push.ps1` and two in `keys.env`. IG and
TikTok are not that, for two reasons that have nothing to do with the relay.

**They are 9:16.** A vertical output is a *second encode* — there is no arrangement that
avoids it, because the relay is deliberately `-c copy` and copying cannot reframe. So this
is the real work, and it is shared by both platforms.

**Neither gives a stable key.** Both hand out a per-session ingest URL and key that you
fetch immediately before going live:

- **Instagram** — Live Producer issues an RTMP URL plus key per session. The Instagram
  Live API is effectively closed to ordinary apps, so assume manual retrieval each time.
  `connect.py` cannot help here the way it does for Facebook, even though both are Meta.
- **TikTok** — LIVE via RTMP requires the account to have been granted LIVE access, and
  the key comes from the LIVE dashboard per session. The TikTok Live API is partner-gated.
  Check the account actually has RTMP LIVE before building anything.

So the shape is: the *vertical path* is engineering, and the *keys* are a manual step that
should be made as painless as possible rather than automated away.

## The vertical path

Recommended, and the reasoning is in the audit: a second OBS canvas via the **Aitum
Vertical** plugin, feeding a **second MediaMTX path** (`vertical`), with its own pushers.

- Keeps the relay dumb and `-c copy` on both paths.
- Keeps framing decisions in OBS, where they are visible. A relay-side AMF crop is less to
  build, but you are blind to what it frames and it spends GPU already carrying the
  horizontal encode.
- Costs a second encode either way. Budget for it: the horizontal is 6000 kbps on an
  RX 9060 XT via AMF, and upload measured ~90 Mbps, so headroom is not the constraint.

## What has to change

- `mediamtx.yml` — a second path, `vertical`, with its own `runOnReady`.
- `push.ps1` — currently one `if` block per platform (`YOUTUBE_ENABLED`, `TWITCH_ENABLED`,
  `FACEBOOK_ENABLED`). Adding two more works, but at five it is worth making the
  destination table data rather than code, keyed by name with `{ingest, path}`.
- `keys.env` — `INSTAGRAM_*`, `TIKTOK_*`, each also naming which path it reads
  (`horizontal` / `vertical`).
- OBS — Aitum Vertical installed and a 9:16 scene collection that is not an afterthought.
  Framing a vertical crop of a horizontal set badly is worse than not going vertical.

## What does NOT have to change

The STREAM tab already renders whatever destinations the relay reports and derives health
per destination from its own progress file. Two more rows need no UI work. Verify rather
than assume, but that was the design intent.

## Acceptance

Not "it connected once". Both vertical destinations feeding for a full set, each
recoverable on its own, with the horizontal three unaffected throughout — and a deliberate
kill of one vertical destination showing `stalled` in the tab within ~5s and then
recovering by itself.

## Open questions for Ian

- Does the TikTok account have RTMP LIVE access granted? Everything else is moot until so.
- Is vertical a *crop* of the same performance, or a separately framed shot? If separate,
  that is a camera decision before it is a software one, and the Pixel 6 is already on a
  WiFi bridge that could be pointed differently.
