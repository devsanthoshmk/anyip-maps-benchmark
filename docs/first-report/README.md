# AnyIP residential trial: first results

21 November 2026. Run `residential_first_20261121_192713`.

The residential setup completed 85 of 100 Google Maps search executions in 9 minutes 18 seconds. The exit IP reported by our witnesses matched at both ends of 96 executions. It changed in one; three had no comparable endpoint checks. This is enough for a first report to AnyIP, not a production decision.

| Measurement | India | United States |
| --- | ---: | ---: |
| Searches that returned places without a terminal block or error | 44 / 50 | 41 / 50 |
| Place records, including repeats | 1,758 | 1,731 |
| Mean latency per OK HTTP response | 1.83 s | 1.50 s |
| Consent / HTTP 403 / HTTP 429 | 0 / 0 / 0 | 0 / 0 / 0 |
| Suspected blocks without HTTP 403 or 429 | 0 | 5 |
| Network-error attempts, before retry resolution | 5 | 0 |
| Exit IP checked at both ends: matched / changed / unknown | 47 / 1 / 2 | 49 / 0 / 1 |
| Reused sticky ID: same observed IP across both searches | 9 / 10 | 6 / 10 |

Nine searches returned no places, five US searches returned partial results after a suspected block, and one India search ended in a network error. There were no recorded proxy errors. Of 3,489 place records, 3,483 passed the scraper's field checks; those checks do not establish that every listing was accurate. AnyIP's account meter increased by 22.07 MB between snapshots, within the 5 GB trial allowance. The client's HTTP wire estimate was 17.95 MiB; it is not an invoice. Production pricing was not quoted.

## What we ran

We selected ten queries per country from the [query panel](../../queries/panel_all.csv). We ran each query five times: twice on a reused sticky-session ID and three times on separate fresh IDs. Each execution requested the initial search and up to two result pages on one sticky ID. The requested session duration was 20 minutes; the reused-ID repeats had at least 60 seconds between them. Up to five independent queries could be active, with a shared cap of two Google HTTP attempts per second. No direct traffic, enrichment, or mobile traffic was included in this run.

Among eligible repeat pairs, the mean top-20 Jaccard overlap was **0.839 on the same observed IP** (13 pairs) and **0.641 on different observed IPs** (30 pairs). Jaccard overlap divides shared places by the number of distinct places in both top-20 sets; it does not measure order. The comparison excludes pairs without comparable witness evidence or without places in either execution. Some pairs share an execution. This describes the sample and does not isolate the IP as the cause. Witnesses only report the address they saw; they cannot prove what Google saw or rule out changes between checks.

## Check the numbers

```sh
python3 docs/first-report/verify.py
```

The verifier reads [evidence.json](evidence.json) and recomputes the headline counts, latency, IP comparisons, and top-20 overlap. The export replaces IPs, sticky IDs, query text, and place identifiers with run-local labels that preserve equality. It omits raw responses, request URLs, credentials, and account details. Query classifications and endpoint selection come from the analyzer; this export cannot replay the original responses or independently verify the provider's meter. The [exporter](export_evidence.py) documents the transformation, and the [run configuration](../../experiments/residential_first.toml) records the original settings. See the [repository README](../../README.md) for offline reproduction.

## What remains open

India had no consent pages, so the trigger AnyIP suggested for a mobile follow-up did not occur. The five US partial searches merit a closer look before production. We did not compare the result with a matched baseline, measure capacity at high concurrency, establish sustained session lifetime or block onset, or test enrichment. This run cannot say whether AnyIP improves on the previous setup or what a production query would cost.
