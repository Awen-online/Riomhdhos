# Facebook Live for the stream-relay rig — verified runbook

Research date: **2026-09-27**. All Meta URLs below were fetched on that date. Meta's developer
docs carry no visible "last updated" stamps, so "as of 2026-09-27" is the strongest date claim
available for most of them; where a page states its own effective date I have quoted it.

---

## Verdict

**Ten minutes of clicking, not weeks of review — but your belief is half right, and the half
that is wrong is the half that matters.** Meta's App Review page states plainly: *"If your app
will only be used by app users who have a role on the app itself, App Review is not required."*
Standard Access is auto-granted to Business-type apps for every permission and feature they can
see, `publish_video` and the Live Video API feature included, and Business Verification attaches
only to **Advanced** Access. So the permissions half of your claim is verified. **But Development
Mode specifically poisons the output**: *"Any data generated while an app is in Development mode,
such as test posts, can only be seen by role users."* A live video broadcast by a dev-mode app is
visible only to people holding a role on the app — which is useless for a public stream. The fix
is cheap and is the whole trick of this runbook: flip the app to **Live mode** while leaving every
permission at **Standard Access** and submitting nothing. In Live mode the content is public, and
Standard Access still confines the *permissions* to role-holders — which is exactly right, since
the only person who will ever authorise this app is you. Nothing here requires App Review or
Business Verification. The two things that can still stop you are (a) whether Meta accepts
`http://localhost:8771/callback` as a redirect URI, which I could **not** settle from primary
sources and which you must test in two minutes, and (b) whether your existing app is of a type
that exposes Page permissions at all.

---

## What `connect.py` actually demands (read from the code, not assumed)

From `C:\Users\mccul\Awen\stream-relay\connect.py`, `facebook()` at line 290 and
`title_facebook()` at line 432:

| Thing | Value | Line |
|---|---|---|
| Graph version | `v21.0` (`GRAPH` constant) | 287 |
| Auth dialog | `https://www.facebook.com/v21.0/dialog/oauth` | 299 |
| Redirect URI | `http://localhost:8771/callback` (exact, port included) | 51 |
| `response_type` | **not sent** — relies on Facebook's `code` default | 299–301 |
| Scopes | `pages_show_list,pages_read_engagement,pages_manage_posts,publish_video` | 301 |
| Required env | `META_APP_ID`, `META_APP_SECRET` (hard `need()` check) | 292 |
| Optional env | `META_PAGE_NAME`, `META_LIVE_TITLE` (defaults to `"Live"`) | 312, 322 |
| Token flow | code → short-lived user token → `fb_exchange_token` → long-lived user token → `GET /me/accounts` → **Page** access token | 303–309 |
| Page pick | `want in p["name"].lower()` — case-insensitive **substring of the Page's display name** | 312–318 |
| Broadcast | `POST /{page_id}/live_videos` with `status=LIVE_NOW`, `title` | 323 |
| Key | `GET /{id}?fields=secure_stream_url`, then `ssu.split("/rtmp/", 1)[1]` | 328–330 |
| Writes | `keys.env` → `FACEBOOK_KEY`; `tokens.json` → `page_id`, `page_name`, `page_token`, `live_video_id` | 319–331 |
| `status` gate | Facebook counts as "app configured" iff `META_APP_SECRET` is non-empty | 580 |

Two consequences worth stating up front:

1. **Per-broadcast key, by construction.** `connect.py` creates a *new* `LiveVideo` object on
   every `connect.py facebook` and on every `golive` that cannot reuse a waiting one, and writes
   whatever key that object returns. It never asks for a persistent key and never could — there
   is no API parameter for one (see §5).
2. **It throws the host away.** Line 330 keeps only the part after `/rtmp/`. Whatever builds the
   ffmpeg destination must supply the RTMPS base itself. See the warning in §6.

---

## Runbook

### Branch A — first, find out which branch you are on (2 minutes)

1. Go to **https://developers.facebook.com/apps/** and open the app whose ID is in
   `META_APP_ID`. (You can read the ID off the dashboard and compare; do not paste the value
   anywhere else.)
