import datetime as dt
import unittest

import collect as c


class NormTest(unittest.TestCase):
    def test_strips_legal_suffixes_and_tlds(self):
        self.assertEqual(c.norm("Talon.One GmbH"), "talonone")
        self.assertEqual(c.norm("beyonnex.io"), "beyonnex")
        self.assertEqual(c.norm("Acme AB"), "acme")
        self.assertEqual(c.norm("Foo Health, Inc."), "foohealth")

    def test_slugs(self):
        self.assertEqual(c.slugs("Counsel Health"), ["counselhealth", "counsel-health", "counsel"])
        self.assertEqual(c.slugs("Linear"), ["linear"])


class HeadlineTest(unittest.TestCase):
    def test_funding(self):
        self.assertEqual(c.classify_headline("Acme raises €12M Series A to automate invoices")[:2], ("funding", "Acme"))
        self.assertEqual(
            c.classify_headline("Berlin-based Foo, the invoicing startup, secures $5 million seed")[:2][0], "funding"
        )

    def test_acquisition(self):
        kind, buyer, target = c.classify_headline("Payfit acquires Zelt to expand in the UK")
        self.assertEqual((kind, buyer, target), ("acquirer", "Payfit", "Zelt"))
        self.assertEqual(c.classify_headline("Zelt acquired by Payfit")[:2], ("acquired", "Zelt"))

    def test_irrelevant(self):
        self.assertIsNone(c.classify_headline("The 10 hottest startups in Lisbon"))


def cand(name="Acme", jobs=(), job_texts=(), signals=()):
    x = c.Candidate(name=name)
    x.ats_url = "https://jobs.ashbyhq.com/acme" if jobs else ""
    x.ats_jobs = list(jobs)
    x.job_texts = list(job_texts)
    x.signals = list(signals)
    return x


def job(title="Senior Backend Engineer", text="TypeScript, Node.js, AWS, integrations", loc="Remote, Europe",
        remote=True, days=10):
    return {"title": title, "text": text, "location": loc, "remote": remote,
            "opened": c.TODAY - dt.timedelta(days=days)}


class GateTest(unittest.TestCase):
    def test_good_candidate_passes_and_scores(self):
        x = cand(jobs=[job(days=90)])
        c.evaluate(x, set(), set())
        self.assertEqual(x.kill, "")
        self.assertGreaterEqual(x.score, 5)
        self.assertEqual(x.facts["oldest_days"], 90)

    def test_bad_stack(self):
        x = cand(jobs=[job(text="Java, Spring Boot, Angular"), job(text="Java microservices")])
        c.evaluate(x, set(), set())
        self.assertTrue(x.kill.startswith("G1"))

    def test_javascript_is_not_java(self):
        x = cand(jobs=[job(text="JavaScript and TypeScript, React")])
        c.evaluate(x, set(), set())
        self.assertEqual(x.kill, "")

    def test_offshore_team(self):
        x = cand(jobs=[job(loc="Kyiv, Ukraine")])
        c.evaluate(x, set(), set())
        self.assertTrue(x.kill.startswith("G3"))

    def test_onsite_only(self):
        x = cand(jobs=[job(loc="Amsterdam", remote=False)])
        c.evaluate(x, set(), set())
        self.assertEqual(x.kill, "on-site only (own ATS)")

    def test_language(self):
        x = cand(jobs=[job(text="Node.js. Fließend Deutsch erforderlich")])
        c.evaluate(x, set(), set())
        self.assertTrue(x.kill.startswith("language"))

    def test_kill_list_and_acquired(self):
        x = cand(name="Storyblok GmbH", jobs=[job()])
        c.evaluate(x, {c.norm("Storyblok")}, set())
        self.assertEqual(x.kill, "kill list")
        y = cand(name="Zelt", jobs=[job()])
        c.evaluate(y, set(), {"zelt"})
        self.assertTrue(y.kill.startswith("G2"))

    def test_funding_only_without_ats_is_dropped(self):
        x = cand(signals=[c.Signal("funding", c.TODAY, "Acme raises $5M", "u")])
        c.evaluate(x, set(), set())
        self.assertEqual(x.kill, "no ATS and weak signal")

    def test_acquirer_without_ats_survives(self):
        x = cand(signals=[c.Signal("acquirer", c.TODAY, "Acme acquires Foo", "u")])
        c.evaluate(x, set(), set())
        self.assertEqual(x.kill, "")
        self.assertEqual(c.offer_hint(x), "план злиття")


class RssTest(unittest.TestCase):
    def test_parse(self):
        xml = """<?xml version="1.0"?><rss><channel>
        <item><title>Acme raises &#8364;3M seed</title><link>https://x/1</link>
        <pubDate>Wed, 24 Sep 2026 08:00:00 +0000</pubDate></item></channel></rss>"""
        items = list(c.rss_items(xml))
        self.assertEqual(items[0][0], "Acme raises €3M seed")
        self.assertEqual(items[0][2], dt.date(2026, 9, 24))


if __name__ == "__main__":
    unittest.main()
