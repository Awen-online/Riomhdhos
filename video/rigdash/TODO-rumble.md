# Later: Rumble

Researched 2026-09-27. **Verdict: add it.** Cheapest destination on the remaining list and
the only genuine drop-in — static paste-once key, 16:9 native, video spec passes with
headroom, simulcasting permitted, and a chat API that needs no OAuth at all.

## Why it is cheap

- **The stream key is static and never rotates.** One line in `keys.env`, no pre-show step.
  Set up at Rumble's Static Stream Key page (help page dated 02 May 2026), resettable on
  demand. Rumble Studio's Direct RTMP room gives the same thing and can be left open
  indefinitely. Ignore every third-party guide describing the older per-broadcast flow of
  creating a stream and copying a key each time — it still exists and is not what we want.
- **A live stream template can be attached to the key**, so Rumble creates the broadcast
  itself with a saved title and category when the encoder connects. Going live needs no
  browser. That is the "a line in a config file, not another app to babysit" property.
- **16:9 native** (1920x1080 / 1280x720, square pixels), so it joins the existing
  `-c copy` fan-out at zero cost. No second encode, unlike TikTok and Instagram.
- **Free.** Key streaming, static key, Direct RTMP and the chat API all cost nothing.

## The one real risk: audio is nominally out of spec

Rumble's published boundaries (help page dated 25 Aug 2026) against what we send:

| | Rumble | Ours | |
|---|---|---|---|
| Video bitrate | max 8,000 kbps | 6,000 | ✅ 25% headroom |
| Resolution | 1920x1080 | 1920x1080 | ✅ |
| Video codec | H.264 L4.1@30 / L4.2@60 | H.264 (AMD AMF) | ✅ |
| Keyframe interval | 2 s | **unconfirmed — see below** | ⚠️ |
| Audio codec | AAC | AAC | ✅ |
| **Audio bitrate** | **128 kbps** | **160 kbps** | ❌ 32 over |
| **Audio sample rate** | **44.1 kHz** | **48 kHz** | ❌ |

**This is the same shape of problem that disqualified LinkedIn, but it is not fatal here,
and the difference matters.** LinkedIn states a hard ceiling and is reported to refuse the
connection. Rumble's wording is a quality warning — settings outside the boundaries *"may
result in an unstable and disrupted stream"* — and the only field report found (OBS forums,
2023) is of Rumble transcoding down rather than rejecting. No primary statement exists
either way about audio specifically.

**If it does refuse, the fix is cheap and does not break the design:** re-encode audio only
on the Rumble leg, `-c:v copy -c:a aac -b:a 128k -ar 44100`. Video still passes through
untouched, so there is no second video encode and the one-encode property survives. Near
zero CPU. That asymmetry is exactly why Rumble is a caveat and LinkedIn was a no.

**Settle it with one throwaway stream before the first real show.** Rumble's own docs warn
that testing on the main channel leaves a public recording.

## Simulcasting is permitted

Rumble's general terms (01 Sep 2026) do contain exclusivity language, but it attaches to
video-management **Agency options A and B** — the syndication products for uploaded files,
where Rumble becomes exclusive worldwide agent. Options C (Rumble Only) and D (Personal
Use) are non-exclusive. Livestreams take C or D, so the exclusivity clauses are
structurally out of reach. No anti-simulcast clause appears in the general terms, the
Creator Program T&Cs (13 Feb 2026), or the Creator Program help page. Rumble evidently
built for it: the static key exists so creators can stream to Rumble while restreaming
elsewhere, and Rumble Studio itself multistreams to custom RTMP destinations.

⚠️ **The one conflict is in monetization, not in streaming.** The Creator Program requires
≥5 hours a month of *Rumble Premium–exclusive* content, which by definition cannot be
simulcast. If the programme is ever pursued, plan that as a deliberate non-fanned-out block
rather than discovering it later. Enrolment also wants ≥100 followers, active Rumble
Premium maintained throughout, and ≥1 hour streamed via Rumble Studio in the prior 30 days.