2. Look at the top bar: it shows **App mode: Development / Live** and the app's **type**.
3. Left nav → **Use cases** (newer dashboards) or **App Review → Permissions and Features**
   (older). Search for `publish_video`.
   - **If `publish_video` and the three `pages_*` permissions are listed and show "Standard
     Access" / "Ready for testing"** → your existing app is fine. Go to step 4 and skip steps
     A1–A4.
   - **If they are absent, greyed out, or the app type is "None"/"Gaming"** → the app type does
     not expose Page permissions. **Create a fresh app** (steps A1–A4). You cannot change an
     app's type after creation. A fresh app means a new `META_APP_ID`.

**A1.** https://developers.facebook.com/apps/ → **Create app**.
**A2.** Use case: choose **"Other"** → app type **"Business"**. (Meta's app-types doc lists only
Consumer and Business; Business is the one described as *"for apps that help businesses,
creators, and organizations manage their presence"* — i.e. the Pages one.) Do **not** pick
Consumer: the access-levels doc notes Consumer apps must reach Live mode before Advanced Access,
and Business is the type Page permissions live under.
**A3.** Name it anything; attach a Business portfolio if prompted (this does **not** trigger
Business Verification — that is a separate, later step you are not doing).
**A4.** Note the new App ID. It replaces `META_APP_ID` in `oauth.env`.

### The actual setup

4. **Add the Facebook Login product.** Left nav → **Products → +** → **Facebook Login** → **Set
   up** → platform **Web** (not "Other" — you need the OAuth redirect field). If asked for a Site
   URL, put `http://localhost:8771/`.

5. **Register the redirect URI.** Left nav → **Products → Facebook Login → Settings**. In
   **Client OAuth Settings**:
   - **Client OAuth Login** → **Yes**
   - **Web OAuth Login** → **Yes**
   - **Use Strict Mode for redirect URIs** → leave **Yes** (Meta: *"Enabling Strict Mode is
     required for all apps."* It demands an exact match, which is fine — `connect.py` sends the
     identical string.)
   - **Valid OAuth Redirect URIs** → paste exactly, no trailing slash:
     ```
     http://localhost:8771/callback
     ```
   - **Enforce HTTPS** → leave at its default. Do not try to switch it off; for apps created
     after March 2018 it is not switchable, and the localhost question (§ below, and
     "how much to trust") is decided by Meta's server, not by this toggle.
   - **Save changes.** ⚠️ **If the dashboard refuses to save the `http://` URI, stop and read
     "The localhost trap" below before going further.**

6. **Add the permissions.** Left nav → **Use cases** → open your use case → **Permissions**
   tab → click **Add** on each of, exactly:
   - `pages_show_list`
   - `pages_read_engagement`
   - `pages_manage_posts`
   - `publish_video`

   Each should land in the **Standard Access** / "Ready for testing" column with no submission.
   **Do not click "Request Advanced Access" on any of them** — that is the path that demands
   Business Verification and App Review, and you do not need it.

   Note: Meta's Live Video API overview says publishing **to a Page** needs only
   `pages_manage_posts` + `pages_read_engagement`, and that `publish_video` is the *profile*
   permission. `connect.py` hardcodes all four in one scope string and you have said not to
   modify it, so all four must be present on the app or the OAuth dialog will reject the scope.

7. **Confirm the Live Video API feature.** Same **Use cases** screen → **Features** tab. You want
   **Live Video API** present at Standard Access. Per the access-levels doc, Business apps are
   *"automatically approved for Standard Access for all permissions and features available to
   their app type"*, so it should already be there. If it offers a "Request Advanced Access"
   button, ignore it.

8. **Confirm your app role.** Left nav → **App roles → Roles**. Your personal Facebook account
   must appear as **Administrator** (Developer or Tester also work). If you created the app it
   already does.

9. **Confirm your Page role.** On Facebook itself, your account must hold a Page role with
   content rights on the Page you intend to stream to — **Full control** or a task-based role
   including **Content**. `GET /me/accounts` only returns Pages you administer, so a missing Page
   role shows up as `connect.py`'s `"facebook: no Pages found for this login."` (line 311).

10. **⚠️ Flip the app to Live mode.** Top bar toggle: **App mode: Development → Live**. This is
    the step that makes your broadcasts publicly visible, and it is the step you did not have in
    your mental model. Meta may ask for a **Privacy Policy URL** and a **category** before it
    lets you flip; any reachable URL satisfies the field. Flipping to Live does **not** submit
    anything for review and does **not** require Business Verification. Your four permissions
    stay at Standard Access, which means they remain requestable only by role-holders — which is
    only ever you.

11. **Get the App Secret.** Left nav → **App settings → Basic** → **App secret** → **Show**.
    Copy it straight into `oauth.env` as the value of `META_APP_SECRET`. Do not echo it, do not
    paste it into a chat, do not commit it.

12. **Set `META_PAGE_NAME`.** See §8 — it is the Page's **display name** (or any unique
    substring of it), *not* the slug and *not* the numeric ID. You may also leave it blank and
    let `connect.py` prompt you with a numbered list (lines 315–318); that is the safest option
    if you are unsure of the exact display name.

