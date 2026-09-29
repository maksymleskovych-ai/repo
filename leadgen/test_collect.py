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


class LiveDataRegressionTest(unittest.TestCase):
    """Cases taken from the 2026-09-27 run."""

    def about(self, name, about, text="Senior Backend Engineer. TypeScript, Node.js, integrations."):
        x = cand(name=name, job_texts=[text])
        x.about = about
        c.evaluate(x, set(), set())
        return x

    def test_no_garbage_contact_names(self):
        for t in ("or integrations, email us directly at jobs@x.io", "Please reach out directly: a@b.io",
                  "contact us for innovative work", "message the team at hiring@x.io"):
            self.assertEqual(c.find_contact(t)[0], "", t)
        self.assertEqual(c.find_contact("Questions? Email our CTO Maria at maria@x.io"), ("Maria", "CTO"))

    def test_no_invented_emails(self):
        self.assertEqual(c.find_email("email me directly at vishnu.swaroop on LinkedIn"), "")
        self.assertEqual(c.find_email("engineers at albert we build"), "")
        self.assertEqual(c.find_email("talent+hn@cwai.co.Short teams ship"), "")
        self.assertEqual(c.find_email("work@yeet.cx"), "work@yeet.cx")

    def test_job_title_is_not_a_company(self):
        self.assertTrue(c.is_eng("Senior Python Backend Engineer"))
        self.assertTrue(c.is_eng("Lead SWE"))

    def test_devtools_from_one_line(self):
        for name, about in [
            ("Mastra", "Mastra is the open-source TypeScript framework for building AI agents (agents, workflows, memory)."),
            ("Checkly", "That instinct is Checkly's whole reason to exist. We give developers and their agents synthetic monitoring."),
            ("yeet", "Building a dynamic runtime on top of the Linux BPF sub-system."),
            ("CyberAtlas", "CyberAtlas maps the internet to help security teams discover their exposed digital infrastructure."),
        ]:
            self.assertEqual(self.about(name, about).kill, "product is infrastructure/devtools/security", name)

    def test_more_devtools_wording(self):
        for name, about in [
            ("Spacelift", "building an infrastructure orchestrator and collaborative management platform for Infrastructure-as-Code"),
            ("Estuary", "Estuary is a post-Series A startup powering right-time data replication. We're open-source, developer-first."),
        ]:
            self.assertEqual(self.about(name, about).kill, "product is infrastructure/devtools/security", name)

    def test_consultant_network_and_recruiter_names(self):
        self.assertTrue(self.about("IWConnect", "We are expanding our B2B consultant network with AI engineers.").kill.startswith("G0"))
        self.assertTrue(self.about("thehivecareers.co", "").kill.startswith("G0"))

    def test_hosted_career_site_is_not_a_slug(self):
        x = c.Candidate(name="Zepto", website="https://zepto.bamboohr.com")
        tried = []
        orig = c.ATS_PROBES
        c.ATS_PROBES = (lambda slug: tried.append(slug),)
        try:
            c.enrich(x)
        finally:
            c.ATS_PROBES = orig
        self.assertNotIn("bamboohr", tried)

    def test_agency_government_and_size(self):
        self.assertTrue(self.about("Prophet Town", "We are a people-first, boutique tech agency creating on-demand teams "
                                   "for long-standing clients.").kill.startswith("G0"))
        self.assertTrue(self.about("GovStar", "GovStar builds AI supporting U.S. national security missions.")
                        .kill.startswith("revenue from government"))
        self.assertTrue(self.about("Republic Services", "the second largest environmental services company, ~40K employees")
                        .kill.startswith("G5"))
        self.assertTrue(self.about("Princeton University", "").kill.startswith("not a product company"))

    def test_small_saas_survives(self):
        x = self.about("Great Question", "Great Question is the best way to understand your customers. "
                       "We're 2nd time founders, 35 people, closed our Series A last year.")
        self.assertEqual(x.kill, "")
        self.assertEqual(x.facts["people"], 35)

    def test_patient_monitoring_is_not_devtools(self):
        x = self.about("CareCo", "CareCo runs remote patient monitoring for 200 clinics.")
        self.assertEqual(x.kill, "")
        self.assertEqual(c.icp_hint(x), "1?")

    def test_benefits_do_not_make_it_health(self):
        x = self.about("Odin", "Odin builds workforce visibility software for construction.",
                       "Senior Platform Engineer. TypeScript. Benefits: health insurance, dental care.")
        self.assertNotEqual(c.icp_hint(x), "1?")

    def test_instructor_is_not_engineering(self):
        self.assertFalse(c.is_eng("Instructor, AI/Machine Learning (Part time)"))


