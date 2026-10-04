"""Contract and safety checks for the offline cross-app POC."""
import unittest

from poc.cross_app import FixtureOpenBot, Member, Refused, Source, evidence_capture, openmuse_view, plan, read_web


class CrossAppPocTests(unittest.TestCase):
    def setUp(self):
        self.actor = Member("h1", "m1", "guardian")
        self.source = Source("web", "https://school.example/notice/42", "m1", "h1",
                             frozenset({"school.example"}))
        self.page = {"url": self.source.url, "title": "Notice", "text": "Consent due Friday.",
                     "truncated": False}

    def test_browser_route_and_review_candidate(self):
        transport = FixtureOpenBot(self.page)
        page = read_web(self.actor, self.source, "family-bot", transport)
        self.assertEqual([method for method, _ in transport.calls], ["GET", "POST", "GET"])
        self.assertEqual([path.rsplit("/", 1)[-1] for _, path in transport.calls],
                         ["status", "navigate", "read"])
        candidate = evidence_capture(self.actor, self.source, page)
        self.assertEqual(candidate["visibility"], "private")
        self.assertEqual(candidate["status"], "awaiting_review")
        self.assertEqual(openmuse_view(candidate)["actions"], ["review", "discard"])

    def test_other_member_cannot_touch_browser(self):
        transport = FixtureOpenBot(self.page)
        with self.assertRaises(Refused):
            read_web(Member("h1", "m2", "guardian"), self.source, "family-bot", transport)
        self.assertEqual(transport.calls, [])

    def test_disallowed_url_does_not_reach_browser(self):
        transport = FixtureOpenBot(self.page)
        source = Source("web", "https://private.example/", "m1", "h1", frozenset({"school.example"}))
        with self.assertRaises(Refused):
            read_web(self.actor, source, "family-bot", transport)
        self.assertEqual(transport.calls, [])

    def test_redirect_outside_grant_refused(self):
        page = dict(self.page, url="https://other.example/notice/42")
        with self.assertRaises(Refused):
            read_web(self.actor, self.source, "family-bot", FixtureOpenBot(page))

    def test_truncated_page_cannot_be_promoted(self):
        with self.assertRaises(Refused):
            evidence_capture(self.actor, self.source, dict(self.page, truncated=True))

    def test_mobile_requires_grant_and_live_verification(self):
        app = Source("app_only", None, "m1", "h1")
        self.assertEqual(plan(self.actor, app)["state"], "requires_member_grant")
        granted = Source("app_only", None, "m1", "h1", explicit_mobile_grant=True)
        self.assertEqual(plan(self.actor, granted)["state"], "requires_android_device_and_agent")
        self.assertEqual(plan(self.actor, granted, android_device_connected=True)["state"],
                         "requires_live_agent_verification")


if __name__ == "__main__":
    unittest.main()