13. **Set `META_LIVE_TITLE`** if you have not already — it is the fallback title used by
    `connect.py facebook`. (`golive --title` overrides it.)

14. **Run it.**
    ```
    python connect.py facebook
    ```
    Sign in, grant all four permissions, pick the Page. Expect
    `facebook: new live video created on "<Page>"; key saved to keys.env.`

15. **Enable it.** In `keys.env` set `FACEBOOK_ENABLED=1`. Confirm with
    `python connect.py status` (no network, never prints keys) — Facebook should read
    `ready: will receive the stream`.

16. **Before every stream**, run `python connect.py golive --title "..."`. The Facebook key in
    `keys.env` is per-broadcast (§5); a stale key will be refused. `golive` reuses a live video
    still in `UNPUBLISHED`/`SCHEDULED_*`/`PREVIEW` and otherwise mints a new one and rewrites
    `FACEBOOK_KEY` (lines 438–459), so re-running is always safe.

### The localhost trap

I could **not** find any Meta primary source that states whether `http://localhost` is accepted
as a Valid OAuth Redirect URI. What Meta *does* say (developers.facebook.com/docs/facebook-login/
security/, fetched 2026-09-27): *"Use HTTPS, instead of HTTP, as an internet protocol, because it
uses encryption"* and *"On October 6, 2018, all apps will be required to use HTTPS."* Neither
sentence mentions a loopback exemption, and neither the manual-flow guide, the web quickstart,
the test-apps page, the app-dashboard page nor the Login overview says anything about localhost
at all. So: **unverified, and you must test it.**

The test takes two minutes and needs no code:

1. Do step 5 above. If the dashboard **refuses to save** the `http://localhost:8771/callback`
   entry, that is your answer — Meta rejects it.
2. If it saves, run `python connect.py facebook`. The script prints the authorization URL
   before opening a browser (line 184–185). If Meta rejects the redirect you will get, on
   Facebook's own error page, **"URL Blocked: This redirect failed because the redirect URI is
   not whitelisted in the app's Client OAuth Settings."** That is unambiguous.

If it *is* blocked, you have a genuine problem, because the redirect string is pinned at
`connect.py:51` and you have asked me not to modify the file. Your options, in order of how
little they disturb the rig:

- **Re-check Strict Mode and exact match first.** A missing/extra trailing slash, a missing port,
  or `http` vs `https` all produce the same "URL Blocked". Character-for-character:
  `http://localhost:8771/callback`.
- **Try `https://localhost:8771/callback`** in the dashboard as well — registering both costs
  nothing. It will not help on its own (`connect.py` sends `http`), but it tells you whether the
  refusal is about the scheme or about the host.
- **Accept a one-line change** to `REDIRECT`/`PORT` — which you would have to authorise, since
  the same constant is what Twitch and Google are already registered against and changing it
  breaks both.

I want to be blunt that I am not able to tell you in advance which way this goes. It is the one
step in this runbook I could not verify.

---

## Encoder spec verdict

Primary source: **Live Video API reference**, developers.facebook.com/docs/live-video-api/
reference/, fetched 2026-09-27. Corroborated by the Help Centre page
facebook.com/help/1534561009906955 (same numbers for 1080p30, labelled "Recommended").

Meta's documented table:

