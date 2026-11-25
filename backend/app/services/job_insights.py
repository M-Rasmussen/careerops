"""Deterministic, conservative facts read from a job description. No AI, no guessing.

- technologies: names that literally appear in the text (word-bounded).
- experience: lines that state a number of years.
- required / preferred: bullet lines under headings that clearly say so. If the posting has no such headings,
  both lists stay empty and `sections_found` is False; nothing is reclassified.
"""

import re

# (label, pattern). Case-insensitive unless the pattern sets its own flags; "Go" must be capitalized.
TECHNOLOGIES: tuple[tuple[str, str], ...] = (
    ("Python", r"python"), ("Java", r"java(?!\s*script)"), ("JavaScript", r"javascript"), ("TypeScript", r"typescript"),
    ("Go", r"golang|(?<=[,/(] )(?-i:Go)|(?<=[,/(])(?-i:Go)|(?-i:Go)(?=\s*[,/)])"), ("C#", r"c#"), ("C++", r"c\+\+"), (".NET", r"\.net(?:\s*core)?"),
    ("Ruby", r"ruby"), ("Rails", r"ruby on rails|(?-i:Rails)"), ("PHP", r"php"), ("Kotlin", r"kotlin"), ("Scala", r"scala"),
    ("Rust", r"(?-i:Rust)"), ("Swift", r"(?-i:Swift)"), ("SQL", r"sql"), ("PostgreSQL", r"postgres(?:ql)?"), ("MySQL", r"mysql"),
    ("MongoDB", r"mongodb|mongo"), ("Redis", r"redis"), ("Kafka", r"kafka"), ("Spark", r"apache spark|(?-i:Spark)"), ("Airflow", r"airflow"),
    ("Snowflake", r"snowflake"), ("AWS", r"aws|amazon web services"), ("Azure", r"azure"), ("GCP", r"gcp|google cloud"),
    ("Docker", r"docker"), ("Kubernetes", r"kubernetes|k8s"), ("Terraform", r"terraform"), ("Ansible", r"ansible"),
    ("Jenkins", r"jenkins"), ("GitHub Actions", r"github actions"), ("GitLab CI", r"gitlab ci"), ("Linux", r"linux"),
    ("Bash", r"bash"), ("React", r"react(?:\.js|js)?"), ("Angular", r"angular"), ("Vue", r"vue(?:\.js)?"),
    ("Node.js", r"node(?:\.js|js)"), ("Django", r"django"), ("Flask", r"(?-i:Flask)"), ("FastAPI", r"fastapi"),
    ("Spring", r"spring\s*boot|spring\s*framework"), ("GraphQL", r"graphql"), ("REST APIs", r"rest(?:ful)?\s+apis?"),
    ("gRPC", r"grpc"), ("Microservices", r"micro-?services"), ("Selenium", r"selenium"), ("Cypress", r"cypress"),
    ("Playwright", r"playwright"), ("pytest", r"pytest"), ("JUnit", r"junit"), ("Datadog", r"datadog"),
    ("Prometheus", r"prometheus"), ("Grafana", r"grafana"), ("Splunk", r"splunk"), ("Elasticsearch", r"elasticsearch"),
    ("CI/CD", r"ci\s*/\s*cd"),
)
_TECH = [(label, re.compile(rf"(?<![\w.#+])(?:{p})(?![\w#+])", re.IGNORECASE)) for label, p in TECHNOLOGIES]
# "3+ years", "3-5 years", "5 plus yrs". Counts only with experience context (see experience_phrases).
_YEARS = re.compile(
    r"(?<![\w.$])(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs?)\b(?:['’]s?)?",
    re.IGNORECASE,
)
_EXPERIENCE_CONTEXT = re.compile(r"experience|\bexp\b|professional|hands[- ]on|industry|background", re.IGNORECASE)
_FOLLOWED_BY = re.compile(
    r"\s*(?:of|in|with|building|developing|working|designing|leading|writing|programming|engineering|doing|as|using)\b",
    re.IGNORECASE,
)
# Sentences about company history, benefits, or the company's own experience are not candidate requirements.
# Checked per sentence, so "We're looking for someone with 5+ years of experience" still counts.
_NOT_EXPERIENCE = re.compile(
    r"\bago\b|\byears old\b|founded|anniversary|in business|\bvest|warranty|retention|"
    r"\bwe(?:['’]ve| have| has| had)\b|\bour (?:company|team|firm|founders?) (?:has|have)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")
MAX_PHRASE = 70

REQUIRED_HEADINGS = ("requirements", "required qualifications", "minimum qualifications", "basic qualifications",
                     "qualifications", "what you'll need", "what you will need", "what you need", "must have",
                     "must-have", "you have", "required skills")
PREFERRED_HEADINGS = ("preferred", "nice to have", "nice-to-have", "bonus", "pluses", "desired", "good to have")
OTHER_HEADINGS = ("responsibilities", "what you'll do", "what you will do", "about", "benefits", "perks",
                  "compensation", "salary", "who we are", "our team", "the role", "why join", "how to apply")
MAX_ITEMS = 15


def technologies(text: str | None) -> list[str]:
    if not text:
        return []
    return [label for label, rx in _TECH if rx.search(text)]


