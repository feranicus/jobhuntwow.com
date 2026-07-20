"""Job scouting. v0.1 returns realistic sample jobs; the real LinkedIn + company-site
scout (reusing linkedin_verifier.py stealth patterns) plugs in here later."""

SAMPLE = [
    {"id": "wd-1",  "company": "Delivery Hero", "role": "Senior Data Engineer", "location": "Berlin · Remote",
     "ats": "Workday",        "url": "https://deliveryhero.wd3.myworkdayjobs.com/...", "fit": 86},
    {"id": "sf-1",  "company": "SAP",           "role": "Analytics Engineer",   "location": "Walldorf · Hybrid",
     "ats": "SuccessFactors", "url": "https://careers.sap.com/...",               "fit": 79},
    {"id": "pe-1",  "company": "Personio",      "role": "BI Engineer",          "location": "Munich",
     "ats": "Personio",       "url": "https://personio.jobs.personio.com/...",    "fit": 81},
    {"id": "hb-1",  "company": "HelloFresh",    "role": "Data Engineer",        "location": "Berlin",
     "ats": "Greenhouse",     "url": "https://boards.greenhouse.io/...",          "fit": 74},
]

def search(query: str, location: str = "", remote: bool = True):
    q = (query or "").lower()
    jobs = [j for j in SAMPLE if not q or q in j["role"].lower() or q in j["company"].lower()]
    return {"query": query, "count": len(jobs), "jobs": jobs, "note": "v0.1 sample data - real LinkedIn/ATS scout wires in here"}