| Profile | Resolution | Video bitrate range |
|---|---|---|
| 1080p @ 60 fps | 1920×1080 | 4,500–9,000 Kbps |
| **1080p @ 30 fps** | **1920×1080** | **3,000–6,000 Kbps** |
| 720p @ 60 fps | 1280×720 | 2,250–6,000 Kbps |
| 720p @ 30 fps | 1280×720 | 1,500–4,000 Kbps |
| 480p @ 30 fps | 854×480 | 600–2,000 Kbps |
| 360p | 640×360 | 400–1,000 Kbps |

- Video codec: *"H.264, Level 4.1 for up to 1080p 30 FPS"*, *"H.264, Level 4.2 for 1080p 60 FPS"*
- Keyframe: *"Key Frame Size — Recommended 2 seconds. Do not exceed 4 seconds."*
- Audio codec: *"AAC low complexity"*
- Audio sample rate: *"44.1kHz or 48kHz"*
- Audio bitrate: *"128 kbps (preferred) to 256 kbps (do not exceed)"*
- Channels: *"Stereo"*
- Protocol: *"RTMPS Streaming"*
- Session cap: **8 hours**, hard — *"Facebook will automatically end streams that reach their
  assigned duration limit (typically 8 or 12 hours). There is no grace period once the limit is
  reached."* (Help Centre, 2026-09-27.)

### Your rig: 1080p / 6000 kbps video / 160 kbps AAC / 48 kHz

**Inside spec on every axis — but with zero headroom on video bitrate, and only if you are at 30 fps.**

- **Video bitrate.** 6,000 Kbps vs a documented range of 3,000–6,000 at 1080p30. You are sitting
  *exactly* on the ceiling. 6000 ≤ 6000 → inside, by nothing. Any encoder overshoot on a complex
  scene puts you over a documented limit. If you want margin without re-encoding for anyone else,
  5,500 buys you 8% and costs you nothing visible.
- **Frame rate — check this.** The spec bifurcates at 30 fps and your brief does not say which
  you run. At **1080p30** you are inside (3,000–6,000) and H.264 **Level 4.1** is correct. At
  **1080p60** the range becomes 4,500–9,000 — 6,000 is comfortably inside, *more* headroom, not
  less — but you must set **Level 4.2**, not 4.1. Arithmetic: 1920×1080 = 120×68 = 8,160
  macroblocks per frame; ×30 fps = 244,800 MB/s against Level 4.1's MaxMBPS of 245,760 — inside
  by 0.4%. ×60 fps = 489,600 MB/s, which blows through 4.1 and needs 4.2 (MaxMBPS 522,240).
  Level 4.1's bitrate ceiling is 50,000 Kbps (62,500 for High profile), so 6,000 is nowhere near
  the level's bitrate limit — it is the *macroblock rate* that forces 4.2 at 60 fps, and OBS
  labelling the stream 4.1 while sending 60 fps is the kind of mismatch that produces a stream
  Facebook accepts and then renders badly.
- **Audio bitrate.** 160 kbps vs "128 kbps (preferred) to 256 kbps (do not exceed)".
  128 < 160 < 256 → **inside**, above preferred, well under the do-not-exceed. No action.
- **Sample rate.** 48 kHz is **explicitly listed** as acceptable ("44.1kHz or 48kHz"). No action.
  (Note the Help Centre page lists only 44.1 kHz; the developer reference is the more specific
  source and it permits 48. Your `-c copy` relay could not resample anyway.)
- **Keyframe interval.** Set OBS to **2 seconds**. This is the one setting the docs give a hard
  "do not exceed" on (4 s). A 2 s GOP also suits Twitch and YouTube, so there is no conflict for
  a single shared encode.
- **Codec.** AAC-LC and H.264 — standard OBS defaults. Fine.

### Refuse or transcode?

**Transcode / degrade, not refuse** — but I have no primary quote that says so in those words, so
treat this as inferred. What Meta does say is *"Applying these settings helps ensure high
reliability and avoids throttling or quality drops"* (Help Centre, 2026-09-27), which is the
language of degradation rather than rejection, and Facebook builds an adaptive ladder from the
ingest so it is transcoding regardless. The genuine hard limits in the documented wording are the
two "do not exceed" values (keyframe 4 s, audio 256 kbps) and the 8-hour session cap. **None of
this disqualifies the rig.** Unlike LinkedIn, Facebook does not fall outside your single encode.

