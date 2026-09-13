# repo-dump

Dumps a GitHub repository's discussions and media **back into a repository**:
every issue, pull request and release - full text, complete comment threads,
reviews, inline review comments, release notes, tags and release artifacts -
plus every attachment (images, audio, video, files) uploaded inside them.

Pure Python 3 standard library. No dependencies, nothing to install.

## Run it from a phone (no laptop, no terminal)

1. Open this repo in a mobile browser.
2. **Actions** tab -> **repo-dump** workflow -> **Run workflow**.
3. Optionally set `source_repository` (any `owner/name`, defaults to this
   repo) and `output_branch` (default `repo-dump`).
4. Tap **Run workflow**. A few minutes later the dump is committed to the
   `repo-dump` branch.

That is the whole operation - one green button, so any team member can do it.

## Run it from a shell

```sh
GITHUB_TOKEN=ghp_xxx python3 tools/repo_dump.py --repo owner/name --out repo-dump
```

## What lands in the dump

```
index.md                 summary + counts
manifest.json            counts, sha256 + byte size per asset, failed downloads
issues/<n>.md|.json      one file per issue: metadata, body, comment thread
pulls/<n>.md|.json       one file per PR: body, comments, reviews, inline comments
releases/<tag>.md|.json  notes, tag, artifacts (binaries downloaded to assets/)
assets/                  every attachment, hashed, named in encounter order
```

Attachment links inside the dumped Markdown are rewritten to the local
`assets/` path, so the dump is self-contained. A download that fails is
recorded in `manifest.json` under `failures` and the original URL is kept -
never a silent gap.

## Notes

- Open **and** closed issues/PRs are exported (`state=all`).
- The Actions run uses the automatic `GITHUB_TOKEN`; dumping a *different*
  private repo needs a PAT with read access stored as a secret.
- Tests: `python3 -m unittest discover -s tests -v`
