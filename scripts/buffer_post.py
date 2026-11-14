#!/usr/bin/env python3
"""Post ads to Buffer from GitHub Actions.

Reads the API key from the BUFFER_ACCESS_TOKEN environment variable and never
prints it. Works with Buffer's current GraphQL API (https://api.buffer.com,
key from https://publish.buffer.com/settings/api) and falls back to the legacy
v1 REST API automatically if the key is an old v1 access token.

Commands (set with --action):
  list-channels   Show the connected Buffer channels (no posting).
  post            Post one ad from --text / --image-url.
  post-folder     Post every ad folder under ads/queue/, then move each
                  posted folder to ads/posted/.

Only the Python standard library is used, so no install step is needed.
"""

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GRAPHQL_URL = "https://api.buffer.com"
LEGACY_URL = "https://api.bufferapp.com/1"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MODES = {"queue": "addToQueue", "now": "shareNow", "schedule": "customScheduled"}


class AuthError(Exception):
    pass


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    req.add_header("User-Agent", "github-actions-buffer-post")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- GraphQL API
class GraphQLBuffer:
    name = "Buffer GraphQL API"

    def __init__(self, token):
        self.token = token
        self._org = None

    def q(self, query, variables=None):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        status, text = http(
            GRAPHQL_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        if status in (401, 403):
            raise AuthError(f"HTTP {status}")
        try:
            payload = json.loads(text)
        except ValueError:
            raise RuntimeError(f"Unexpected response (HTTP {status}): {text[:300]}")
        errors = payload.get("errors")
        if errors:
            msg = "; ".join(e.get("message", str(e)) for e in errors)
            codes = {str((e.get("extensions") or {}).get("code", "")).upper() for e in errors}
            if codes & {"UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN"} or "unauthor" in msg.lower():
                raise AuthError(msg)
            raise RuntimeError(msg)
        return payload["data"]

    def org_ids(self):
        if self._org is None:
            data = self.q("query { account { organizations { id name } } }")
            self._org = data["account"]["organizations"]
        return self._org

    def channels(self):
        out = []
        query = """
        query GetChannels($input: ChannelsInput!) {
          channels(input: $input) { id name displayName service isQueuePaused }
        }"""
        for org in self.org_ids():
            data = self.q(query, {"input": {"organizationId": org["id"]}})
            for c in data["channels"]:
                out.append(
                    {
                        "id": c["id"],
                        "service": c.get("service") or "",
                        "name": c.get("displayName") or c.get("name") or "",
                        "handle": c.get("name") or "",
                        "paused": bool(c.get("isQueuePaused")),
                    }
                )
        return out

    def create(self, channel, text, image_urls, mode, due_at):
        post_input = {
            "channelId": channel["id"],
            "text": text,
            "schedulingType": "automatic",
            "mode": MODES[mode],
            "assets": [{"image": {"url": u}} for u in image_urls],
        }
        if mode == "schedule":
            post_input["dueAt"] = due_at
        mutation = """
        mutation CreatePost($input: CreatePostInput!) {
          createPost(input: $input) {
            ... on PostActionSuccess { post { id dueAt } }
            ... on MutationError { message }
          }
        }"""
        result = self.q(mutation, {"input": post_input})["createPost"]
        if result.get("message"):
            raise RuntimeError(result["message"])
        post = result.get("post") or {}
        return f"post {post.get('id')}" + (f" due {post['dueAt']}" if post.get("dueAt") else "")


# ------------------------------------------------------------- legacy v1 API
class LegacyBuffer:
    name = "Buffer legacy v1 API"

    def __init__(self, token):
        self.token = token

    def channels(self):
        url = f"{LEGACY_URL}/profiles.json?" + urllib.parse.urlencode({"access_token": self.token})
        status, text = http(url)
        if status in (401, 403):
            raise AuthError(f"HTTP {status}")
        if status != 200:
            raise RuntimeError(f"HTTP {status}: {text[:300]}")
        return [
            {
                "id": p["id"],
                "service": p.get("service", ""),
                "name": p.get("formatted_username") or p.get("service_username") or "",
                "handle": p.get("service_username") or "",
                "paused": bool(p.get("paused")),
            }
            for p in json.loads(text)
        ]

    def create(self, channel, text, image_urls, mode, due_at):
        fields = [("access_token", self.token), ("profile_ids[]", channel["id"]), ("text", text)]
        if image_urls:
            fields += [("media[photo]", image_urls[0]), ("media[thumbnail]", image_urls[0])]
            for extra in image_urls[1:]:
                fields.append(("extra_media[][photo]", extra))
        if mode == "now":
            fields.append(("now", "true"))
        elif mode == "schedule":
            fields.append(("scheduled_at", due_at))
        status, body = http(
            f"{LEGACY_URL}/updates/create.json",
            data=urllib.parse.urlencode(fields).encode(),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status in (401, 403):
            raise AuthError(f"HTTP {status}")
        data = json.loads(body) if body.strip().startswith("{") else {}
        if status != 200 or not data.get("success", False):
            raise RuntimeError(data.get("message") or f"HTTP {status}: {body[:300]}")
        ups = data.get("updates") or [{}]
        return f"update {ups[0].get('id')}"


def connect(token):
    """Try the current GraphQL API first, then the legacy v1 API."""
    errors = []
    for cls in (GraphQLBuffer, LegacyBuffer):
        client = cls(token)
        try:
            chans = client.channels()
            print(f"Connected with the {client.name}.")
            return client, chans
        except AuthError as e:
            errors.append(f"{client.name}: {e}")
    fail(
        "BUFFER_ACCESS_TOKEN was rejected by both Buffer APIs ("
        + " | ".join(errors)
        + "). Create a new API key at https://publish.buffer.com/settings/api "
        "and save it as the BUFFER_ACCESS_TOKEN repository secret."
    )


# ------------------------------------------------------------------- helpers
def pick_channels(all_channels, selector):
    selector = (selector or "all").strip()
    if selector.lower() in ("", "all"):
        chosen = list(all_channels)
    else:
        wanted = [s.strip().lower() for s in selector.split(",") if s.strip()]
        chosen = []
        for c in all_channels:
            keys = {c["id"].lower(), c["service"].lower(), c["name"].lower(), c["handle"].lower()}
            if keys & set(wanted) and c not in chosen:
                chosen.append(c)
        missing = [w for w in wanted if not any(
            w in {c["id"].lower(), c["service"].lower(), c["name"].lower(), c["handle"].lower()}
            for c in all_channels)]
        if missing:
            print(f"::warning::No Buffer channel matched: {', '.join(missing)}")
    if not chosen:
        fail(f"No Buffer channels selected by '{selector}'. Run the list-channels action to see names.")
    return chosen


def print_channels(chans):
    print(f"{'SERVICE':<12} {'NAME':<32} {'ID':<26} PAUSED")
    for c in chans:
        print(f"{c['service']:<12} {c['name'][:32]:<32} {c['id']:<26} {'yes' if c['paused'] else ''}")
    summary(
        "### Buffer channels\n\n| Service | Name | ID | Queue paused |\n|---|---|---|---|\n"
        + "".join(f"| {c['service']} | {c['name']} | `{c['id']}` | {'yes' if c['paused'] else ''} |\n" for c in chans)
    )


def summary(md):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def raw_url(path):
    repo = os.environ.get("GITHUB_REPOSITORY")
    sha = os.environ.get("GITHUB_SHA")
    if not repo or not sha:
        fail("GITHUB_REPOSITORY / GITHUB_SHA not set; folder images need to run in GitHub Actions.")
    rel = Path(path).as_posix()
    return f"https://raw.githubusercontent.com/{repo}/{sha}/" + urllib.parse.quote(rel)


def post_one(client, channels, text, image_urls, mode, due_at, dry_run, label):
    ok = True
    rows = []
    for c in channels:
        target = f"{c['service']} / {c['name']}"
        if dry_run:
            print(f"[dry run] {label} -> {target}")
            rows.append(f"| {label} | {target} | dry run |")
            continue
        try:
            ref = client.create(c, text, image_urls, mode, due_at)
            print(f"OK   {label} -> {target}: {ref}")
            rows.append(f"| {label} | {target} | ✅ {ref} |")
        except Exception as e:  # keep going for other channels
            ok = False
            print(f"::error::{label} -> {target}: {e}")
            rows.append(f"| {label} | {target} | ❌ {e} |")
    return ok, rows


def load_folder(folder):
    caption_file = next((folder / n for n in ("caption.txt", "post.txt", "text.txt") if (folder / n).is_file()), None)
    if not caption_file:
        return None, None, None, "no caption.txt"
    text = caption_file.read_text(encoding="utf-8").strip()
    images = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    urls = [raw_url(p) for p in images]
    url_file = folder / "image_url.txt"
    if url_file.is_file():
        urls += [u.strip() for u in url_file.read_text().splitlines() if u.strip()]
    chan_file = folder / "channels.txt"
    selector = chan_file.read_text().strip().replace("\n", ",") if chan_file.is_file() else None
    return text, urls, selector, None


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--action", required=True, choices=["list-channels", "post", "post-folder"])
    ap.add_argument("--text", default="")
    ap.add_argument("--image-url", default="", help="Comma-separated public image URLs")
    ap.add_argument("--channels", default="all")
    ap.add_argument("--mode", default="queue", choices=list(MODES))
    ap.add_argument("--due-at", default="")
    ap.add_argument("--folder", default="ads/queue")
    ap.add_argument("--dry-run", default="false")
    args = ap.parse_args()
    dry_run = str(args.dry_run).lower() == "true"

    token = os.environ.get("BUFFER_ACCESS_TOKEN", "").strip()
    if not token:
        fail("The BUFFER_ACCESS_TOKEN repository secret is missing or empty. "
             "Add it under Settings > Secrets and variables > Actions.")
    print(f"::add-mask::{token}")

    due_at = args.due_at.strip()
    if args.mode == "schedule":
        if not due_at:
            fail("mode=schedule needs due_at, e.g. 2026-11-20T15:00:00Z (UTC).")
        try:
            due_at = datetime.fromisoformat(due_at.replace("Z", "+00:00")).astimezone(timezone.utc) \
                .strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except ValueError:
            fail(f"due_at '{due_at}' is not an ISO date-time like 2026-11-20T15:00:00Z.")

    client, all_channels = connect(token)

    if args.action == "list-channels":
        print_channels(all_channels)
        return

    table = ["| Ad | Channel | Result |", "|---|---|---|"]

    if args.action == "post":
        text = args.text.strip()
        if not text:
            fail("The text input is required for action=post.")
        urls = [u.strip() for u in args.image_url.split(",") if u.strip()]
        chans = pick_channels(all_channels, args.channels)
        ok, rows = post_one(client, chans, text, urls, args.mode, due_at, dry_run, "manual post")
        summary("### Buffer posting\n\n" + "\n".join(table + rows))
        sys.exit(0 if ok else 1)

    # post-folder
    queue = Path(args.folder)
    if not queue.is_dir():
        fail(f"Folder {queue} does not exist.")
    folders = sorted(p for p in queue.iterdir() if p.is_dir())
    if not folders:
        print(f"No ad folders in {queue}; nothing to post.")
        summary(f"No ad folders in `{queue}`.")
        return
    posted_root = Path("ads/posted")
    all_ok = True
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for folder in folders:
        text, urls, selector, err = load_folder(folder)
        if err:
            print(f"::warning::Skipping {folder}: {err}")
            table.append(f"| {folder.name} | - | skipped: {err} |")
            continue
        chans = pick_channels(all_channels, selector or args.channels)
        ok, rows = post_one(client, chans, text, urls, args.mode, due_at, dry_run, folder.name)
        table += rows
        all_ok &= ok
        if ok and not dry_run:
            posted_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(posted_root / f"{stamp}-{folder.name}"))
    summary("### Buffer posting\n\n" + "\n".join(table))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