### ⚠️ One RTMPS gotcha the spec implies and `connect.py` hides

Facebook ingest is **RTMPS on 443**, not plain RTMP. Two knock-on effects for the relay:

1. Your `ffmpeg -c copy` fan-out leg for Facebook must use an ffmpeg built with TLS
   (OpenSSL/GnuTLS/SChannel). Check with `ffmpeg -protocols | findstr rtmps`. A build without it
   fails at connect time with an unhelpful error.
2. `connect.py` line 330 stores **only the key**, discarding the host. Its comment asserts the
   base is `rtmps://live-api-s.facebook.com:443/rtmp/`, but Meta's own reference shows samples of
   `rtmps://rtmp-api.facebook...` and `rtmps://rtmp-pc.facebook.com:443/rtmp/`. If whatever
   assembles your destination URL has `live-api-s` hardcoded and Meta hands back a different
   ingest host, you will get a key that works and a URL that does not. **Worth printing
   `secure_stream_url`'s host once, by hand, on your first run and comparing.** I could not
   determine from the docs whether Meta guarantees a single stable ingest hostname.

---

## Chat / comments

**Your belief is correct and verified.** Meta's "Interacting with viewers" guide
(developers.facebook.com/docs/live-video-api/interact-with-viewers, fetched 2026-09-27) documents
exactly the endpoint you named:

```
GET https://streaming-graph.facebook.com/{live-video-id}/live_comments?access_token=...
GET https://streaming-graph.facebook.com/{live-video-id}/live_reactions?access_token=...
```

- **Transport: Server-Sent Events.** The guide describes these as the real-time alternative to
  polling; the polling fallback is `GET /{live-video-id}/comments` on the normal Graph host, and
  the Graph reference for live-video comments says *"The best practice for querying comments on a
  Live video is to continually poll for comments in the reverse chronological ordering mode."*
- **Fields returned:** `created_time`, `from` (name + id), `message`, `id`, plus `view_id` on the
  streaming variant.
- **Auth:** the **Page access token** — the same one `connect.py` already persists in
  `tokens.json` under `facebook.page_token`.
- **The live video ID is already there too.** `connect.py` writes `facebook.live_video_id` into
  `tokens.json` on every broadcast creation (lines 325–327, 456–458). Your dashboard's chat tab
  can read both out of `tokens.json` and needs no new OAuth flow and no change to `connect.py`.
- **Review needed:** none beyond what §Runbook already sets up. Reading engagement on a Page you
  administer is covered by `pages_read_engagement` at Standard Access, and the Live Video API
  feature is likewise Standard. The guide itself does not state a permission requirement, which
  is the one soft spot here.
- **Caveat I could not verify:** whether `from` is populated for all commenters. Meta has
  progressively restricted commenter identity on Page content without Advanced Access
  (`Page Public Content Access`). Expect the possibility of anonymised or ID-only authors on a
  Standard-Access app. This will not break the tab; it may make it less useful.

---

## How much of this to trust

### Verified, quoted from Meta primary sources (all fetched 2026-09-27)

