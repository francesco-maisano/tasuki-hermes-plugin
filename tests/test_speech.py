import unittest

import helpers  # noqa: F401
from _tasuki_hermes_core import speech


class SpeechTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(speech.sanitize("**Hi** [x](http://a.b) `code` — ok https://z.com/q"), "Hi x code, ok")

    def test_tool_line_uses_name_first(self):
        self.assertEqual(speech.tool_line("tasuki_calendar_events"), "Checking your calendar.")
        self.assertEqual(speech.tool_line("tasuki_mail_read"), "Checking your email.")
        self.assertEqual(speech.tool_line("tasuki_food_day"), "Checking your food log.")
        self.assertEqual(speech.tool_line("zzz"), "One moment, working on that.")

    def test_proposal(self):
        t = 'ok\nPROPOSAL: {"action":"delete","summary":"Delete 4","items":["a","b","c","d"]} trailing'
        p = speech.parse_proposal(t)
        self.assertEqual((p["action"], p["items"]), ("delete", ["a", "b", "c", "d"]))
        self.assertEqual(speech.question(p["summary"], p["items"]), "Delete 4: a, b, c and 1 more. Should I go ahead?")
        self.assertIsNone(speech.parse_proposal("PROPOSAL: {bad"))
        self.assertNotIn("PROPOSAL", speech.strip_proposal(t))

    def test_persona_files_are_the_v2_copies(self):
        self.assertIn("at most three short sentences", speech.turn_extra())
        self.assertEqual(speech.PERSONA_VERSION, "v2")


if __name__ == "__main__":
    unittest.main()
