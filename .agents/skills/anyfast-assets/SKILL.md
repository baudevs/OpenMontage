---
name: anyfast-assets
description: |
  How to put a person's face (or any reusable reference) into a Seedance video through AnyFast: register the person, upload their face to an AIGC asset group, reference it as asset://<ID>, and fall back to real-person verification only when AnyFast refuses the image. Also covers when a reference does NOT need an asset at all, the people registry that remembers who is already uploaded, the R2 hosting requirement, and the API behaviours that contradict the published docs. Use for any video featuring a real person, any local reference video, or any confusing AnyFast asset 404.
---

# AnyFast assets and face references

Tools: `anyfast_assets` (the `/volc/asset` library + verification),
`people_registry` (who is already uploaded), `r2_storage` (hosting),
`anyfast_video` (generation).

## Rule 0 — Seedance runs on AnyFast

**When `ANYFAST_API_KEY` is set, Seedance 2.0 and 2.5 go through `anyfast_video`.**
Not fal.ai, not Replicate. They serve the same models, but AnyFast is cheaper and
is the only route that can reference a registered face — `asset://` does not exist
on the others, so a fal.ai Seedance call simply cannot put a specific person in the
shot. `video_selector` pins this automatically; overriding it means telling the
user which provider you switched to and why.

## Decide what kind of reference you have

| The reference is… | Do this |
|---|---|
| **A person's face** | Upload to that person's **AIGC asset group**, reference `asset://<ID>`. Reusable, and works on **every** Seedance model. |
| **A face AnyFast refuses** (`PrivacyInformation` / sensitive content) | **Stop and ask the user.** Real-person verification is the only route, and it needs them present with a phone. The resulting group restricts generation to **Seedance 2.0**. |
| **Not a person** — product, location, style, prop | Just pass the local path or URL. It is inlined as Base64; no asset, no group, no bookkeeping. Register it as an asset only if you will reuse it across many runs. |
| **A local video** | Must become an asset (`asset://`) — AnyFast cannot ingest video any other way. |

The face path is **not** the verification path by default. An ordinary AIGC group
accepts face images and its assets work on Seedance 2.5; verification is the
exception, for images AnyFast refuses.

## Face reference flow

```
1. people_registry list_people        -> offer the user someone already registered
2. new person?  ask for consent, then anyfast_assets upload with person="<slug>"
                (creates <slug>-assets as an AIGC group, records everything)
3. people_registry references person=<slug> model=<model>  -> asset:// refs
4. anyfast_video with those refs and role reference_image
```

`anyfast_assets upload` with a `person` does all the bookkeeping: resolves or
creates the group, hosts the file on R2, registers the asset, and records it in
the per-project people database. **Never re-upload a face that is already
registered** — ask the user first, they usually mean the person you already have.

### Consent

Record it once. Pass `consent_confirmed: true` on the first upload for a person,
after the user has confirmed that person authorized use of their likeness. It is
timestamped and never cleared, and later runs reuse it silently. Do not generate
with a face that has no consent record without asking.

### The people registry

Local SQLite, per project, outside the repository
(`~/.openmontage/projects/<project>/people.db`). It stores names and provider
identifiers — never images. It exists because a `LivenessFace` GroupId cannot be
listed back from the API and its `BytedToken` expires: lose it and the person has
to verify again.

Before generating anything with a person in it, `list_people` and present the
options, showing which models each person supports:

- someone with an **AIGC** group → any Seedance model
- someone with only a **LivenessFace** group → Seedance 2.0 only

## The one rule that explains most failures

**AnyFast ingests an asset from a URL it downloads itself.** Host the file
first (`r2-storage`), then pass the URL to `CreateAsset`.

Its multipart file upload is documented and does return an asset Id — but those
assets stayed permanently unresolvable in testing, while the *identical files*
ingested from a public R2 URL reached `Active` in seconds. `anyfast_assets`
therefore publishes local files to R2 automatically; multipart is behind
`allow_multipart: true` and should not be used.

## Verification flow — only when AnyFast refuses the face

Required: an API token created with the **Byteplus-Direct** group. An AIGC-only
token returns `GroupType must be one of [AIGC]`.

```
1. create_liveness_session(callback_url=...)   -> H5Link + BytedToken
2. the person opens H5Link ON A PHONE, completes the scan, and taps Complete
3. get_liveness_result(byted_token)            -> GroupId
4. upload(source, group_id, group_type="LivenessFace")  -> asset://<ID>, Active
5. anyfast_video with that asset:// reference
```

Non-obvious, all verified against the live API:

- **`callback_url` is required and is a redirect target.** The published schema
  marks the body optional; an empty body is rejected. More important: the H5
  page sends the person's browser to that URL when they tap **Complete**, and
  the group is created only at that moment. A session where they scanned but
  never landed on the callback stays at `GroupId: ""` forever. "Did you end up
  on the callback page?" is the completion check.
- **Save the `GroupId` immediately.** A LivenessFace group never appears in
  `ListAssetGroups` — not even filtered by its exact `Id` — and the `BytedToken`
  expires, after which `get_liveness_result` can no longer recover it. Put it in
  the project's notes or `ANYFAST_ASSET_GROUP_ID`.
- **One token owns everything.** The token that created the session must also
  create and read the assets.
- **Names must be unique inside a group.** A duplicate `Name` makes the API
  answer `404` as if the *group* did not exist — a genuinely misleading error.
  `anyfast_assets` appends a random suffix by default; leave it on.

