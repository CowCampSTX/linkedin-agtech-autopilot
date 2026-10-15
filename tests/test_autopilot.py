import argparse
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopilot import cli as lp  # noqa: E402
from autopilot import core as li  # noqa: E402

CFG = li.load_cfg()
CFG = {**CFG, "privacy": {"private_terms": ["Acme Ranch"]}}   # a test-only private term

GOOD = """Would you trust a fence you can't see with 400 head of cattle?

A virtual fencing company just raised new money to put GPS collars on more herds.

Most people's first guess is that the hard part is the tech.

The story says otherwise: the collars work, and the slow part is getting ranchers to change how they move cattle.

That matches what I see working cattle. A tool has to save a day of work in the first month, or it ends up in the barn.

Three things I'd want to know before buying in:
- What happens when the signal drops in a draw
- Who fixes a collar at 5 a.m.
- What it costs per head per year, all in

If you've run virtual fencing on a real operation, what surprised you most in the first season?

#AgTech #Livestock #VirtualFencing #Ranching"""


class CheckPostTests(unittest.TestCase):
    def test_good_post_passes(self):
        self.assertEqual(li.check_post(GOOD, CFG), [])

    def test_link_in_body_blocked(self):
        self.assertTrue(any("link" in i for i in li.check_post(GOOD.replace("new money", "new money https://x.com/a"), CFG)))

    def test_needs_closing_question(self):
        text = GOOD.replace("what surprised you most in the first season?", "tell me about it.")
        self.assertIn("no closing question for comments", li.check_post(text, CFG))

    def test_hashtag_count(self):
        self.assertTrue(any("hashtags" in i for i in li.check_post(GOOD.replace(" #Ranching", " #A #B #C"), CFG)))

    def test_banned_and_private_terms(self):
        issues = li.check_post(GOOD.replace("A tool has", "A game-changer tool from Acme Ranch has"), CFG)
        self.assertTrue(any("banned" in i for i in issues))
        self.assertTrue(any("private" in i for i in issues))

    def test_job_seeking_language_blocked(self):
        issues = li.check_post(GOOD.replace("A tool has", "I'm open to work in AgTech. A tool has"), CFG)
        self.assertIn("banned phrase: 'open to work'", issues)

    def test_long_hook(self):
        self.assertTrue(any("hook" in i for i in li.check_post("x" * 200 + "\n" + GOOD, CFG)))


class CommentaryTests(unittest.TestCase):
    def test_escapes_reserved_and_keeps_hashtags(self):
        out = li.to_commentary("Cost (per head) is $40 @ scale #AgTech")
        self.assertEqual(out, r"Cost \(per head\) is $40 \@ scale {hashtag|\#|AgTech}")


class TokenTests(unittest.TestCase):
    def test_pasted_token_whitespace_and_quotes_stripped(self):
        self.assertEqual(li.LinkedIn(' "abc123"\n', "202605").token, "abc123")


class FeedTests(unittest.TestCase):
    RSS = b"""<?xml version="1.0"?><rss><channel>
      <item><title>Startup raises $20M for cattle sensors</title><link>https://www.ex.com/a/</link>
        <description>&lt;p&gt;Livestock data for ranch use&lt;/p&gt;</description><pubDate>Mon, 12 Oct 2026 10:00:00 GMT</pubDate></item>
      <item><title>Old drone story</title><link>https://ex.com/b</link><description>drone</description>
        <pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate></item>
      <item><title>Recipe: corn bread</title><link>https://ex.com/c</link><description>recipe</description>
        <pubDate>Tue, 13 Oct 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""

    def test_parse_pick_and_dedupe(self):
        items = li.parse_feed(self.RSS, "Ex")
        self.assertEqual(items[0]["summary"], "Livestock data for ranch use")
        now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        picked = li.pick_stories(items, CFG, set(), now=now)
        self.assertEqual([p["url"] for p in picked], ["https://www.ex.com/a/"])   # stale + skip-word stories dropped
        self.assertEqual(li.pick_stories(items, CFG, {"ex.com/a"}, now=now), [])  # already drafted

    def test_atom(self):
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Drone spraying rules</title>
          <link href="https://ex.com/d"/><summary>precision ag</summary><updated>2026-10-13T08:00:00Z</updated></entry></feed>"""
        self.assertEqual(li.parse_feed(atom, "Ex")[0]["url"], "https://ex.com/d")


