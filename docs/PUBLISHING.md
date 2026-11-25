# Publish benchmark results

Dashboard: https://anyip-bench.pages.dev/. Source: https://github.com/devsanthoshmk/anyip-maps-benchmark.

## Add a run

1. Review the completed local report. Never add `results/`, `.env`, raw SQLite files, request URLs, or exit IPs to Git.
2. From the repo root, run `python3 docs/public-runs/publish.py results/<run-id>/analysis.json "Short display title" --number 2`. Choose a unique positive number. The dashboard lists runs from `docs/public-runs/index.json`, not arbitrary files in the repo. Only include `--account-usage-delta-bytes` if the before and after account readings are available and reviewed.
3. Review the changes in `docs/public-runs/`, commit, and push to `main`. The dashboard reads the index and selected run directly from GitHub in the browser. Refresh the page after GitHub's CDN cache updates. No Pages deploy is needed for data changes.

## Deploy UI changes

Run `npm ci` and `npm run build` in `ui/`, then from that directory run `wrangler pages deploy out --project-name anyip-bench --branch main` using the global Wrangler installation. Only deploy the static build output, never the repo root. Check the live page on a desktop and mobile viewport and confirm it fetches the GitHub index and displays the expected run.