## Reading a 404

`CreateAsset` returns an Id and then `GetAsset` answers `[NotFound.asset_id]`
forever. That is not a read-back quirk — the asset did not survive
preprocessing. In order of likelihood:

1. **Face consistency verification failed.** The account log shows
   `502 upstream asset error: Face consistency verification failed` immediately
   before the 404s, and AnyFast emails the failure. Upload a clear, front-facing
   shot of the *verified* person; a different person in a verified group always
   fails.
2. **Duplicate `Name`** in the group (see above).
3. **Wrong token** for that group.

Do not poll a 404 for minutes — it only fills the account log. `anyfast_assets`
stops after a short grace window and reports these three causes.

`ListAssets` does not list real-human assets either; `GetAsset` with the asset
Id is the read that works, and it returns a fresh 12-hour signed `URL` each
call. Use `asset://<ID>` in generations, never that signed URL.

## Upload envelopes

Checked locally before the paid create, because an out-of-envelope file is
accepted and only fails later.

| Asset | Real-human (LivenessFace) | Generic (AIGC) |
|-------|---------------------------|----------------|
| Image | JPEG/PNG/WebP/GIF/HEIC, < 30 MB, 300–6000 px/side, ratio 0.4–2.5 | + BMP/TIFF |
| Video | MP4/MOV, ≤ 50 MB, 2–15 s, 24–60 FPS | ≤ 200 MB, 2–30 s |
| Audio | WAV/MP3, ≤ 15 MB, 2–15 s | ≤ 15 MB, 2–30 s |

Video is bounded by **pixels per frame: 407,696–8,295,044**, not by a
resolution name. A 406×720 portrait crop is nominally "720p" but only 292k
pixels and is rejected as `PixelCountTooSmall`. Rescale to e.g. 720×1280
(921,600). A 4K 60fps phone clip needs transcoding first:

```bash
ffmpeg -i in.mp4 -t 12 -vf "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280" \
  -r 30 -c:v libx264 -crf 23 -pix_fmt yuv420p -c:a aac out.mp4
```

## Generating with the asset

> **The GROUP decides which models can read the asset. Verified 2026-08-26.**
>
> | Group type | `seedance-2.5` | `seedance-2.0` family |
> |------------|----------------|------------------------|
> | **AIGC** (ordinary) | works | works |
> | **LivenessFace** (verified) | `InvalidParameter: ... is not found` | works |
>
> A face in an **AIGC** group generates on 2.5 — tested with a real portrait,
> uploaded with no verification, rendering correctly at 480p/4s. That is why the
> AIGC path is the default for faces.
>
> A **LivenessFace** asset is invisible to 2.5 (it reports the asset missing even
> though `GetAsset` says `Active`) and readable by the 2.0 family. So a person who
> had to go through verification costs you 2.5: 4–15s shots instead of 4–30s, and
> 9/3/3 reference items instead of 30/10/10.
>
> Passing a face as a **plain URL or Base64** is refused outright with
> `InputImageSensitiveContentDetected.PrivacyInformation`. Faces must be assets.
>
> `anyfast_video` warns at `dry_run` when an `asset://` meets a 2.5 model (it
> cannot tell which group the asset came from — `people_registry references
> model=seedance-2.5` can, and only returns what will work).

Pass the reference as a content item with a `role`. Seedance 2.5, one endpoint,
`POST /v1/video/generations`:

```json
{
  "type": "image_url",
  "image_url": { "url": "asset://asset-20260702223855-bdv2r" },
  "role": "reference_image"
}
```

Through `anyfast_video`, hand the `asset://` reference in the normal reference
inputs — they accept `asset://` anywhere a URL is accepted:

```python
registry.get("anyfast_video").execute({
    "operation": "reference_to_video",
    "model": "seedance-2.5",
    "prompt": "@image1 walks into the kitchen and smiles at the camera",
    "reference_image_urls": ["asset://asset-20260826182019-7xcvh"],
    "resolution": "1080p",
    "aspect_ratio": "16:9",
    "duration": 8,
    "output_path": "projects/<slug>/assets/video/shot-01.mp4",
})
```

Roles by operation:

| Operation | Role(s) sent | Notes |
|-----------|--------------|-------|
| `image_to_video` | `first_frame`, optional `last_frame` | ratio forced to `adaptive` |
| `reference_to_video` | `reference_image` / `reference_video` / `reference_audio` | up to 30 / 10 / 10; refer to them as `@image1`, `@video1`, `@audio1` in the prompt |
| `video_edit` | `reference_video` | `ratio: adaptive`, `duration: -1` only |
| `video_extend` | `reference_video` | `ratio: adaptive` |

Prompt the reference explicitly — `@image1 <does something>` — and state what
each item should contribute. A verified portrait plus a motion clip is the
combination that keeps identity stable across a shot.

## Ethics and consent

The docs are explicit: do not send reference images or videos containing
real-person faces unless the workflow and account are authorized. The
verification flow *is* that authorization — it requires the person to be
present, on their phone, in the moment. Never route around it by passing a
portrait as a plain URL, never put two different people in one verified group,
and delete the R2 copies when the work is done.

## Related

- `r2-storage` — hosting the file so AnyFast can fetch it
- `seedance-2-5` — prompt contract, multi-shot structure, reference limits
