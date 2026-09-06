from pm_engine.frontmatter import _fallback_parse, dump_frontmatter, parse_frontmatter


def test_parses_skill_frontmatter():
    doc = parse_frontmatter('---\nname: create-prd\ndescription: "Create a PRD. Use when writing a PRD."\n---\n\n# Title\nbody')
    assert doc.meta["name"] == "create-prd"
    assert doc.meta["description"].startswith("Create a PRD")
    assert doc.body.startswith("# Title")


def test_command_frontmatter_with_hyphenated_keys():
    doc = parse_frontmatter('---\ndescription: Do a thing\nargument-hint: "[a|b] <ctx>"\nallowed-tools: Read, Grep, Bash(git log:*)\n---\nbody')
    assert doc.meta["argument-hint"] == "[a|b] <ctx>"
    assert doc.meta["allowed-tools"].startswith("Read")


def test_no_frontmatter():
    doc = parse_frontmatter("# Just markdown\n")
    assert doc.meta == {}
    assert doc.body == "# Just markdown\n"


def test_unterminated_frontmatter_is_body():
    doc = parse_frontmatter("---\nname: x\n")
    assert doc.meta == {}


def test_fallback_parser_handles_quotes_lists_and_comments():
    meta = _fallback_parse("# comment\nname: 'quoted'\ntags: [a, b, 'c']\nempty: |\n")
    assert meta["name"] == "quoted"
    assert meta["tags"] == ["a", "b", "c"]
    assert meta["empty"] == ""


def test_roundtrip_dump():
    text = dump_frontmatter({"name": "x-y", "description": "Has: colon"}, "# body\n")
    doc = parse_frontmatter(text)
    assert doc.meta["name"] == "x-y"
    assert doc.meta["description"] == "Has: colon"
    assert doc.body.strip() == "# body"
