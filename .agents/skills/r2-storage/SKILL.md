---
name: r2-storage
description: |
  Publish local media to Cloudflare R2 and get a public https URL, for providers that will only ingest a URL they can download themselves (AnyFast's asset library, reference video, oversized reference images). Covers enabling R2, the five env vars, key naming and uniqueness, verifying the object is actually public, and cleanup. Use whenever a tool rejects a local path or Base64 and asks for a URL.
---

# R2 media hosting

Some providers cannot take a local file. AnyFast's asset library is the clearest
case: **video is only ingestible as a URL or `asset://` ID**, and its multipart
upload path produces assets that never become usable. R2 with the bucket's
public development URL gives us a URL any provider can fetch.

## Is it enabled?

```python
from tools.storage import r2_client
r2_client.is_configured()   # True when all five vars are set
r2_client.missing_env()     # names the ones that are missing
```

`r2_storage` reports UNAVAILABLE until all five are present. Never assume it is
configured — check, and if it is not, say which variables are missing rather
than falling back to a local path the provider will reject.

## Setup (one time)

1. Cloudflare dashboard → R2 → create a bucket.
2. Bucket → Settings → **Public Development URL** → enable. This yields
   `https://pub-<hash>.r2.dev`. Without it every object is private and the
   provider's download fails with an opaque error.
3. R2 → Manage API tokens → create a token with **Object Read & Write**.
4. Put in `.env`:

```bash
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=
R2_PUBLIC_BASE_URL=https://pub-<hash>.r2.dev
# optional
R2_KEY_PREFIX=openmontage
```

## Use

```python
registry.get("r2_storage").execute({
    "operation": "upload",
    "path": "projects/<slug>/references/portrait.jpg",
    "project": "<slug>",           # <prefix>/<project>/<kind>/<file>
    "kind": "faces",               # faces | refs | video | audio
})
# -> {"key": "openmontage/<slug>/faces/portrait-8f21a0c3.jpg",
#     "url": "https://pub-<hash>.r2.dev/openmontage/<slug>/faces/portrait-8f21a0c3.jpg",
#     "public_verified": true, "retained": true}
```

Operations: `upload`, `upload_many`, `delete`, `exists`, `url`, `list`, `sweep`.

## Key layout

    <prefix>/<project>/<kind>/<name>-<random>.<ext>

Project first, so everything for one client is a single prefix and finishing a
project is one sweep. `kind` under it, so retention can treat a face differently
from a disposable prop:

| kind | for | retention |
|------|-----|-----------|
| `faces` | a person's reference images | **kept** |
| `refs` | products, locations, props, style boards | swept |
| `video` / `audio` | reference clips | swept |

## Retention

The provider copies the file into its own storage once its asset is `Active`, so
the staging object has no further purpose — except for faces, where losing the
original means re-uploading to re-register the person.

- `anyfast_assets` deletes a transient object automatically after ingestion and
  keeps `faces`. Override per call with `keep_hosted`.
- `sweep` cleans up what is left: `{"operation": "sweep", "project": "<slug>",
  "older_than_days": 7}`. It **defaults to a dry run** — read `candidates`, then
  repeat with `sweep_dry_run: false`. It skips `faces` unless you pass
  `include_retained: true`.
- Deleting an object a provider has not ingested yet breaks that generation.
  When in doubt, sweep by age rather than immediately.

## Rules that matter

- **Keys are unique by default.** A random suffix is appended. Do not turn this
  off to "keep tidy names": overwriting a key a provider already fetched
  silently changes what it ingested, and AnyFast rejects a second asset that
  reuses a name.
- **Everything uploaded is world-readable by URL.** That is the point — the
  provider downloads anonymously — but it means R2 is not the place for
  anything private. Say so before publishing someone's face, and get consent.
- **`public_verified`** comes from a real HEAD against the public URL. If it is
  false the upload raises: the bucket's public URL is off, and the provider
  would have failed later with a vague message.
- **Clean up.** Treat the bucket as staging. `delete` with the returned `key`
  once the provider has ingested the asset and the generation is done.

## Every provider uses this

`video_selector` and the fal.ai-backed tools call `upload_reference_media()`,
which prefers R2 whenever it is configured and falls back to fal.ai storage
otherwise. One hosting story, one bucket to sweep, and no dependency on a fal.ai
key just to hand a provider a reference image.

## Related

- `anyfast-assets` — the consumer of these URLs (asset library, faces)
