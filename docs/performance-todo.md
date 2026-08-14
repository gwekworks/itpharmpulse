# Performance TODO

Tracked client-side perf wins we know about but have not yet implemented.

## Homepage / sitewide perceived load time

**Status:** identified 2026-05-03, not yet applied.

**Server is not the bottleneck.** TTFB on `/home` measured ~240ms; the two count() queries on the home view take ~43ms combined. The remaining wait is all client-side asset cost.

### Hot spots, biggest first

1. **Tailwind CDN runtime JIT** — `<script src="https://cdn.tailwindcss.com"></script>` in `templates/pharmacypulse/layouts/blank.html`. This script downloads, parses, scans the DOM for utility classes, and generates CSS on the fly every page load. The browser console explicitly warns: *"cdn.tailwindcss.com should not be used in production."* Estimated cost 200-800ms depending on device.

2. **Google Fonts, 5 weights across 2 families** — `Plus Jakarta Sans` at 400/500/600/700/800 plus `DM Serif Display`. Each weight is a separate font file. Audit usage; we likely only need 400 + 600 + 800 (or even 400 + 700).

3. **Translation swap on ES** — `swapDOM` in `partials/topbar.html` walks every text node + every element with `placeholder/title/aria-label/value` and runs both an exact-string dict swap and a regex pattern pass. Fine on small pages, can stutter on long lists. Acceptable for now; revisit if it ever shows up in profiling.

### Recommended fix order

1. **Replace Tailwind CDN with precompiled CSS.** Three options:
   - **Lowest-effort, biggest immediate win:** swap to a prebuilt static stylesheet, e.g. `https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css` (~30KB gzipped, browser-cached, zero runtime cost). Ships some unused CSS but kills the JIT warning and the compilation step.
   - **Best long-term:** add Tailwind CLI to the build (`npx tailwindcss -i input.css -o pharmacypulse/static/pharmacypulse/tailwind.css --minify`) configured against the templates/ glob. Smallest output, fully tree-shaken. Requires adding npm to the toolchain.
   - **Smallest payload, most manual:** hand-roll the ~50 utility classes actually used into `theme.css` and drop Tailwind entirely. Inventory via:
     ```bash
     grep -rho 'class="[^"]*"' pharmacypulse/templates/ | tr ' ' '\n' | sort -u
     ```

2. **Trim font weights.** Edit the Google Fonts URL in `templates/pharmacypulse/layouts/blank.html`:
   ```html
   <link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=Plus+Jakarta+Sans:wght@400;600;800&display=swap" rel="stylesheet">
   ```
   (Confirm by grepping templates for `font-weight:` and any tailwind `font-medium`/`font-semibold` to make sure 500/700 aren't relied on.)

3. **Optional: self-host the two fonts** under `/static/pharmacypulse/fonts/` to remove the Google Fonts round trip entirely. WhiteNoise will serve them with long cache headers. Lower priority than items 1-2.

### Don't bother (already fine)

- `Pharmacy.objects.filter(status='active').count()` and `Review.objects.filter(deleted_at__isnull=True).count()` in `page_data.page_home` — both clock under 50ms, no caching needed yet.
- The geo `resolve_loc(request)` chain — already short-circuits for authed users via `user.zip_code`.
- Map page Google Maps SDK — already loaded `async defer`.
- Google Maps JS preload in `partials/head.html` — already uses `loading=async`.

### How to verify after applying

1. Hard reload `/home` with DevTools Network tab open, "Disable cache" off.
2. Compare LCP and TTI in Lighthouse before / after.
3. Confirm the `cdn.tailwindcss.com should not be used in production` warning is gone from the console.
