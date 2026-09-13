import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import repo_dump  # noqa: E402

ATT = "https://github.com/user-attachments/assets/8210eda6-f708-448a-8ca3-515bc00d1edc"
IMG = "https://user-images.githubusercontent.com/1/abc.png"


class FakeClient:
    """Serves canned API pages; records calls. No network."""
    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.requests = 0

    def get(self, path, params=None, raw=False):
        self.calls.append(path)
        self.requests += 1
        if raw:
            return b"\x89PNG fake-bytes", {}
        return self.pages[path], {}

    def paged(self, path, params=None):
        self.calls.append(path)
        self.requests += 1
        return iter(self.pages.get(path, []))


def issue(n, with_pr=False, body="hello"):
    i = {"number": n, "state": "open", "title": "t%d" % n,
         "user": {"login": "u"}, "created_at": "2026-01-01",
         "closed_at": None, "labels": [], "assignees": [], "milestone": None,
         "body": body, "reactions": {"total_count": 0},
         "html_url": "https://x/%d" % n}
    if with_pr:
        i["pull_request"] = {"url": "https://api/pulls/%d" % n}
    return i


class AttachmentTests(unittest.TestCase):
    def test_finds_both_attachment_hosts(self):
        urls = repo_dump.find_attachments("see %s and %s)" % (ATT, IMG))
        self.assertEqual(urls, [ATT, IMG])

    def test_dedupes_and_strips_trailing_punct(self):
        urls = repo_dump.find_attachments("%s. %s," % (ATT, ATT))
        self.assertEqual(urls, [ATT])

    def test_empty_and_none(self):
        self.assertEqual(repo_dump.find_attachments(None, ""), [])


class DumpTests(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.out)

    def make_client(self):
        return FakeClient({
            "/repos/o/r/issues": [issue(1), issue(2, with_pr=True)],
            "/repos/o/r/issues/1/comments": [
                {"user": {"login": "c"}, "created_at": "2026-01-02",
                 "updated_at": "2026-01-02", "body": "pic %s" % ATT,
                 "reactions": {}}],
            "/repos/o/r/pulls": [dict(issue(2), merged_at="2026-01-03",
                                      head={"ref": "h"}, base={"ref": "main"})],
            "/repos/o/r/issues/2/comments": [],
            "/repos/o/r/pulls/2/reviews": [
                {"user": {"login": "r"}, "state": "APPROVED",
                 "submitted_at": "2026-01-04", "body": "lgmt"}],
            "/repos/o/r/pulls/2/comments": [
                {"user": {"login": "r"}, "path": "a.py", "line": 3,
                 "created_at": "2026-01-04", "body": "nit"}],
            "/repos/o/r/releases": [
                {"tag_name": "v1.0", "name": "one", "draft": False,
                 "prerelease": False, "author": {"login": "u"},
                 "created_at": "2026-01-05", "published_at": "2026-01-05",
                 "body": "notes", "html_url": "https://x/rel",
                 "assets": [{"name": "bin.zip", "size": 10,
                             "browser_download_url": "https://dl/bin.zip"}]}],
        })

    def test_full_dump_layout_and_manifest(self):
        manifest = repo_dump.run("o/r", self.out, token=None, client=self.make_client())
        self.assertEqual(manifest["counts"],
                         {"issues": 1, "pull_requests": 1, "releases": 1})
        for path in ["issues/1.md", "issues/1.json", "pulls/2.md",
                     "pulls/2.json", "releases/v1.0.md", "releases/v1.0.json",
                     "index.md", "manifest.json"]:
            self.assertTrue(os.path.exists(os.path.join(self.out, path)), path)
        # attachment in the issue comment was downloaded and rewritten
        md = open(os.path.join(self.out, "issues/1.md")).read()
        self.assertIn("assets/001-", md)
        self.assertNotIn(ATT, md)
        self.assertEqual(len(manifest["assets"]), 2)  # comment pic + release zip
        self.assertEqual(manifest["failures"], [])
        # PR json carries reviews and inline comments
        pr = json.load(open(os.path.join(self.out, "pulls/2.json")))
        self.assertEqual(pr["reviews"][0]["state"], "APPROVED")
        self.assertEqual(pr["review_comments"][0]["path"], "a.py")

    def test_pr_rows_filtered_out_of_issues(self):
        repo_dump.run("o/r", self.out, token=None, client=self.make_client())
        self.assertFalse(os.path.exists(os.path.join(self.out, "issues/2.json")))

    def test_failed_download_is_reported_not_fatal(self):
        client = self.make_client()
        def boom(path, params=None, raw=False):
            if raw:
                raise OSError("404")
            return client.pages[path], {}
        client.get = boom
        store = repo_dump.AssetStore(client, self.out)
        counts = {"issues": repo_dump.dump_issues(client, "o/r", self.out, store)}
        self.assertEqual(counts["issues"], 1)
        self.assertEqual(len(store.failures), 1)
        md = open(os.path.join(self.out, "issues/1.md")).read()
        self.assertIn(ATT, md)  # original URL kept when download fails

    def test_safe_name(self):
        self.assertTrue(repo_dump.safe_name(ATT, 1).startswith("001-"))
        self.assertNotIn("/", repo_dump.safe_name(IMG, 2))


if __name__ == "__main__":
    unittest.main()
