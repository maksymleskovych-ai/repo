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

    def test_europe_scores_higher(self):
        eu, us = cand(jobs=[job(loc="Remote, Europe")]), cand(jobs=[job(loc="Remote, US")])
        for x in (eu, us):
            c.evaluate(x, set(), set())
        self.assertEqual(eu.score - us.score, 2)

    def test_too_many_eng_roles(self):
        x = cand(jobs=[job() for _ in range(13)])
        c.evaluate(x, set(), set())
        self.assertTrue(x.kill.startswith("G5"))

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


class ParserTest(unittest.TestCase):
    def test_news_name_prefixes(self):
        self.assertEqual(c.clean_news_name("Amsterdam-based Duqu"), "Duqu")
        self.assertEqual(c.clean_news_name("Italy’s Clastix"), "Clastix")
        self.assertEqual(c.clean_news_name("London traveltech Stasher"), "Stasher")
        self.assertEqual(c.clean_news_name("Berlin-based mika"), "mika")
        self.assertEqual(c.clean_news_name("Sticker Mule"), "Sticker Mule")
        self.assertEqual(c.classify_headline("Paris-based Primo raises €4M seed")[:2], ("funding", "Primo"))

    def test_cms_takes_only_offsite_links(self):
        page = """<nav><a href="https://www.cms.gov/x">Overview</a></nav><main>
        <h3>Patient Facing Apps</h3>
        <a href="https://www.cms.gov/pledge">COMPANY PLEDGE</a>
        <a href="https://acmehealth.com/"><img src="a.png" alt="Acme Health logo"></a>
        <a href="https://www.vinyl.health">Vinyl Health</a>
        <a href="https://www.youtube.com/watch">Pledge Demo Showcase</a></main>"""
        self.assertEqual(c.cms_companies(page), {"Acme Health": "https://acmehealth.com",
                                                 "Vinyl Health": "https://vinyl.health"})

    def test_hn_first_line(self):
        text = 'Acme (<a href="https://acme.io">https://acme.io</a>) | Senior Backend Engineer | REMOTE (EU) | Full-time<p>We build...'
        self.assertEqual(c.hn_first_line(text), ("Acme", "https://acme.io", "Senior Backend Engineer | REMOTE (EU) | Full-time"))

    def test_eng_titles(self):
        for t in ("Senior Backend Engineer", "DevOps Engineer", "Member of Technical Staff", "Engineering Manager"):
            self.assertTrue(c.is_eng(t), t)
        for t in ("Engineer Estimator", "Architectural Designer", "Sales Engineer", "Sr Solutions Architect",
                  "Senior Data Engineer & Consultant"):
            self.assertFalse(c.is_eng(t), t)

    def test_staffing_name(self):
        x = cand(name="Infoplus Technologies Inc", job_texts=["Senior Backend Engineer\nNode"])
        c.evaluate(x, set(), set())
        self.assertTrue(x.kill.startswith("G0"))


class StageOneTest(unittest.TestCase):
    def test_infra_product_killed(self):
        x = cand(name="Railway", jobs=[job(text="Railway is an infrastructure platform for developers. "
                                           "We are building the deployment platform for the next decade.")])
        c.evaluate(x, set(), set())
        self.assertEqual(x.kill, "product is infrastructure/devtools/security")

    def test_saas_using_postgres_not_killed(self):
        x = cand(name="Acme", jobs=[job(text="Acme is a B2B invoicing platform for wholesalers. "
                                        "Our platform runs on Postgres, Node.js and AWS with many integrations.")])
        c.evaluate(x, set(), set())
        self.assertEqual(x.kill, "")
        self.assertTrue(x.about.startswith("Acme is a B2B invoicing platform"))

    def test_public_company(self):
        x = cand(name="Future PLC", jobs=[job()])
        c.evaluate(x, set(), set())
        self.assertTrue(x.kill.startswith("G2/G5"))

    def test_email_deobfuscation_and_preference(self):
        t = "Apply at jobs@acme.io or email me directly: jane [at] acme [dot] io"
        self.assertEqual(c.find_email(t, "https://acme.io"), "jane@acme.io")
        self.assertEqual(c.find_email("send CV to careers@acme.io", ""), "careers@acme.io")
        self.assertEqual(c.find_email("no address here", ""), "")

    def test_contact_patterns(self):
        self.assertEqual(c.find_contact("Hi, I'm Jane Doe, the CTO. We are hiring."), ("Jane Doe", "CTO"))
        self.assertEqual(c.find_contact("Questions? - Tom, co-founder"), ("Tom", "co-founder"))
        self.assertEqual(c.find_contact("We are a small team building invoices."), ("", ""))

    def test_hn_about(self):
        text = "Acme | Backend | REMOTE (EU)<p>Acme automates order processing for 400 wholesalers across Europe.<p>Apply: jobs@acme.io"
        self.assertEqual(c.hn_about(text), "Acme automates order processing for 400 wholesalers across Europe.")

    def test_contact_in_output(self):
        x = cand(jobs=[job()])
        x.contact = {"name": "Jane", "title": "CTO", "email": "jane@acme.io", "source": "https://hn/1", "hn_user": "jdoe"}
        c.evaluate(x, set(), set())
        row = c.row_for(1, x)
        self.assertEqual((row["contact_name"], row["contact_email"]), ("Jane", "jane@acme.io"))
        self.assertNotIn("named tech contact", row["to_verify"])


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
