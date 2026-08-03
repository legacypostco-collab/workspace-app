from unittest import TestCase

from consolidator_site.observability import scrub_sentry_event


class SentryScrubbingTests(TestCase):
    def test_authentication_and_secret_values_are_removed(self):
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer private",
                    "User-Agent": "test",
                    "Cookie": "sessionid=private",
                },
                "data": {"password": "private", "query": "visible"},
            },
            "user": {"id": "private", "email": "private@example.test"},
            "extra": {"api_key": "private", "count": 2},
        }

        scrubbed = scrub_sentry_event(event, {})

        self.assertEqual(scrubbed["request"]["headers"]["Authorization"], "[Filtered]")
        self.assertEqual(scrubbed["request"]["headers"]["Cookie"], "[Filtered]")
        self.assertEqual(scrubbed["request"]["data"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["api_key"], "[Filtered]")
        self.assertNotIn("user", scrubbed)
