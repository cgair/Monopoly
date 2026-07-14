#!/usr/bin/env python3
"""Render the walkthrough markdown to a styled HTML, then print to PDF via Chrome headless."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import markdown

HERE = Path(__file__).parent
SRC = HERE / "monopoly-walkthrough.md"
HTML = HERE / "monopoly-walkthrough.html"
PDF = HERE / "monopoly-walkthrough.pdf"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

CSS = """
@page { size: A4; margin: 18mm 15mm 18mm 15mm; }
body {
  font-family: -apple-system, "PingFang SC", "Hiragino Sans GB",
               "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 11pt;
  line-height: 1.55;
  color: #222;
  max-width: 100%;
  margin: 0;
  padding: 0;
}
h1 { font-size: 22pt; border-bottom: 2px solid #333; padding-bottom: 6px; margin-top: 20px; }
h2 { font-size: 16pt; border-bottom: 1px solid #999; padding-bottom: 4px; margin-top: 24px; }
h3 { font-size: 13pt; margin-top: 18px; }
h4 { font-size: 11.5pt; margin-top: 12px; }
p, li { font-size: 10.5pt; }
code, kbd, samp, pre {
  font-family: "SF Mono", "Menlo", "Consolas", "Courier New", monospace;
  font-size: 9.5pt;
}
code {
  background: #f4f4f4;
  padding: 1px 5px;
  border-radius: 3px;
  border: 1px solid #e0e0e0;
  color: #c0392b;
}
pre {
  background: #f8f8f8;
  border: 1px solid #ddd;
  border-left: 3px solid #888;
  border-radius: 4px;
  padding: 10px 12px;
  overflow-x: auto;
  page-break-inside: avoid;
}
pre code {
  background: transparent;
  border: none;
  padding: 0;
  color: #222;
}
blockquote {
  border-left: 3px solid #888;
  margin: 8px 0;
  padding: 4px 12px;
  color: #555;
  background: #fafafa;
}
table {
  border-collapse: collapse;
  margin: 10px 0;
  font-size: 10pt;
  page-break-inside: avoid;
}
th, td {
  border: 1px solid #ccc;
  padding: 5px 9px;
  text-align: left;
  vertical-align: top;
}
th { background: #f0f0f0; font-weight: 600; }
img {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 12px auto;
  page-break-inside: avoid;
}
a { color: #1565c0; text-decoration: none; }
a:hover { text-decoration: underline; }
hr { border: none; border-top: 1px solid #ccc; margin: 18px 0; }
ul, ol { padding-left: 22px; }
li { margin: 2px 0; }
:not(pre) > code { white-space: nowrap; }
h2, h3, h4 { page-break-after: avoid; }
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Monopoly Trading Agent 代码走读</title>
  <style>{css}</style>
</head>
<body>
{body}
</body>
</html>
"""


def main() -> int:
    if not SRC.exists():
        print(f"missing source: {SRC}", file=sys.stderr)
        return 1

    md_text = SRC.read_text(encoding="utf-8")

    extensions = [
        "fenced_code",
        "tables",
        "toc",
        "sane_lists",
        "codehilite",
    ]
    extension_configs = {
        "codehilite": {"guess_lang": False, "use_pygments": True, "noclasses": True},
        "toc": {"permalink": False},
    }
    body = markdown.markdown(
        md_text, extensions=extensions, extension_configs=extension_configs
    )

    HTML.write_text(HTML_TEMPLATE.format(css=CSS, body=body), encoding="utf-8")
    print(f"wrote HTML: {HTML}")

    if not Path(CHROME).exists():
        print(f"chrome not found at {CHROME}", file=sys.stderr)
        return 2

    cmd = [
        CHROME,
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={PDF}",
        f"file://{HTML.absolute()}",
    ]
    print("running:", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("chrome stdout:", result.stdout, file=sys.stderr)
        print("chrome stderr:", result.stderr, file=sys.stderr)
        return 3

    if not PDF.exists():
        print("chrome ran but no PDF produced", file=sys.stderr)
        return 4

    size_kb = PDF.stat().st_size / 1024
    print(f"wrote PDF: {PDF} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
