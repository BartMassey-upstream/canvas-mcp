"""Restoring unbounded attribute/body regexes must fail bounded completion."""

import re
import subprocess
import sys

import pytest

from canvas_mcp.core.html import iter_html_tags, strip_html_tags, unescape_html
from canvas_mcp.tools.accessibility import (
    _check_content_accessibility,
    _extract_violations_from_html,
    _fix_legacy_designplus,
    _fix_orange_contrast,
    _fix_redundant_alt_prefix,
    _fix_th_scope,
)


def test_designplus_does_not_rewrite_bare_class_attribute():
    markup = '<div class id="kl_banner">Unchanged</div>'
    assert _fix_legacy_designplus(markup) == (markup, 0)


@pytest.mark.parametrize("expression", [
    "_check_content_accessibility('<span style=\"color:' + 'a' * 300_000, 'page', 7, 'Synthetic')",
    "_extract_violations_from_html('WCAG 1.1.1\\npage ' + '<' * 300_000)",
    "_check_content_accessibility('<img style=\"' * 10_000, 'page', 7, 'Synthetic')",
    "_check_content_accessibility('<a href=\"a.pdf\">' * 50_000, 'page', 7, 'Synthetic')",
    "_check_content_accessibility('<table>' * 50_000, 'page', 7, 'Synthetic')",
    "_fix_orange_contrast('style=\"' + 'background-color:#ff5f05;' * 25_000)",
])
def test_malformed_accessibility_input_finishes(expression):
    result = subprocess.run(
        [sys.executable, "-c", "from canvas_mcp.tools.accessibility import "
         "_check_content_accessibility, _extract_violations_from_html, _fix_orange_contrast; " + expression],
        capture_output=True, text=True, encoding="utf-8", timeout=8,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("markup,issue", [
    ('<img src="chart.png">', "missing_alt_text"),
    ("<h2> </h2>", "empty_heading"),
    ("<table><tr><td>Data</td></tr></table>", "table_without_headers"),
    ('<a href="/detail">click here</a>', "non_descriptive_link"),
    ('<a href="/detail"> </a>', "empty_link"),
    ('<a href="/detail">https://example.org</a>', "url_as_link_text"),
    ("<table><th>Heading</th></table>", "th_missing_scope"),
    ("<h2>First</h2><h4>Skipped</h4>", "heading_skip"),
    ('<p style="background-color:#ff5f05;color:white">Text</p>', "low_contrast"),
    ('<div class="kl_wrapper">Text</div>', "legacy_designplus"),
    ('<img src="chart.png" alt="chart.png">', "filename_alt_text"),
    ('<img src="chart.png" alt="image of a chart">', "redundant_alt_prefix"),
    ('<img src="chart.png" alt="abc">', "short_alt_text"),
    ('<img src="chart.png" alt="' + "a" * 101 + '">', "long_alt_text"),
    ('<a href="handout.pdf">Handout</a>', "doc_link_no_type"),
    ('<iframe src="https://youtube.com/embed/synthetic"></iframe>', "video_caption_check"),
    ('<p style="text-decoration:underline">Text</p>', "underline_not_link"),
    ('<p style="text-decoration: underline dotted">Text</p>', "underline_not_link"),
    ("<p>if a<b, that's true.</p><img src=\"chart.png\">", "missing_alt_text"),
    ('<p style="font-size:8px">Text</p>', "small_font_size"),
    ("<p>• Manual bullet</p>", "manual_bullets"),
    ('<p title="Use x<y here">• Manual bullet</p>', "manual_bullets"),
    ('<span style="color:red">Important</span>', "color_only_meaning"),
])
def test_scan_still_identifies_documented_defect(markup, issue):
    issues = _check_content_accessibility(markup, "page", 7, "Synthetic")
    found = next((item for item in issues if item["type"] == issue), None)
    assert found is not None, issues
    assert found["content_type"] == "page"
    assert found["content_id"] == 7
    assert found["content_title"] == "Synthetic"
    assert isinstance(found["auto_fixable"], bool)


@pytest.mark.parametrize("style", [
    "background-color:#ff5f05;color:white",
    "color:#fff;background-color:#ff5f05",
])
def test_contrast_fixer_changes_only_the_foreground(style):
    fixed, count = _fix_orange_contrast(f'<p style="{style}">Synthetic</p>')
    assert count == 1
    assert "background-color:#ff5f05" in fixed
    assert "color: #000000" in fixed
    assert ">Synthetic</p>" in fixed


@pytest.mark.parametrize("markup", [
    '<span style="color:inherit">Important</span>',
    '<span style="color:red;font-weight:bold">Important</span>',
    '<span style="color:red"><strong>Important</strong></span>',
    '<span style="background-color:red">Important</span>',
])
def test_color_rule_does_not_flag_redundant_or_non_foreground_styles(markup):
    assert "color_only_meaning" not in {
        item["type"] for item in _check_content_accessibility(markup, "page", 7, "Synthetic")
    }


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize("markup,issue", [
    ('<img src="chart.png">', "missing_alt_text"),
    ('<h2> </h2>', "empty_heading"),
    ('<table><td>Value</td></table>', "table_without_headers"),
    ('<a href="/detail">click here</a>', "non_descriptive_link"),
    ('<a href="/detail"> </a>', "empty_link"),
    ('<a href="/detail">https://example.org</a>', "url_as_link_text"),
    ('<th>Header</th>', "th_missing_scope"),
    ('<h2>First</h2><h4>Skipped</h4>', "heading_skip"),
    ('<img alt="chart.png">', "filename_alt_text"),
    ('<img alt="image of a chart">', "redundant_alt_prefix"),
    ('<a href="handout.pdf">Handout</a>', "doc_link_no_type"),
    ('<iframe src="https://youtube.com/embed/synthetic"></iframe>', "video_caption_check"),
    ('<p style="text-decoration:underline">Text</p>', "underline_not_link"),
    ('<span style="color:red">Important</span>', "color_only_meaning"),
])
def test_scanner_accepts_quoted_angle_brackets(markup, issue, quote):
    markup = re.sub(r"^(<[a-z][a-z0-9]*)", lambda match: f"{match[0]} title={quote}x<y>z{quote}", markup)
    found = {item["type"] for item in _check_content_accessibility(markup, "page", 7, "Synthetic")}
    assert issue in found


@pytest.mark.parametrize("quote", ['"', "'"])
def test_scope_fixer_preserves_row_scope_and_changes_only_missing_attribute(quote):
    row = f'<TH title={quote}x<y>z{quote} scope="row">Row</TH>'
    assert _fix_th_scope(row) == (row, 0)
    missing = f'<TH title={quote}x<y>z{quote}>Column</TH>'
    fixed, count = _fix_th_scope(missing)
    assert count == 1
    assert fixed == missing.replace("<TH", '<TH scope="col"', 1)


def test_attributes_inside_title_do_not_satisfy_alt_scope_or_style():
    markup = '<img title=\'alt="Chart"\'><th title=\'scope="row"\'>Header</th>'
    markup += '<p title=\'style="color:white;background-color:#ff5f05"\'>Text</p>'
    found = {item["type"] for item in _check_content_accessibility(markup, "page", 7, "Synthetic")}
    assert {"missing_alt_text", "th_missing_scope"} <= found
    assert "low_contrast" not in found
    fixed, count = _fix_orange_contrast(markup)
    assert (fixed, count) == (markup, 0)


def test_contrast_scanner_and_fixer_honor_important_priority():
    markup = '<p style="background-color:#ff5f05;color:white !important;color:black">Text</p>'
    assert "low_contrast" in {
        item["type"] for item in _check_content_accessibility(markup, "page", 7, "Synthetic")
    }
    fixed, count = _fix_orange_contrast(markup)
    assert count == 1
    assert "color: #000000 !important" in fixed
    assert "color:black" in fixed
    assert "background-color:#ff5f05" in fixed


@pytest.mark.parametrize("quote", ['"', "'"])
def test_redundant_alt_fixer_handles_the_quotes_advertised_by_scanner(quote):
    markup = f'<img title="x<y>z" alt={quote}image of a chart{quote}>'
    assert "redundant_alt_prefix" in {
        item["type"] for item in _check_content_accessibility(markup, "page", 7, "Synthetic")
    }
    fixed, count = _fix_redundant_alt_prefix(markup)
    assert (fixed, count) == (markup.replace("image of ", "", 1), 1)


@pytest.mark.parametrize(("markup", "expected", "count"), [
    ('<div id="kl_banner">x</div>', '<div id="kl_banner" class="dp-header">x</div>', 1),
    ('<div class="a" id="kl_banner">x</div>', '<div class="a dp-header" id="kl_banner">x</div>', 1),
    ('<div id="kl_custom_block_3" class="b">x</div>',
     '<div id="kl_custom_block_3" class="b dp-content-block">x</div>', 1),
    ('<div id="kl_banner" class="dp-header">x</div>', '<div id="kl_banner" class="dp-header">x</div>', 0),
    ('<div id="other">x</div>', '<div id="other">x</div>', 0),
    ('<div title="x<y" id="kl_banner">x</div>', '<div title="x<y" id="kl_banner" class="dp-header">x</div>', 1),
    ('<div class="kl_wrapper" id="kl_readings2">x</div>',
     '<div class="dp-wrapper dp-content-block" id="kl_readings2">x</div>', 2),
])
def test_designplus_fix_adds_class_for_legacy_ids(markup, expected, count):
    assert _fix_legacy_designplus(markup) == (expected, count)


@pytest.mark.parametrize("wrapper", ["<!--{}-->", "<script>{}</script>", "<style>{}</style>"])
def test_scanner_and_fixers_ignore_hidden_fake_markup(wrapper):
    fake = '<th><img alt="image of cat"><div class="kl_wrapper" id="kl_banner" style="background-color:#ff5f05;color:white">'
    markup = wrapper.format(fake)
    assert _check_content_accessibility(markup, "page", 7, "Synthetic") == []
    for fixer in (_fix_th_scope, _fix_orange_contrast, _fix_legacy_designplus, _fix_redundant_alt_prefix):
        assert fixer(markup) == (markup, 0)


def test_real_attribute_names_are_required():
    markup = '<img data-alt="image of cat"><th data-scope="row">Header</th>'
    markup += '<div data-class="kl_wrapper" data-id="kl_banner" data-style="font-size:8px">kl_wrapper</div>'
    found = {issue["type"] for issue in _check_content_accessibility(markup, "page", 7, "Synthetic")}
    assert found == {"missing_alt_text", "th_missing_scope"}
    assert _fix_th_scope(markup) == (markup.replace('<th ', '<th scope="col" ', 1), 1)
    for fixer in (_fix_orange_contrast, _fix_legacy_designplus, _fix_redundant_alt_prefix):
        assert fixer(markup) == (markup, 0)


def test_stray_quote_does_not_promote_a_bogus_attribute_name():
    markup = '<img "alt"="cat"><th "scope"="row">Heading</th>'
    found = {issue["type"] for issue in _check_content_accessibility(markup, "page", 7, "Synthetic")}
    assert {"missing_alt_text", "th_missing_scope"} <= found


@pytest.mark.parametrize("name", ["script", "style"])
@pytest.mark.parametrize("fake_end", ["ſcript", "ſtyle", "scrıpt", "styKe"])
def test_raw_text_ends_only_at_ascii_closing_tag(name, fake_end):
    markup = f'<{name}>raw</{fake_end}><img></{name}><th>visible</th>'
    assert [tag.name for tag in iter_html_tags(markup)] == [name, name, "th", "th"]
    assert strip_html_tags(markup) == "visible"


@pytest.mark.parametrize("digits,expected", [("9" * 10_000, "\ufffd"), ("0" * 10_000 + "65", "A")])
def test_giant_decimal_entities_decode_without_integer_limit(digits, expected):
    assert unescape_html(f'&#{digits};') == expected
    assert strip_html_tags(f'<p>&#{digits};</p>') == expected
    violations = _extract_violations_from_html(f'WCAG 1.1.1\npage &#{digits}; missing alt text')
    assert expected in violations[0]["location"]


@pytest.mark.parametrize("style", [
    "background-color:#ff5f05;--color:white;color:black",
    "background-color:#ff5f05;border-color:white;color:black",
    "background-color:#ff5f05;color:whitewash",
    "--background-color:#ff5f05;color:white",
])
def test_contrast_requires_exact_css_properties_and_values(style):
    markup = f'<p style="{style}">Text</p>'
    assert _fix_orange_contrast(markup) == (markup, 0)
    assert "low_contrast" not in {issue["type"] for issue in _check_content_accessibility(markup, "page", 7, "Synthetic")}


def test_contrast_does_not_modify_custom_or_border_properties():
    markup = '<p style="background-color:#ff5f05;--color:white;border-color:white;color:white">Text</p>'
    expected = '<p style="background-color:#ff5f05;--color:white;border-color:white;color: #000000">Text</p>'
    assert _fix_orange_contrast(markup) == (expected, 1)


@pytest.mark.parametrize("quote", ['"', "'", ""])
def test_designplus_migrates_exact_class_tokens_and_numeric_ids(quote):
    markup = f'<div CLASS={quote}kl_wrapper{quote} ID="kl_custom_block_123">kl_wrapper</div>'
    fixed, count = _fix_legacy_designplus(markup)
    assert count == 2
    assert 'dp-wrapper dp-content-block' in fixed
    assert '>kl_wrapper</div>' in fixed
    assert _fix_legacy_designplus(fixed) == (fixed, 0)
    assert "UNTRUSTED" not in fixed


def test_designplus_does_not_replace_class_substrings_or_text():
    markup = '<div class="prefix-kl_wrapper kl_wrapper-extra" title="class=&quot;kl_wrapper&quot;">kl_wrapper</div>'
    assert _fix_legacy_designplus(markup) == (markup, 0)


@pytest.mark.parametrize("fixer,markup", [
    (_fix_th_scope, '<th title="x > y">Heading</th>'),
    (_fix_orange_contrast, '<p style="background-color:#ff5f05;color:white">Text</p>'),
    (_fix_redundant_alt_prefix, '<img alt="image of a chart">'),
    (_fix_legacy_designplus, '<div class="kl_wrapper" id="kl_banner">Text</div>'),
])
def test_all_fixers_are_idempotent_and_return_raw_html(fixer, markup):
    fixed, count = fixer(markup)
    assert count > 0
    assert fixer(fixed) == (fixed, 0)
    assert "UNTRUSTED CANVAS CONTENT" not in fixed


def test_report_parser_ignores_comments_and_raw_text():
    markup = '<!--WCAG 1.1.1 missing alt text--><script>WCAG 2.4.4 link text</script><style>WCAG 1.4.3 color contrast</style>'
    assert _extract_violations_from_html(markup) == []


@pytest.mark.parametrize("style", [
    "font-family:'A;background-color:#ff5f05;color:white;B'",
    "font-family:'A;font-size:8px;text-decoration:underline;B'",
    "--example:url(data:text/plain;background-color:#ff5f05;color:white)",
    "/* background-color:#ff5f05;color:white; */font-family:serif",
    "background-color:#ff5f05;color:white;font-family:'unfinished",
])
def test_css_strings_comments_and_functions_are_not_declarations(style):
    markup = f'<p style="{style}">Text</p>'
    found = {item["type"] for item in _check_content_accessibility(markup, "page", 7, "Synthetic")}
    assert not found & {"low_contrast", "small_font_size", "underline_not_link"}
    assert _fix_orange_contrast(markup) == (markup, 0)


def test_contrast_fixer_preserves_fake_declarations_in_css_strings():
    style = "font-family:'A;color:white;B';background-color:#ff5f05;color:white"
    markup = f'<p style="{style}">Text</p>'
    fixed, count = _fix_orange_contrast(markup)
    assert count == 1
    assert fixed == markup.replace(';color:white">', ';color: #000000">')
    assert _fix_orange_contrast(fixed) == (fixed, 0)