class GateTests(unittest.TestCase):
    def draft(self, status):
        return {"id": "AG-0001", "status": status, "text": GOOD, "first_comment": "Source", "created_at": "2026-10-14"}

    def test_pending_needs_autopost(self):
        cfg = {**CFG, "linkedin": {**CFG["linkedin"], "autopost": False}}
        self.assertFalse(lp.postable(self.draft("pending"), cfg))
        self.assertTrue(lp.postable(self.draft("approved"), cfg))
        cfg["linkedin"]["autopost"] = True
        self.assertTrue(lp.postable(self.draft("pending"), cfg))
        self.assertFalse(lp.postable({**self.draft("pending"), "text": "too short?"}, cfg))

    def test_autopost_ships_off(self):
        self.assertFalse(CFG["linkedin"]["autopost"])

    def test_dry_run_without_confirm(self):
        d = self.draft("approved")
        lp.publish(argparse.Namespace(confirm=False), CFG, d)
        self.assertEqual(d["status"], "approved")

    def test_once_a_day(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        cfg = {**CFG, "linkedin": {**CFG["linkedin"], "post_days": lp.DAYS, "post_time_local": "00:00"}}
        self.assertEqual(lp.due(cfg, [{"posted_at": recent}]), (False, "already posted today"))
        self.assertTrue(lp.due(cfg, [])[0])


class PublishTests(unittest.TestCase):
    """The live path, with LinkedIn's API replaced by a fake: post, then the source link as the first comment."""

    def test_posts_then_comments(self):
        calls = []

        def fake_call(self, method, path, body=None, rest=True):
            calls.append((method, path, body))
            return ({"x-restli-id": "urn:li:share:123"} if path == "/rest/posts" else {}), {}

        d = {"id": "AG-0001", "status": "approved", "text": GOOD, "first_comment": "Source: Example News\nhttps://ex.com/a",
             "created_at": "2026-10-14"}
        env = {"LINKEDIN_ACCESS_TOKEN": "t", "LINKEDIN_PERSON_URN": "urn:li:person:abc", "LINKEDIN_TOKEN_EXPIRES": ""}
        with mock.patch.object(li.LinkedIn, "_call", fake_call), mock.patch.dict("os.environ", env):
            lp.publish(argparse.Namespace(confirm=True), CFG, d)
        self.assertEqual(d["status"], "posted")
        self.assertEqual(d["post_url"], "https://www.linkedin.com/feed/update/urn:li:share:123/")
        self.assertEqual([c[1] for c in calls], ["/rest/posts", "/rest/socialActions/urn%3Ali%3Ashare%3A123/comments"])
        self.assertIn("{hashtag|\\#|AgTech}", calls[0][2]["commentary"])

    def test_api_error_marks_failed(self):
        def boom(self, *a, **k):
            raise li.LinkedInError("LinkedIn POST /rest/posts -> 401: expired")

        d = {"id": "AG-0002", "status": "approved", "text": GOOD, "first_comment": "", "created_at": "2026-10-14"}
        with mock.patch.object(li.LinkedIn, "_call", boom), \
                mock.patch.dict("os.environ", {"LINKEDIN_ACCESS_TOKEN": "t", "LINKEDIN_PERSON_URN": "urn:li:person:abc"}):
            lp.publish(argparse.Namespace(confirm=True), CFG, d)
        self.assertEqual(d["status"], "failed")


if __name__ == "__main__":
    unittest.main()
