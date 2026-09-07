/** Precompiled Tailwind build. Replaces the 407KB render-blocking Play CDN
 *  (cdn.tailwindcss.com) which compiled CSS in the browser on every page load.
 *  Scans templates + static JS for utility classes; stock defaults (the site
 *  used no custom Play-CDN config). Rebuild: see scripts/build-css.sh */
module.exports = {
  content: [
    "./pharmacypulse/templates/**/*.html",
    "./pharmacypulse/static/**/*.js",
  ],
  theme: { extend: {} },
  plugins: [],
}
