"""Generate the OpenAPI schema and a standalone HTML reference.

    docker compose exec web python scripts/build_api_docs.py

Produces two files in docs/api/:

* ``schema.yml``  -- the machine-readable contract. Committed, so an API change
  shows up in a diff rather than in a message someone missed.
* ``index.html``  -- a single self-contained file. The frontend team opens it in
  any browser; no server, no install, no repo access needed.

Regenerate after any change to serializers, views or routes. A stale schema is
worse than none, because people trust it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "api"
SCHEMA_PATH = OUT_DIR / "schema.yml"
HTML_PATH = OUT_DIR / "index.html"

# Pinned rather than "latest": the HTML is emailed around and may be opened
# months from now. It should not change under the reader.
REDOC_CDN = "https://cdn.redoc.ly/redoc/v2.1.5/bundles/redoc.standalone.js"


def generate_schema() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [sys.executable, "manage.py", "spectacular", "--file", str(SCHEMA_PATH)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit("Schema generation failed.")
    if result.stderr.strip():
        # Warnings mean the generated client will be subtly wrong. Surface them.
        sys.stderr.write("Schema warnings:\n" + result.stderr)


def build_html() -> None:
    """Embed the schema in a single HTML file.

    The spec is inlined as JSON rather than fetched, so the file works from a
    local disk with no web server -- opening it over file:// would otherwise
    hit CORS.
    """
    import yaml  # provided by drf-spectacular

    spec = yaml.safe_load(SCHEMA_PATH.read_text(encoding="utf-8"))
    spec_json = json.dumps(spec)

    HTML_PATH.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>BizEdge Grievances API</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .banner {{
      background: #0f2b33; color: #eaf4f6; padding: 14px 22px;
      font-size: 14px; line-height: 1.5;
    }}
    .banner strong {{ color: #7fd4e0; }}
    .banner code {{
      background: rgba(255,255,255,.12); padding: 1px 5px; border-radius: 3px;
    }}
  </style>
</head>
<body>
  <div class="banner">
    <strong>BizEdge Grievances API</strong> &mdash; generated reference.
    Read <code>API-NOTES.md</code> alongside this: it covers the behaviour the
    schema cannot express, including when <code>complainant</code> is null and
    why HR does not see every complaint.
  </div>
  <div id="redoc"></div>
  <script src="{REDOC_CDN}"></script>
  <script>
    Redoc.init({spec_json}, {{
      scrollYOffset: 52,
      hideDownloadButton: false,
      theme: {{ colors: {{ primary: {{ main: '#0f7b8a' }} }} }}
    }}, document.getElementById('redoc'));
  </script>
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> None:
    generate_schema()
    build_html()
    print(f"schema -> {SCHEMA_PATH.relative_to(ROOT)}")
    print(f"docs   -> {HTML_PATH.relative_to(ROOT)}")
    print("\nSend docs/api/index.html to the frontend team. It opens in any browser.")


if __name__ == "__main__":
    main()