def _heading(line: str, in_list: bool = False) -> str | None:
    """Classifies a short line as a section heading: 'preferred', 'required', 'other', or None. Inside a
    requirements list, a generic "...:" line ("Proficiency in one of:") is an item, not a new heading."""
    clean = line.strip().rstrip(":").strip().lower()
    if not clean or len(clean) > 60:
        return None
    if any(h in clean for h in PREFERRED_HEADINGS):  # "Preferred qualifications" is preferred, not required
        return "preferred"
    if any(clean.startswith(h) or clean == h for h in REQUIRED_HEADINGS):
        return "required"
    if any(clean.startswith(h) for h in OTHER_HEADINGS) or (not in_list and line.strip().endswith(":") and len(clean.split()) <= 6):
        return "other"
    return None


def requirements(text: str | None) -> dict[str, object]:
    lines = [ln.strip(" \t•·*-–") for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    required: list[str] = []
    preferred: list[str] = []
    section: str | None = None
    found = False
    for line in lines:
        kind = _heading(line, in_list=section in ("required", "preferred"))
        if kind:
            section = kind
            found = found or kind in ("required", "preferred")
            continue
        target = required if section == "required" else preferred if section == "preferred" else None
        if target is not None and len(target) < MAX_ITEMS:
            target.append(line[:400])
    experience = [ln[:300] for ln in lines if experience_phrases(ln)][:5]
    return {
        "sections_found": found,
        "required": required,
        "preferred": preferred,
        "experience": experience,
        "technologies": technologies(text),
    }


def experience_phrases(text: str | None) -> list[str]:
    """Explicitly stated years of experience, as short phrases ("3+ years of Python experience", "3–5 years in
    backend development"). A number of years counts only when the sentence is about experience; nothing is
    inferred from titles or seniority words."""
    out: list[str] = []
    for line in _SENTENCE.split(text or ""):
        if _NOT_EXPERIENCE.search(line):
            continue
        matches = list(_YEARS.finditer(line))
        for i, m in enumerate(matches):
            if not (_EXPERIENCE_CONTEXT.search(line) or _FOLLOWED_BY.match(line, m.end())):
                continue
            stop = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            phrase = re.split(r"[.;(\n]|,\s*(?:or|and)\b", line[m.start():stop])[0].strip(" ,:-–")
            phrase = re.sub(r"(\d)\s*(?:-|—|to)\s*(\d)", r"\1–\2", phrase)
            if len(phrase) > MAX_PHRASE:
                phrase = phrase[:MAX_PHRASE].rsplit(" ", 1)[0] + "…"
            if phrase.lower() not in (p.lower() for p in out):
                out.append(phrase)
    return out


def required_experience(text: str | None, limit: int = 3) -> list[str]:
    """Experience phrases for a job card: ones under a requirements heading first, then the rest of the text."""
    reqs = requirements(text)
    ordered = experience_phrases("\n".join(reqs["required"])) + experience_phrases(text)  # type: ignore[arg-type]
    seen: list[str] = []
    for p in ordered:
        if p.lower() not in (x.lower() for x in seen):
            seen.append(p)
    return seen[:limit]


# --- Non-vacancy postings -----------------------------------------------------------------------
# Conservative: a posting is excluded only on explicit evidence that it is not a specific opening. Single words such
# as "talent", "prospect", "general", "pool" or "evergreen" never decide it (real jobs use them, e.g. recruiters).
_NV_TITLE = re.compile(
    r"\bexpression of interest\b|\b(?:join|register for|sign up for) our talent (?:community|network|pool)\b|"
    r"^talent (?:community|network)\b|\btalent (?:community|network)$|\binterest (?:form|registration)\b|"
    r"\bregister your interest\b",
    re.IGNORECASE,
)
_NV_STATEMENT = re.compile(
    r"\b(?:this|the) (?:posting|listing|role|position|requisition) is not (?:a|an|tied to a|for a) "
    r"(?:current|open|active|specific|actual) (?:opening|vacancy|position|role|job)\b",
    re.IGNORECASE,
)
_NV_SOURCE = re.compile(r"\b(?:prospects|talent (?:community|network|pool))\b", re.IGNORECASE)
# Weak alone: evergreen roles for a specific job also say this ("a future opportunity that may not be immediately
# available"), and those are real hiring. Counts only with a prospect / talent-pool source label.
_NV_FUTURE_ONLY = re.compile(
    r"\bstay in touch\b[^.\n]{0,200}\b(?:when|if|once|as)\b[^.\n]{0,120}\bopportunit|"
    r"\bthis is a future opportunity\b[^.\n]{0,120}\bnot (?:be )?immediately available\b",
    re.IGNORECASE,
)


def non_vacancy_reason(title: str | None, description: str | None, source_company: str | None = None) -> str | None:
    """Why this posting is not a real vacancy (a talent pool, interest form, or prospect pool), or None."""
    title = title or ""
    text = description or ""
    if m := _NV_TITLE.search(title):
        return f'Title says it is a candidate interest form: "{m.group(0)}"'
    if m := _NV_STATEMENT.search(text):
        return f'Posting states it is not a current opening: "{m.group(0)[:160]}"'
    if source_company and (s := _NV_SOURCE.search(source_company)) and (k := _NV_FUTURE_ONLY.search(text)):
        return f'Source labels it "{source_company}" ({s.group(0)}) and it only offers to "{k.group(0)[:120]}"'
    return None