| Claim | Source |
|---|---|
| *"If your app will only be used by app users who have a role on the app itself, App Review is not required."* | developers.facebook.com/docs/app-review |
| *"Permissions with Standard Access can only be requested from app users who have a role on the requesting app. Similarly, features with Standard Access are only active for app users who have a role on the app."* | /docs/graph-api/overview/access-levels |
| *"Business, Consumer, and Gaming apps are automatically approved for Standard Access for all permissions and features available to their app type."* | /docs/graph-api/overview/access-levels |
| *"Standard Access is intended for apps that will only be used by people who have roles on them, or used during app development…"* | /docs/graph-api/overview/access-levels |
| *"Business Verification is required to get Advanced Access."* (i.e. **Advanced only**; effective 1 Feb 2023) | /docs/graph-api/overview/access-levels |
| *"For apps that are not approved for a feature in Meta App Review, the feature is only active for app users who have a role on the app or a role in a business portfolio that has claimed the app."* | /docs/features-reference |
| *"Apps in Development mode can only request permissions from role users, and only permissions with standard or advanced access levels."* | /docs/development/build-and-test/app-modes |
| ***"Any data generated while an app is in Development mode, such as test posts, can only be seen by role users."*** and *"that data will be visible to non-role users once the app is switched to Live mode."* | /docs/development/build-and-test/app-modes |
| Page live video needs `pages_manage_posts` + `pages_read_engagement`; `publish_video` is the **profile** permission | /docs/live-video-api |
| 60-day-account and 100-follower gates (error subcodes 1363120 / 1363144) apply **only to user profiles, not Pages** | /docs/live-video-api/reference |
| Full encoder table, H.264 levels, AAC-LC, 44.1/48 kHz, 128–256 kbps audio, 2 s (max 4 s) keyframe, RTMPS | /docs/live-video-api/reference |
| 8-hour hard session cap, no grace period | facebook.com/help/1534561009906955 |
| `streaming-graph.facebook.com/{id}/live_comments` and `/live_reactions` SSE endpoints | /docs/live-video-api/interact-with-viewers |
| Persistent stream key exists as a **Live Producer UI** toggle; *"You may only have one live video at a time with a persistent stream key."* | facebook.com/help/587160588142067 |
| *"Enabling Strict Mode is required for all apps."* Redirect URI must be an exact match. | /docs/facebook-login/security |
| Graph **v21.0** released 2024-10-02, **available until 2027-01-21**; newest is v26.0 (2026-07-29) | /docs/graph-api/changelog |

### Inferred — well-supported but stitched from more than one page

- **"Live mode + Standard Access only = public broadcast, no review."** This is the load-bearing
  conclusion of the whole runbook and no single Meta page states it. It follows from three
  quoted facts: dev-mode data is role-users-only; Live-mode data is visible to everyone;
  Standard Access permissions are auto-granted and remain requestable by role-holders regardless
  of app mode. The app-modes page also says *"Apps in Live mode can request permissions from
  anyone, but only permissions approved through App Review"* — which reads restrictive but is
  about non-role users, and is qualified two lines later by *"Standard Access features are only
  active for role users."* I am confident in the synthesis. I cannot hand you one sentence that
  says it.
- **Facebook transcodes rather than refuses an out-of-spec ingest.** Supported by the "throttling
  or quality drops" wording and by the existence of an adaptive ladder. Not stated outright.
- **Business app type is the right choice.** The app-types page describes Business as the
  "manage their presence" type but does not enumerate which permissions each type exposes.

### Contradiction I found and did not fully resolve — read this

The feature page **/docs/features-reference/live-video-api** says, of the Live Video API feature:
*"Requires App Review"* and *"This permission or feature is only available with business
verification."* Taken alone that would sink this entire plan.

I believe those badges describe the **Advanced Access** path — they are the standard boilerplate
Meta stamps on every feature page, and they are directly contradicted by the access-levels page's
*"automatically approved for Standard Access for all permissions and features available to their
app type"* and by the App Review page's *"If your app will only be used by app users who have a
role on the app itself, App Review is not required."* The features-reference index page agrees
with me: unapproved features are *"only active for app users who have a role on the app"* — i.e.
they **are** active, for role-holders, without approval.

**But I am reading intent into a badge.** If step 6 or 7 of the runbook shows the Live Video API
feature as unavailable rather than Standard, this contradiction is the reason, and Business
Verification becomes mandatory — which is the "weeks" branch. You will know within five minutes
of opening the dashboard.

### Could not verify at all

1. **Whether Meta accepts `http://localhost:8771/callback`.** No Meta page I could reach —
   login security, manual flow, web quickstart, test apps, app dashboard, login overview,
   login use case — mentions localhost or loopback in any form. Only the general "use HTTPS"
   guidance and the 2018 HTTPS requirement. **Test it; do not assume.** This is the single
   likeliest thing to cost you the afternoon.
2. **Whether Meta's live ingest hostname is stable** (`live-api-s` vs `rtmp-api` vs `rtmp-pc`).
   Docs show at least two. Verify by eye on your first run.