class RegistryTest(unittest.TestCase):
    def test_registry_skips(self):
        keys = c.load_registry()
        self.assertIn(c.norm("Kombo"), keys)           # qualified: never again
        self.assertIn(c.norm("Storyblok"), keys)       # killed, never
        self.assertIn(c.norm("Peec AI"), keys)         # killed +6 months, still inside the window
        self.assertIn(c.norm("HelmGuard"), keys)       # re-qualified in section 8
        self.assertNotIn(c.norm("Some New Co"), keys)


class MoreLeadsTest(unittest.TestCase):
    """Misfits from the 28–29.09 verdicts, and the YC directory source."""

    def test_junk_names(self):
        for n in ("Location: London, UK", "Cologne, Germany", "SaaS Startup", "Director of Sales (Legal Ark AI)", "Stealth AI"):
            self.assertTrue(c.JUNK_NAME.search(n), n)
        for n in ("Acme, Inc", "Chief", "Lead Bank", "Cora AI", "Biobase", "VersaFeed.com", "Joi + Blokes"):
            self.assertFalse(c.JUNK_NAME.search(n), n)

    def test_nonprofit_and_institutes(self):
        x = cand(name="Crossref", job_texts=["Senior Backend Engineer. Python."])
        x.about = "Crossref is a not-for-profit membership organization for scholarly publishing."
        c.evaluate(x, set(), set())
        self.assertTrue(x.kill.startswith("non-profit"))
        y = cand(name="Child Mind Institute", job_texts=["Software Engineer"])
        c.evaluate(y, set(), set())
        self.assertTrue(y.kill.startswith("not a product company"))

    def yc_row(self, **kw):
        r = {"name": "Acme", "slug": "acme", "website": "https://acme.io", "status": "Active", "isHiring": True,
             "team_size": 30, "industry": "B2B", "subindustry": "B2B -> Operations", "regions": ["Europe"],
             "all_locations": "Berlin, Germany", "batch": "W24", "one_liner": "Order automation for wholesalers",
             "long_description": "Acme connects ERPs and webshops."}
        r.update(kw)
        return r

    def test_yc_filter(self):
        rows = [self.yc_row(),
                self.yc_row(name="Big", slug="big", team_size=500),
                self.yc_row(name="Tiny", slug="tiny", team_size=3),
                self.yc_row(name="Dev", slug="dev", subindustry="B2B -> Engineering, Product and Design"),
                self.yc_row(name="Sec", slug="sec", subindustry="B2B -> Security"),
                self.yc_row(name="Gone", slug="gone", status="Acquired"),
                self.yc_row(name="Quiet", slug="quiet", isHiring=False),
                self.yc_row(name="Asia", slug="asia", regions=["South Asia"], all_locations="Bengaluru, India"),
                self.yc_row(name="Consumer", slug="consumer", industry="Consumer", subindustry="Consumer -> Gaming")]
        self.assertEqual([r["name"] for r in c.yc_candidates(rows, c.TODAY)], ["Acme"])

    def test_yc_rotation_covers_everyone(self):
        rows = [self.yc_row(name=f"Co{i}", slug=f"co{i:03d}") for i in range(200)]
        seen = set()
        for d in range(3):
            seen |= {r["slug"] for r in c.yc_candidates(rows, c.TODAY + dt.timedelta(days=d))}
        self.assertEqual(len(seen), 200)

    def test_yc_candidate_passes_with_size(self):
        pool = c.Pool()
        orig = c.try_fetch
        c.try_fetch = lambda url, as_json=False: [self.yc_row()]
        try:
            c.collect_yc(pool)
        finally:
            c.try_fetch = orig
        x = pool.by_key["acme"]
        c.evaluate(x, set(), set())
        self.assertEqual(x.kill, "")
        self.assertEqual((x.team_size, x.website, c.trigger_of(x).kind), (30, "https://acme.io", "yc"))

    def test_yc_too_small(self):
        x = cand(name="Tiny", signals=[c.Signal("yc", None, "YC", "u")])
        x.team_size = 4
        c.evaluate(x, set(), set())
        self.assertTrue(x.kill.startswith("G5: too small"))


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
