#!/usr/bin/env bash
# Render the markdown guides into styled HTML pages that live on the GitHub
# Pages site (so they don't bounce to github.com). Re-run after editing any
# docs/*.md guide.  Requires: pandoc.
#
#   cd docs && ./build_docs.sh
set -euo pipefail
cd "$(dirname "$0")"

REPO="https://github.com/Minyoung1005/research-bot"
# slug|Title  (order = nav order)
GUIDES=(
  "setup|Setup"
  "usage|Usage"
  "multi-machine|Multi-machine"
  "architecture|Architecture"
  "troubleshooting|Troubleshooting"
)

nav_links () { # $1 = active slug
  local active="$1" out="" slug title
  for g in "${GUIDES[@]}"; do
    slug="${g%%|*}"; title="${g##*|}"
    if [ "$slug" = "$active" ]; then
      out+="<a class=\"active\" href=\"${slug}.html\">${title}</a>"
    else
      out+="<a class=\"hideable\" href=\"${slug}.html\">${title}</a>"
    fi
  done
  printf '%s' "$out"
}

rewrite_links () { # rewrite intra-repo .md links to on-site .html (keep #anchors)
  sed -E \
    -e 's#href="\.\./README\.md#href="'"$REPO"'/blob/main/README.md#g' \
    -e 's#href="README\.md#href="index.html#g' \
    -e 's#href="setup\.md#href="setup.html#g' \
    -e 's#href="usage\.md#href="usage.html#g' \
    -e 's#href="multi-machine\.md#href="multi-machine.html#g' \
    -e 's#href="architecture\.md#href="architecture.html#g' \
    -e 's#href="troubleshooting\.md#href="troubleshooting.html#g'
}

for g in "${GUIDES[@]}"; do
  slug="${g%%|*}"; title="${g##*|}"
  echo "building ${slug}.html"
  body="$(pandoc --from gfm --to html5 --no-highlight "${slug}.md" | rewrite_links)"
  cat > "${slug}.html" <<HTML
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>${title} — research-bot docs</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%94%AC%3C/text%3E%3C/svg%3E" />
<link rel="stylesheet" href="assets/docs.css" />
</head>
<body>
<header class="nav">
  <div class="nav-inner">
    <a class="brand" href="index.html"><span class="logo">🔬</span> research-bot</a>
    <nav class="nav-links">$(nav_links "$slug")<a class="nav-cta" href="${REPO}">GitHub ★</a></nav>
  </div>
</header>
<main class="doc">
  <div class="breadcrumb"><a href="index.html">Docs</a> / ${title}</div>
${body}
</main>
<footer class="doc-footer">
  MIT License · © 2026 Minyoung Hwang ·
  <a href="${REPO}">GitHub</a> ·
  <a href="${REPO}/issues">Issues</a> ·
  <a href="index.html">Docs home</a>
</footer>
</body>
</html>
HTML
done
echo "done: $(printf '%s.html ' "${GUIDES[@]%%|*}")"