The historic Rumble exclusivity deals (Bongino and similar) are individually negotiated
contracts with named creators, not account terms. They do not apply.

## Gate to stream at all

One of: **phone verification** (free, one-off), 5 followers, or active Rumble Premium. No
account age, no approval queue, no ID check. Phone verification is the path.

## Chat: the easiest on the board, easier than Twitch or YouTube

**Rumble Live Stream API**, v1.1, help page dated 20 Nov 2025.

- `https://rumble.com/-livestream-api/get-data?key=<KEY>`, keys at
  `rumble.com/account/livestream-api`.
- ⚠️ **No authentication. The URL *is* the credential** — it embeds the user id and key, so
  it must be handled exactly like a stream key: config file only, never logged, never
  pasted into chat.
- **Polling only.** Plain HTTPS GET returning JSON. No websocket, no SSE, no webhook,
  despite Rumble describing it as real-time.
- Chat fields: `latest_message`, `recent_messages` (username, badges, text, created_on),
  plus rants with amounts. **Capped at 50 results total.**
- The same poll also returns live status, title, categories, viewer count, likes,
  followers and subscribers — so one request feeds both the destination row and the chat
  tab. Empty while off air.
- **Read-only**, which fits the CHAT tab exactly.

⚠️ **Honesty constraint for the UI when this gets built:** with a 50-result cap and no
cursor, a fast chat between polls is silently truncated. A poll that comes back with
exactly 50 is the detectable signal that messages were probably dropped, and the tab has to
say so rather than quietly showing an incomplete log. Start polling at 5–10 s (community
libraries default to 10; Rumble documents no rate limit) and back off on non-200s.

## Metadata

**No write API.** The live stream template attached to the static key sets title and
category automatically at connect, which covers the normal case. Per-show titles mean
editing the saved template in the web UI or accepting a generic one. Not scriptable.

## What has to happen, and who does it

1. **Ian, in the Rumble account:** verify phone, create the static stream key, attach a
   live stream template, read the ingest hostname off the Encoder Details panel, and
   create a Live Stream API key. None of this is visible from outside the account.
2. **AWEN session:** `RUMBLE_ENABLED` / `RUMBLE_KEY` / `RUMBLE_INGEST` in `keys.env` plus a
   destination in `push.ps1`, following the `TWITCH_INGEST` precedent. Not config-only —
   `push.ps1` hardcodes its three platforms.
3. **This session:** nothing for the destination row, which is discovered generically. The
   chat reader is a new source alongside Twitch, and is genuinely easy — one timed GET and
   a dedupe on `created_on` + `username` + `text`.

## How much of this to trust

**Primary source:** the static key being non-rotating; the full encoder boundary table and
its exact wording; the Live Stream API's existence, version, no-auth model, field list and
50-result cap; Creator Program eligibility and the 5 Premium-exclusive hours; the Agency
A/B versus C/D exclusivity split.

**Inference, and it is the load-bearing one:** that livestreams can only take options C or
D. Rumble never enumerates which licences a livestream may take; the reasoning is sound
because A and B are syndication products for uploaded files, but the simulcast verdict
rests on it. Also inferred: that Rumble degrades rather than refuses, from soft wording
plus one 2023 forum post.

**Could not verify at all:**
- **RTMPS.** Rumble documents only "RTMP" and no source offers an `rtmps://` endpoint.
  Assume the stream key crosses the network in the clear until the account UI says
  otherwise — which matters for the venue-laptop case, not at home.
- **The ingest hostname.** Visible only in-account under Encoder Details.
- Regional endpoints; API rate limits; whether 160 kbps / 48 kHz AAC is actually accepted;
  whether a Direct RTMP room counts as "via Rumble Studio" for Creator Program enrolment;
  whether the static key section is behind a beta flag on any given account.

## Open questions only answerable from inside the account

- What does Encoder Details show — hostname, and `rtmp://` or `rtmps://`?
- Is the Static Stream Key section present, or gated?
- Does 160 kbps / 48 kHz AAC connect and stay up? One throwaway stream settles it.
- Is the Live Stream API key per-account or per-channel?
