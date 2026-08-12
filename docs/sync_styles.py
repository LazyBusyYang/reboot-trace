#!/usr/bin/env python3
"""Embed docs/style.css into every HTML document, or verify they are in sync."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


DOCS_DIR = Path(__file__).resolve().parent
STYLE_PATTERN = re.compile(r"<style>\n?.*?</style>", re.DOTALL)


def expected_style(css: str) -> str:
    return f"<style>\n{css.rstrip()}\n</style>"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="only verify embedded styles")
    args = parser.parse_args()

    css = (DOCS_DIR / "style.css").read_text(encoding="utf-8")
    replacement = expected_style(css)
    stale: list[Path] = []

    for path in sorted(DOCS_DIR.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        updated, count = STYLE_PATTERN.subn(lambda _: replacement, source, count=1)
        if count != 1:
            raise SystemExit(f"{path.name}: expected exactly one <style> block")
        if updated != source:
            stale.append(path)
            if not args.check:
                path.write_text(updated, encoding="utf-8")

    if stale and args.check:
        print("stale embedded styles:", ", ".join(path.name for path in stale))
        return 1
    if stale:
        print("updated:", ", ".join(path.name for path in stale))
    else:
        print("all embedded styles are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
