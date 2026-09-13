#!/usr/bin/env python3
"""repo_dump: dump a GitHub repository's issues, PRs, releases and their
attached media back into a repository directory.

Python 3.9+ standard library only. Designed to run inside GitHub Actions
(triggerable from a phone) or from any shell.

Usage:
    GITHUB_TOKEN=... python3 tools/repo_dump.py --repo owner/name --out repo-dump
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
ATTACH_RE = re.compile(
    r"https://(?:"
    r"github\.com/user-attachments/assets/[0-9a-fA-F-]+|"
    r"github\.com/user-attachments/files/[0-9]+/[^\s)\"'>]+|"
    r"user-images\.githubusercontent\.com/[^\s)\"'>]+)")


class DumpError(Exception):
    pass


class GitHubClient:
    def __init__(self, token, max_retries=3):
        self.token = token
        self.max_retries = max_retries
        self.requests = 0

    def _headers(self, url):
        h = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "repo-dump-tool"}
        # API tokens are rejected by github.com's web/CDN hosts (403), so
        # only send Authorization to the API host. Public assets download
        # fine anonymously.
        if self.token and urllib.parse.urlparse(url).netloc == "api.github.com":
            h["Authorization"] = "Bearer " + self.token
        return h

    def get(self, path, params=None, raw=False):
        url = path if path.startswith("http") else API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=self._headers(url))
        for attempt in range(self.max_retries):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self.requests += 1
                    if raw:
                        return resp.read(), resp.headers
                    return json.loads(resp.read().decode("utf-8")), resp.headers
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore").lower()
                if e.code in (403, 429) and attempt < self.max_retries - 1:
                    if "rate limit" in body:
                        reset = int(e.headers.get("X-RateLimit-Reset",
                                                  time.time() + 60))
                        wait = max(1, min(reset - int(time.time()), 900))
                    else:
                        wait = 5 * (attempt + 1)  # throttled web/CDN host
                    print("HTTP %s; retrying in %ds" % (e.code, wait),
                          file=sys.stderr)
                    time.sleep(wait)
                    continue
                if e.code >= 500 and attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise DumpError("GET %s failed: HTTP %s" % (url, e.code))
            except urllib.error.URLError as e:
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise DumpError("GET %s failed: %s" % (url, e.reason))
        raise DumpError("GET %s failed after retries" % url)

    def paged(self, path, params=None):
        """Yield every item from a paginated list endpoint."""
        params = dict(params or {})
        params["per_page"] = 100
        page = 1
        while True:
            params["page"] = page
            items, _ = self.get(path, params)
            if not isinstance(items, list):
                raise DumpError("expected list from %s" % path)
            for item in items:
                yield item
            if len(items) < 100:
                return
            page += 1


def find_attachments(*texts):
    """Return the de-duplicated GitHub attachment URLs in the given texts."""
    seen, out = set(), []
    for text in texts:
        for url in ATTACH_RE.findall(text or ""):
            url = url.rstrip(".,;")
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


def safe_name(url, index):
    base = url.rstrip("/").rsplit("/", 1)[-1] or "asset"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return "%03d-%s" % (index, base)


class AssetStore:
    """Downloads attachments into <out>/assets and rewrites URLs to local paths."""

    def __init__(self, client, out_dir, enabled=True):
        self.client = client
        self.dir = os.path.join(out_dir, "assets")
        self.enabled = enabled
        self.map = {}
        self.records = []
        self.failures = []
        self._n = 0

    def fetch(self, url):
        if url in self.map:
            return self.map[url]
        self._n += 1
        name = safe_name(url, self._n)
        if not self.enabled:
            self.map[url] = url
            return url
        os.makedirs(self.dir, exist_ok=True)
        dest = os.path.join(self.dir, name)
        try:
            data, _ = self.client.get(url, raw=True)
            with open(dest, "wb") as f:
                f.write(data)
            digest = hashlib.sha256(data).hexdigest()
            rel = "assets/" + name
            self.records.append({"url": url, "path": rel, "sha256": digest,
                                 "bytes": len(data)})
            self.map[url] = rel
            return rel
        except Exception as e:  # keep dumping; report the gap in the manifest
            self.failures.append({"url": url, "error": str(e)})
            self.map[url] = url
            return url

    def rewrite(self, text):
        for url in find_attachments(text):
            text = (text or "").replace(url, self.fetch(url))
        return text or ""


def comment_view(c, assets):
    return {"author": (c.get("user") or {}).get("login"),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
            "body": assets.rewrite(c.get("body")),
            "reactions": (c.get("reactions") or {}).get("total_count", 0)}


def item_view(i, assets):
    return {"number": i.get("number"), "state": i.get("state"),
            "title": i.get("title"),
            "author": (i.get("user") or {}).get("login"),
            "created_at": i.get("created_at"), "closed_at": i.get("closed_at"),
            "labels": [l.get("name") for l in i.get("labels", [])],
            "assignees": [a.get("login") for a in i.get("assignees", [])],
            "milestone": (i.get("milestone") or {}).get("title"),
            "body": assets.rewrite(i.get("body")),
            "reactions": (i.get("reactions") or {}).get("total_count", 0),
            "url": i.get("html_url")}


def render_markdown(kind, item, comments, extra_sections=None):
    lines = ["# %s #%s: %s" % (kind, item["number"], item["title"]), ""]
    lines.append("- state: %s" % item["state"])
    lines.append("- author: %s" % item["author"])
    lines.append("- created: %s" % item["created_at"])
    if item.get("closed_at"):
        lines.append("- closed: %s" % item["closed_at"])
    if item.get("labels"):
        lines.append("- labels: %s" % ", ".join(item["labels"]))
    lines.append("- url: %s" % item["url"])
    lines += ["", item["body"] or "*(no body)*", "", "---", "## Comments", ""]
    for c in comments:
        lines.append("### %s at %s" % (c["author"], c["created_at"]))
        lines += ["", c["body"] or "*(empty)*", ""]
    for title, body in (extra_sections or []):
        lines += ["---", "## " + title, "", body, ""]
    return "\n".join(lines)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def dump_issues(client, repo, out, assets):
    count = 0
    for issue in client.paged("/repos/%s/issues" % repo, {"state": "all"}):
        if "pull_request" in issue:  # PRs are handled by dump_prs
            continue
        n = issue["number"]
        comments = [comment_view(c, assets) for c in client.paged(
            "/repos/%s/issues/%d/comments" % (repo, n))]
        view = item_view(issue, assets)
        view["comments"] = comments
        write_json(os.path.join(out, "issues", "%d.json" % n), view)
        write_text(os.path.join(out, "issues", "%d.md" % n),
                   render_markdown("Issue", view, comments))
        count += 1
    return count


def dump_prs(client, repo, out, assets):
    count = 0
    for pr in client.paged("/repos/%s/pulls" % repo, {"state": "all"}):
        n = pr["number"]
        issue_comments = [comment_view(c, assets) for c in client.paged(
            "/repos/%s/issues/%d/comments" % (repo, n))]
        reviews, bodies = [], []
        for r in client.paged("/repos/%s/pulls/%d/reviews" % (repo, n)):
            reviews.append({"author": (r.get("user") or {}).get("login"),
                            "state": r.get("state"),
                            "submitted_at": r.get("submitted_at"),
                            "body": assets.rewrite(r.get("body"))})
            bodies.append("**%s** (%s at %s)\n\n%s" % (
                reviews[-1]["author"], r.get("state"),
                r.get("submitted_at"), reviews[-1]["body"] or "*(empty)*"))
        inline, inline_md = [], []
        for rc in client.paged("/repos/%s/pulls/%d/comments" % (repo, n)):
            inline.append({"author": (rc.get("user") or {}).get("login"),
                           "path": rc.get("path"), "line": rc.get("line"),
                           "created_at": rc.get("created_at"),
                           "body": assets.rewrite(rc.get("body"))})
            inline_md.append("**%s** on `%s`:%s\n\n%s" % (
                inline[-1]["author"], rc.get("path"), rc.get("line"),
                inline[-1]["body"] or "*(empty)*"))
        view = item_view(pr, assets)
        view.update({"merged_at": pr.get("merged_at"),
                     "head": (pr.get("head") or {}).get("ref"),
                     "base": (pr.get("base") or {}).get("ref"),
                     "comments": issue_comments, "reviews": reviews,
                     "review_comments": inline})
        write_json(os.path.join(out, "pulls", "%d.json" % n), view)
        write_text(os.path.join(out, "pulls", "%d.md" % n),
                   render_markdown("PR", view, issue_comments, [
                       ("Reviews", "\n\n".join(bodies) or "*(none)*"),
                       ("Inline review comments",
                        "\n\n".join(inline_md) or "*(none)*")]))
        count += 1
    return count


def dump_releases(client, repo, out, assets):
    count = 0
    for rel in client.paged("/repos/%s/releases" % repo):
        tag = rel.get("tag_name") or "untagged"
        files = []
        for a in rel.get("assets", []):
            url = a.get("browser_download_url")
            files.append({"name": a.get("name"), "url": url,
                          "size": a.get("size")})
            if url:
                assets.fetch(url)
        view = {"tag": tag, "name": rel.get("name"),
                "draft": rel.get("draft"), "prerelease": rel.get("prerelease"),
                "author": (rel.get("author") or {}).get("login"),
                "created_at": rel.get("created_at"),
                "published_at": rel.get("published_at"),
                "body": assets.rewrite(rel.get("body")),
                "assets": files, "url": rel.get("html_url")}
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", tag)
        write_json(os.path.join(out, "releases", safe + ".json"), view)
        lines = ["# Release %s" % (view["name"] or tag), "",
                 "- tag: %s" % tag, "- published: %s" % view["published_at"],
                 "- url: %s" % view["url"], "", view["body"] or "*(no notes)*",
                 "", "## Artifacts", ""]
        for f in files:
            lines.append("- %s (%s bytes)" % (f["name"], f["size"]))
        write_text(os.path.join(out, "releases", safe + ".md"),
                   "\n".join(lines))
        count += 1
    return count


def run(repo, out, token, include_attachments=True, client=None):
    if client is None:
        client = GitHubClient(token)
    assets = AssetStore(client, out, enabled=include_attachments)
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    counts = {"issues": dump_issues(client, repo, out, assets),
              "pull_requests": dump_prs(client, repo, out, assets),
              "releases": dump_releases(client, repo, out, assets)}
    manifest = {"repository": repo, "dumped_at": started,
                "counts": counts, "api_requests": client.requests,
                "assets": assets.records, "failures": assets.failures}
    write_json(os.path.join(out, "manifest.json"), manifest)
    lines = ["# Repository dump: %s" % repo, "",
             "Dumped at %s." % started, "",
             "- issues: %d" % counts["issues"],
             "- pull requests: %d" % counts["pull_requests"],
             "- releases: %d" % counts["releases"],
             "- assets downloaded: %d" % len(assets.records),
             "- failed downloads: %d" % len(assets.failures), "",
             "Layout: `issues/`, `pulls/`, `releases/` hold one Markdown and",
             "one JSON file per item; `assets/` holds downloaded attachments;",
             "`manifest.json` records counts, checksums and any failures."]
    write_text(os.path.join(out, "index.md"), "\n".join(lines))
    return manifest


def main(argv=None):
    p = argparse.ArgumentParser(description="Dump a GitHub repo into itself.")
    p.add_argument("--repo", required=True, help="owner/name to dump")
    p.add_argument("--out", default="repo-dump", help="output directory")
    p.add_argument("--skip-attachments", action="store_true")
    args = p.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    manifest = run(args.repo, args.out, token,
                   include_attachments=not args.skip_attachments)
    print(json.dumps(manifest["counts"]))
    if manifest["failures"]:
        print("WARNING: %d downloads failed, see manifest.json"
              % len(manifest["failures"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
