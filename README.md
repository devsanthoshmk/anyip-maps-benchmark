# gmap-bench

`gmap-bench` measures how AnyIP proxy sessions behave during Google Maps queries. It records request outcomes, latency, and the exit IPs seen by network probes. The analysis keeps partial results, blocks, and network errors separate.

Start with the [first report](docs/first-report/README.md) for the results and their limits. The [methodology](docs/METHODOLOGY.md) explains how the benchmark counts outcomes and calculates its statistics. An IP probe reports the address seen by that probe, not necessarily the address seen by Google Maps.

## Verify offline

To check the published results, run `python3 docs/first-report/verify.py`. The verifier needs no dependencies or credentials.

Use Python 3.11 or later and [uv](https://docs.astral.sh/uv/). Install the dependencies once with `uv sync --locked`. This setup step may need internet access.

From the project root, run:

```sh
uv run --offline --no-sync python -m pytest
uv run --offline --no-sync python experiment.py experiments/residential_first.toml --dry-run
output_dir=$(mktemp -d)
uv run --offline --no-sync python offline_report.py "$output_dir/report"
```

The dry run prints the experiment plan without making requests. The last command makes a report from fictional records. Open `report.md` in the generated `report` directory. None of these checks needs AnyIP credentials. Running an experiment without `--dry-run` can make network requests even if the configuration uses no proxy.

## Reproduce the run

If you have an AnyIP account and authorization to run the workload, copy `.env.example` to `.env` and fill in your own account and proxy settings. From the project root, run `uv run --no-sync python experiment.py experiments/residential_first.toml --run-id residential_reproduction`. Use a new run ID each time. This sends live traffic and consumes quota. The configuration repeats the workload, not the original exit IPs or search results.

To view the styled first report locally, open `docs/first-report/index.html` in a browser. It does not need a server or a login.

## Keep raw runs private

Store credentials in a local `.env` file. Do not publish it or the `results/` directory. A live run's database and exports can contain request URLs, session identifiers, IP addresses, and place data. Share reviewed aggregates and figures separately from the raw run files.
