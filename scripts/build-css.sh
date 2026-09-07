#!/usr/bin/env bash
# Rebuild the precompiled Tailwind stylesheet after changing template classes.
set -euo pipefail
cd "$(dirname "$0")/.."
npx tailwindcss@3.4.17 -c tailwind.config.js \
  -i tailwind.input.css \
  -o pharmacypulse/static/pharmacypulse/tailwind.css --minify
