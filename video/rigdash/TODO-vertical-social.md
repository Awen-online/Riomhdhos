# Later: X, LinkedIn, Instagram, TikTok

Researched 2026-09-26. Horizontal (YouTube / Twitch / Facebook) works first and gets
boring before any of this starts.

## The one architectural fact

**X and LinkedIn are 16:9 and join the existing `-c copy` fan-out cleanly. Instagram and
TikTok are both 9:16 and neither will.** A vertical output is a genuinely separate
composition — not a crop, a different layout — so it needs a second OBS canvas (Aitum
Vertical) and a second encode. That breaks the "one encode, N copies" property the relay
depends on, and it is the single biggest piece of work here. Treat Instagram and TikTok as
one combined project, never as two more rows in `keys.env`.

## Ranked by effort to value

### 1. X — do this first
The only one with a **persistent, paste-once key**, so it drops into `keys.env` with no new
machinery at all. 16:9 native. Bitrate ceiling ~40 Mbps against our 6 — enormous headroom.

- **Cost: X Premium, $8/mo web.** Basic is not enough. Account must be public.
- Key comes from an **RTMP source** created in Live Studio (`studio.x.com`, launched 1 Jul
  2026) or the older Media Studio Producer. Sources are reusable across broadcasts.
- X documents a **3-second keyframe** where everything else here wants 2. Shorter is safe,
  so our single OBS GOP setting is fine — just know the docs differ.
- Broadcasts are cut at 24 hours. No H.265 on ingest.
- A rebuilt Livestream API shipped 22–25 Sep 2026 but is approval-gated and its public docs
  are not up yet. **Irrelevant to us** — the persistent key means we never need it.

### 2. LinkedIn — free, but a manual step before every single broadcast
Free, and the 150-follower / 30-day bar is trivial. 16:9, no second encode.

- ⚠️ **Since 22 June 2026 you cannot go live spontaneously.** Every broadcast must hang off
  a scheduled LinkedIn Event.
- ⚠️ **The key only exists 1–2 hours before the scheduled start** (2 h for verified Pages,
  1 h otherwise) and is per-broadcast. So: create an Event, wait, fetch a fresh URL+key,
  inject it. Unautomatable — the Live Events API is partner-only, requires a certification
  video and background verification, and its docs are stale enough to predate the June 2026
  change.
- ⚠️ **Our current encoder settings violate LinkedIn's spec.** Their documented ceilings:
  **6 Mbps video (we are exactly at it, zero headroom), 128 kbps audio (we send 160),
  1080p max, 30 fps max, 2 s keyframe, Baseline profile recommended.** LinkedIn is reported
  to refuse the connection outright rather than degrade. A LinkedIn leg needs its own
  encode, or the whole rig drops to fit.
- Cannot stream to a profile and a Page simultaneously — matters with three brands.
- Audience fit is the real question. **Sync.Land is the only brand where this obviously
  makes sense**; a live set on a professional-network feed is an odd match otherwise.

### 3. TikTok — highest audience value, and the stream key does not exist
🚩 Second vertical encode. No API of any kind for going live.

⚠️ **Answered by hand 2026-09-26: there is no stream key.** Going live offers only the
LIVE Studio desktop app. That settles the architecture — **TikTok cannot be a relay
destination**, now or until the encoder permission appears. The relay pushes RTMP to a URL
with a key; LIVE Studio has no RTMP ingest, so there is nothing to push to. Do not add a
TikTok row to `keys.env`; there is no key to put in it.

**What still works, and why the big piece of work is unchanged.** LIVE Studio captures a
window, a display or a camera and does its own encode. So the vertical composition — the
expensive part — is needed either way, and only the last mile differs: instead of an RTMP
leg, LIVE Studio window-captures the vertical canvas. If the encoder permission ever
appears, the last mile swaps to a relay leg with no rework to the composition.

Three costs to go in with eyes open:
- **Riastrad goes blind.** LIVE Studio is outside MediaMTX, so the dashboard cannot arm it,
  disarm it, or tell you it dropped. It cannot be a row in the STREAM tab, and it must not
  be given one — a control that cannot act has no business looking operable.
- **Virtual-camera slots are already taken.** OBS Virtual Camera is the Pixel 6 bridge and
  Unity Video Capture is the Pixel 8, so LIVE Studio cannot be fed that way without
  evicting a phone. Feed it an OBS *windowed projector* of the vertical scene instead.
  (Whether Aitum Vertical exposes a projector of its own canvas is unconfirmed.)
- **A third encoder on a box already decoding two phone H.264 streams.** Unmeasured.

- Two separate gates: mobile LIVE (≈1,000 followers, 18+, 30-day-old account) **and a
  distinct, undocumented permission for third-party encoder / stream-key access.** Having
  LIVE on the phone does not mean a stream key exists. Some reports say it is unlocked only
  by joining a creator network.
- ⚠️ **Credible reports that a raw OBS/ffmpeg push gets dropped or visibility-restricted**,
  because TikTok LIVE Studio sends extra stream-side metadata that a bare `-c copy` leg does
  not. Unverified against TikTok, but it means TikTok may not behave as a plain RTMP sink.
- **Check by hand before committing any work: does Stream Settings actually show a key?**

### 4. Instagram — lowest priority, possibly not available at all
🚩 Second vertical encode. Per-broadcast key ("the stream key is not static, and will
refresh each time"). **No API, and Meta has said it is not building one.**

- Two stacked gates: a hard **1,000-follower + public account** floor imposed 1 Aug 2025,
  and Live Producer's own "limited access at this time" status — wording Meta's page has
  carried since 2022 and never updated. He may simply not have the option, with no appeal.
- Sending 1920×1080 gets **centre-cropped and zoomed** to 9:16, which destroys the framing.
- **Check by hand: does Live Producer appear on instagram.com?**
- If the vertical work happens for TikTok anyway, Instagram is a cheap rider on it.

## Simulcasting is not a problem

Twitch dropped its broad simulcast restriction in October 2023; Affiliates and Partners may
stream anywhere concurrently. Nothing at X, LinkedIn, Instagram or TikTok restricts it.
Fanning out to all seven is fine.

## How much of this to trust

- **X:** `help.x.com` returned 403 to every automated fetch, so the X claims come from
  secondary sources dated 2026. The required Premium tier is contested — one source claims
  $3 Basic suffices, most say $8 Premium and a verified account. Confirm at signup.
- **Instagram:** Meta's own Live Producer page is from Nov 2022 and unchanged; general
  availability could not be confirmed from a primary source.
- **TikTok:** publishes almost nothing. One guide claims 10,000 followers for non-gaming
  RTMP access; that could not be corroborated and every other source says 1,000. The
  existence of a second undocumented gate is well attested; its threshold is not.
- **LinkedIn:** encoder limits and access criteria are from LinkedIn's own help pages and
  are solid. The API documentation is stale (last updated Dec 2023).

## Open questions for Ian

- ~~Does the TikTok account show a stream key?~~ **Answered 2026-09-26: no.** The open
  question is now whether the encoder permission can be requested at all, or only waited for.
- Does Live Producer appear on instagram.com for the account in question?
- Is vertical a *crop* of the same performance or a separately framed shot? That is a
  camera decision before it is a software one, and the Pixel 6 is already on a WiFi bridge
  that could be pointed differently.