3. **Exactly what the dashboard demands before it will flip an app to Live mode** in 2026.
   /docs/development/release confirms *why* you flip but lists no checklist. Expect a Privacy
   Policy URL and a category prompt; I cannot promise that is all.
4. **How long an unstreamed `LIVE_NOW` live video stays valid** before its key dies. Not
   documented anywhere I could find. Practical mitigation: run `golive` shortly before you
   stream, which the workflow already does.
5. **Whether commenter identity (`from`) is populated at Standard Access.** See §Chat.
6. **Whether any of these doc pages changed recently.** Meta publishes no last-modified dates on
   developer docs. Everything above is "as served on 2026-09-27".

### Notes on the code, offered not acted on (I changed nothing)

- **Graph v21.0 expires 2027-01-21.** Not urgent today, hard-stops in under four months.
  `GRAPH` at line 287 is a one-line bump when you choose to do it. Note v24.0+ removed
  `overlay_url`; nothing `connect.py` uses.
- The OAuth dialog call omits `response_type` and relies on Facebook's `code` default, and the
  Facebook path uses no PKCE (unlike the YouTube path). Both are legitimate for a
  confidential client holding an app secret. Not a defect.
- Page access tokens obtained from `/me/accounts` **with a long-lived user token** do not expire,
  which is why there is no Facebook refresh path. They die if you change your password, revoke
  the app, or lose the Page role. If Facebook starts failing with an auth error long after setup,
  re-run `python connect.py facebook`.

---

## Answers to the numbered questions

**1. Is the Development Mode behaviour real and current in 2026?** **Yes, for permissions.**
*"Apps in Development mode can only request permissions from role users, and only permissions
with standard or advanced access levels"* (/docs/development/build-and-test/app-modes) plus
*"If your app will only be used by app users who have a role on the app itself, App Review is not
required"* (/docs/app-review). Both fetched 2026-09-27. Your recollection that the person must
hold both an app role and a Page role is correct — the app role is what Meta's docs require, and
the Page role is required independently by `/me/accounts`, which only returns Pages you
administer.

**2. Does it cover `publish_video` specifically?** **Yes.** The mechanism is not
permission-by-permission: *"Business, Consumer, and Gaming apps are automatically approved for
Standard Access for **all** permissions and features available to their app type."* `publish_video`
is such a permission and the **Live Video API** is such a feature, and the features-reference
index confirms unapproved features stay active for role-holders. Worth knowing: for a **Page**
broadcast Meta documents only `pages_manage_posts` + `pages_read_engagement` as required —
`publish_video` is the *profile* permission. `connect.py` asks for it anyway, so it must be
present on the app, but it is not the thing doing the work.

**3. Does Development Mode restrict the resulting live video?** **Yes, fatally — this is where
your model was wrong.** *"Any data generated while an app is in Development mode, such as test
posts, can only be seen by role users."* A broadcast created by a dev-mode app is visible only to
people with a role on the app. The good news: *"that data will be visible to non-role users once
the app is switched to Live mode"*, and flipping to Live mode requires neither App Review nor
Business Verification. **Do development in Development mode; flip to Live before you stream to
actual humans.** Step 10.

**4. Does Business Verification or Advanced Access get required anywhere?** **No, not on this
path.** *"Business Verification is required to get Advanced Access"* — Advanced Access is what
you would need to let *other people* authorise your app, and you never will. Standard Access is
auto-granted and sufficient. The one thing that could overturn this is the badge contradiction
on the Live Video API feature page, flagged above; you will see it in the dashboard immediately
if it bites.

**5. Persistent vs per-broadcast stream key.** **Both exist, but only the per-broadcast one is
reachable from the API, and that is what `connect.py` uses.** The persistent key is a **Live
Producer UI** toggle — *"turn on the Persistent stream key option if you want to reuse this
stream key in the future"* at facebook.com/live/create, with the constraint *"You may only have
one live video at a time with a persistent stream key"* (facebook.com/help/587160588142067). I
found **no** Graph API parameter or `LiveVideo` field to request one, and the Live Video API docs
never mention persistent keys. `connect.py` is built entirely around the per-broadcast model: it
mints a new `LiveVideo` and rewrites `FACEBOOK_KEY` on each run (lines 323–331, 452–458). **Your
approach depends on the per-broadcast key and that is the correct choice** — it is the only one
the API offers. The operational cost is that `golive` must run before each stream. Do not try to
mix in a UI-generated persistent key: it would conflict with the `LiveVideo` objects
`connect.py` creates, and the one-live-video-at-a-time rule would bite.

