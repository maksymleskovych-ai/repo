# TWS lead collector

A daily script that does the cheap part of lead generation, so the Claude routine only has to judge.

**Flow**

1. **02:17 UTC**: GitHub Actions runs `leadgen/collect.py`:
   - gathers companies from remote job boards (Remotive, RemoteOK, Himalayas, Arbeitnow), news RSS (EU-Startups, Tech.eu, Crunchbase News, Fierce Healthcare, MobiHealthNews) and the CMS pledge pages;
   - drops anything in `leadgen/data/kill_list.txt` or already passed on (`leadgen/data/seen.csv`);
   - probes each company's own ATS (Ashby, Greenhouse, Lever);
   - kills on the cheap gates: staffing wording, Java/.NET/Angular/PHP/Ruby stack, engineering roles in EE/India/Vietnam, local language required, too many open roles, on-site only;
   - scores the rest and writes the top 30 to [`output/latest.csv`](output/latest.csv), plus a dated copy.
2. **04:00 UTC**: the Claude routine reads `output/latest.csv`, checks what a script can't (ownership, who pays, size, named contact), and delivers the Pipedrive CSV, the registry and the email.

**Everyday tweaks**

- Never want to see a company again: add a line to `leadgen/data/kill_list.txt`.
- Run it now: Actions → "Collect lead candidates" → Run workflow.
- Change the count: the `top` input, or `--top` in the workflow.

Tests: `cd leadgen && python -m unittest test_collect`.
