"""Repo-wide template guards for mistakes that render as user-visible junk."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).parent.parent / "src" / "maier" / "core" / "templates"


def test_no_multiline_hash_comments():
    """Django renders a `{#` comment lacking its `#}` on the SAME line as
    literal page text (it is a single-line comment syntax). This has shipped
    visible junk three times (scan banner, review, settings -- 2026-08-24);
    multi-line comments must use `{% comment %}` blocks.
    """
    offenders = []
    for tpl in TEMPLATES.rglob("*.html"):
        for lineno, line in enumerate(tpl.read_text().splitlines(), 1):
            for m in re.finditer(r"\{#", line):
                if "#}" not in line[m.start() :]:
                    offenders.append(f"{tpl.name}:{lineno}: {line.strip()[:60]}")
    assert offenders == [], "multi-line {# #} comments render as page text:\n" + "\n".join(
        offenders
    )