**6. Encoder spec.** See the full section above. **Your 1080p / 6000 / 160 / 48 kHz is inside
spec on every axis** — 6000 is exactly the documented 1080p30 ceiling (zero headroom), 160 kbps
sits between the 128 "preferred" and the 256 "do not exceed", and 48 kHz is explicitly permitted.
Set keyframe interval to 2 s, and make sure the H.264 level matches your frame rate (4.1 for 30
fps, 4.2 for 60). Facebook degrades rather than refuses. Unlike LinkedIn, this does not
disqualify the rig.

**7. Page vs profile.** **A Page is effectively required, and `connect.py` assumes one
absolutely.** It calls `GET /me/accounts` and exits with *"facebook: no Pages found for this
login"* if there are none (line 311); it stores `page_id`/`page_token`; it posts to
`/{page_id}/live_videos`. There is no profile code path. This is also the better branch on the
merits: Meta's own reference confirms the 60-day-account and 100-follower eligibility gates
(error subcodes 1363120 and 1363144) apply **only to user profiles, not Pages** — so going via a
Page sidesteps the follower threshold entirely.

**8. What `META_PAGE_NAME` should contain.** **The Page's display name — or any unique
case-insensitive substring of it.** From line 312–314:
```python
want = cfg.get("META_PAGE_NAME", "").lower()
page = next((p for p in pages if want and want in p["name"].lower()), None)
```
`p["name"]` is the `name` field returned by `/me/accounts?fields=id,name,access_token`, which is
the Page's **display name**. Not the username/slug (never fetched), not the numeric ID (that is
`p["id"]`, which is never compared). A numeric ID in this variable will fail to match and drop
you into the interactive picker. Because the test is a substring, a short distinctive fragment of
the name is fine and is more robust than the full name (no punctuation or emoji to get wrong) —
but make sure it is unique across your Pages, since `next()` takes the first match. **Leaving it
blank is entirely safe**: `connect.py` then prints a numbered list of your Pages and asks
(lines 315–318).

**9. Reading live chat.** **Your belief is confirmed.**
`GET https://streaming-graph.facebook.com/{live-video-id}/live_comments?access_token=...`, Server-
Sent Events, documented at /docs/live-video-api/interact-with-viewers. Auth is the Page token
already in `tokens.json`; the live video ID is already in `tokens.json` too. No extra permissions,
no extra review, no change to `connect.py`. Polling `GET /{live-video-id}/comments` is the
documented fallback. See §Chat for the one caveat about commenter identity.

---

## Only you can answer these, from inside your Meta account

1. **Is the existing `META_APP_ID` app of a type that exposes Page permissions?** Open it and see
   whether `publish_video` and the three `pages_*` permissions appear under Use cases. If not,
   create a fresh Business app — app type cannot be changed after creation, and a new app means a
   new `META_APP_ID` in `oauth.env`.
2. **What mode is that app in right now**, and has it ever been submitted for review? A previously
   reviewed app may already hold Advanced Access, in which case you are finished sooner.
3. **Does Meta's dashboard accept `http://localhost:8771/callback`?** The unresolved question.
   Test it at step 5.
4. **Do you hold Full control (or a Content-including role) on the Page you want to stream to?**
   If `connect.py` reports "no Pages found", this is why.
5. **Does the Live Video API feature show as Standard Access, or does it demand Business
   Verification?** This is the one thing that would flip the verdict from ten minutes to weeks.
6. **Are you running 1080p30 or 1080p60 in OBS**, and what H.264 level is the encoder announcing?
   Decides whether Level 4.1 or 4.2 is correct and whether 6000 kbps has headroom or none.
7. **What ingest host does `secure_stream_url` actually return** on your first run, and does your
   relay's hardcoded RTMPS base match it?
