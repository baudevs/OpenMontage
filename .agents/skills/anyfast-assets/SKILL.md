---
name: anyfast-assets
description: |
  The AnyFast asset library and real-human (LivenessFace) verification, end to end: verify a person on their phone, host media on R2, register it with CreateAsset, poll to Active, then reference it as asset://<ID> in a Seedance 2.5 generation with the right role. Use whenever a video must feature a real person's face, whenever a LOCAL video is needed as a reference, or whenever an AnyFast asset call returns a confusing 404. Records the API behaviours that contradict the published docs.
---

# AnyFast assets and real-human faces

Two tools: `anyfast_assets` (the `/volc/asset` library + verification) and
`anyfast_video` (generation). Both read `ANYFAST_API_KEY`.

## The one rule that explains most failures

**AnyFast ingests an asset from a URL it downloads itself.** Host the file
first (`r2-storage`), then pass the URL to `CreateAsset`.

Its multipart file upload is documented and does return an asset Id — but those
assets stayed permanently unresolvable in testing, while the *identical files*
ingested from a public R2 URL reached `Active` in seconds. `anyfast_assets`
therefore publishes local files to R2 automatically; multipart is behind
`allow_multipart: true` and should not be used.

## Real-human flow (an authorized person's face)

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

> **Model choice decides whether a verified face works. Verified 2026-08-26.**
>
> | Model | AIGC `asset://` | LivenessFace `asset://` |
> |-------|-----------------|-------------------------|
> | `seedance-2.5`, `seedance-2.5-nsfw` | works | **`InvalidParameter: ... is not found`** |
> | `seedance-2.0` and its variants | works | **works** |
>
> Seedance 2.5 cannot resolve a real-human asset — generation reports it missing
> even though `GetAsset` returns `Active`. The 2.0 family resolves the same asset
> ID. **So a real person's face means Seedance 2.0**, and a pipeline that needs
> both 2.5's 30-second takes and a verified face has to choose.
>
> There is no way around it with a plain URL either: a real face sent as a public
> URL is refused with `InputImageSensitiveContentDetected.PrivacyInformation`
> ("may contain real person"). The verified asset is the only route, and 2.0 is
> the only model that reads it.
>
> `anyfast_video` warns at `dry_run` when an `asset://` reference meets a 2.5
> model, and `get_info()["model_catalog"][model]["resolves_real_human_assets"]`
> exposes the flag for routing.

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
