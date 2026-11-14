# Ads for Buffer

The **Post ad to Buffer** workflow (`.github/workflows/buffer-post.yml`) sends ads
to your Buffer channels using the `BUFFER_ACCESS_TOKEN` repository secret.

## Run it

GitHub → **Actions** → **Post ad to Buffer** → **Run workflow**, then pick an action:

| Action | What it does |
|---|---|
| `list-channels` | Lists your connected Buffer channels. Run this first to check the token works. |
| `post` | Posts the `text` (and optional `image_url`) you type in the form. |
| `post-folder` | Posts every ad folder in `ads/queue/`, then moves each one to `ads/posted/`. |

Other fields:

- **channels**: `all`, or a comma list of services, names or IDs, e.g. `instagram,facebook`.
- **mode**: `queue` (next free slot in your Buffer schedule), `now`, or `schedule` with **due_at** in UTC, e.g. `2026-11-20T15:00:00Z`.
- **dry_run**: tick it to see what would be posted without posting.

## Queue an ad as a folder

```
ads/queue/agency-growth-blueprint/
  caption.txt      ← the post text (required)
  image.png        ← one or more images (png, jpg, gif, webp), optional
  channels.txt     ← optional: e.g. "instagram,facebook" (one per line or comma list)
  image_url.txt    ← optional: public image URLs, one per line
```

Images in the folder are served to Buffer from `raw.githubusercontent.com`, which works
because this repository is public. Instagram needs an image on every post; X/Twitter
captions must fit in 280 characters.

If a post fails on any channel, that folder stays in `ads/queue/` so you can fix and
re-run it (channels that already succeeded will get it again, so narrow `channels.txt`
first).
