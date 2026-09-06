import pytest

from pm_engine.search import tokenize

# Prompts are the upstream README's own examples → the expected skill is the one it documents.
README_EXAMPLES = [
    ("What are the riskiest assumptions for our AI writing assistant idea?", "identify-assumptions"),
    ("Help me build an Opportunity Solution Tree for improving user activation", "opportunity-solution-tree"),
    ("Prioritize these 12 feature requests from our enterprise customers", "analyze-feature-requests"),
    ("How large a sample do I need for 95% confidence with a 2% MDE?", "ab-test-analysis"),
    ("What's the best beachhead segment for a developer productivity tool?", "beachhead-segment"),
    ("Design a growth loop for a B2B SaaS with a freemium tier", "growth-loops"),
    ("Define our ICP for an AI-powered HR screening platform", "ideal-customer-profile"),
    ("Brainstorm 5 positioning angles that differentiate us from Notion", "positioning-ideas"),
    ("What's a good North Star Metric for a two-sided marketplace?", "north-star-metric"),
    ("Generate value prop statements for our sales team's pitch deck", "value-prop-statements"),
    ("Review my PM resume against best practices", "review-resume"),
    ("Check this product announcement for grammar and clarity", "grammar-check"),
    ("What documentation does my Supabase app need before someone can review it?", "shipping-artifacts"),
    ("Run a pre-mortem on our Q3 launch plan", "pre-mortem"),
    ("Write OKRs for the growth team", "brainstorm-okrs"),
    ("Draft an NDA between Acme and a contractor", "draft-nda"),
    ("SWOT analysis for our analytics product", "swot-analysis"),
    ("Porter's five forces for the CRM market", "porters-five-forces"),
    ("Estimate the market size TAM SAM SOM for meal kits", "market-sizing"),
    ("Create user personas from these survey results", "user-personas"),
    ("Summarize this customer interview transcript", "summarize-interview"),
    ("Write a SQL query for monthly active users by country", "sql-queries"),
    ("Stakeholder map for the billing migration", "stakeholder-map"),
    ("We need a privacy policy that covers GDPR", "privacy-policy"),
    ("lean canvas for a marketplace connecting freelancers", "lean-canvas"),
    ("Analyze sentiment in these app store reviews", "sentiment-analysis"),
]


def test_tokenize_stems_and_splits_hyphens():
    toks = tokenize("Pre-mortem risks; riskiest assumptions when writing PRDs")
    assert "pre-mortem" in toks and "mortem" in toks
    assert toks.count("risk") >= 1 and "assumption" in toks and "writ" in toks
    assert "when" not in toks  # stop word


@pytest.mark.parametrize("query,expected", README_EXAMPLES)
def test_readme_examples_rank_expected_skill_first(index, query, expected):
    hits = index.search(query, kind="skill", limit=3)
    assert hits, query
    assert hits[0].name.startswith(expected), [h.name for h in hits]


def test_auto_load_is_selective(index):
    picked = index.auto_load("What's a good North Star Metric for a two-sided marketplace?")
    assert [s.name for s in picked][0] == "north-star-metric"
    assert len(picked) <= 3
    assert index.auto_load("hello") == []
    assert index.auto_load("") == []


def test_commands_searchable(index):
    hits = index.search("write a PRD for SSO", kind="command", limit=3)
    assert hits[0].name == "write-prd"
    assert index.suggest_command("write a PRD for SSO").name == "write-prd"


def test_plugin_filter(index):
    hits = index.search("metrics", plugin="pm-marketing-growth")
    assert hits and all(h.plugin == "pm-marketing-growth" for h in hits)


def test_did_you_mean(index):
    dym = index.did_you_mean("/writeprd")
    assert dym and dym[0].name == "write-prd"
    assert index.did_you_mean("pm-execution:premortem")[0].name == "pre-mortem"
    assert index.did_you_mean("zzzzzz") == []


def test_explain(index):
    ex = index.explain("north star metric", "north-star-metric")
    assert ex["score"] > 0 and "north" in ex["core_terms"]
    assert "error" in index.explain("x", "nope")
