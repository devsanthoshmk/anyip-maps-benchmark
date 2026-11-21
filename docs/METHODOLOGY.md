# Methodology: Benchmarking AnyIP Residential & Mobile Proxies Against Google Maps

**Document Version:** 1.0.0  
**Scope:** `gmap-bench` Evaluation Framework  
**Target Architecture:** Python 3.12, `aiohttp`, SQLite 3 (WAL), AnyIP Proxy Network, Google Maps Web Scraper  

---

## 1. Executive Summary & Research Objectives

Automated data collection from Google Maps presents unique proxy infrastructure challenges. Unlike static web pages, Google Maps employs a Single-Page Application (SPA) architecture combining initial HTML page generation, client-side JavaScript execution, dynamic cookie negotiation, and asynchronous pagination data streams (`/search?tbm=map...`). Maintaining scrape reliability requires sustained session continuity: if an exit IP changes between the initial SPA load and subsequent pagination calls, Google's edge heuristics frequently detect session state divergence and trigger anti-bot countermeasures (consent walls, automated query challenges, or HTTP 403/429 blocks).

This benchmark framework evaluates the performance, stability, and anti-bot resistance of **AnyIP** residential and mobile proxy pools against Google Maps web scraping. The primary objectives are:

1. **Quantify Session Retention & IP Drift (TTL)**: Measure how long AnyIP sticky sessions maintain a continuous egress IP address under benign probing without scrape traffic, detecting peer replacement events and verifying configured session lifetimes (`sesstime_min`).
2. **Characterize Google Maps Block Onset Hazard**: Model the survival distribution of sticky sessions under active scraping, determining the empirical hazard rate, median requests-to-first-block, and the distribution of block types.
3. **Measure Search Result & Ranking Variance**: Quantify how place results and SERP rankings diverge for identical queries when executed across the same exit IP, distinct exit IPs, continuously rotated IPs, and unproxied local baselines.
4. **Evaluate Concurrency & Load Sensitivity**: Determine how per-session and global request concurrency affects gateway latency, proxy error rates (502/504), and block onset acceleration.
5. **Compare Residential vs. Mobile vs. Local Pools**: Provide a rigorous head-to-head comparison of success rates, block incidence, carrier/ASN distributions, and bandwidth efficiency (cost per 1,000 places) across proxy pools.

---

## 2. Core Scientific Hypotheses

We formulate five formal, testable hypotheses:

### Hypothesis 1 (Session Retention & IP Stability)
* **Statement**: An AnyIP sticky session configured with duration $T$ maintains a constant egress IP address for $t < T$ under low-frequency probing, but exhibits non-zero stochastic peer replacement due to residential node churn.
* **Testable Prediction**: IP survival probability $\hat{S}_{\text{IP}}(t)$ will remain near $1.0$ for short intervals ($t \le 2\text{ min}$), but will decay as $t \to T$, with observed survival influenced by peer availability in the targeted country/region.
* **Falsification Condition**: Observing $\hat{S}_{\text{IP}}(t) < 0.50$ at $t = 0.2 \cdot T$ in the absence of network errors falsifies the provider's claim of session stickiness.

### Hypothesis 2 (Google Block Onset & Hazard Rate)
* **Statement**: Google Maps rate-limiting is hazard-driven over consecutive requests within an individual sticky session; survival time (requests to first block) decays non-linearly with cumulative request volume and request pacing.
* **Testable Prediction**: Consecutive pagination requests through a single sticky session will exhibit an increasing hazard rate $\lambda(r)$, where $r$ is the request index, resulting in a median requests-to-first-block $\tilde{R} < \infty$.
* **Falsification Condition**: If session block rate is independent of request count ($H_0: \lambda(r) = \text{const}$ for all $r \in [1, 200]$), Google does not maintain cumulative request counters per session/IP.

### Hypothesis 3 (SERP Rank Stability & Geo-Localization)
* **Statement**: Google Maps search result place sets and ranking order for an identical geographic query exhibit high stability when executed through the same exit IP, but diverge significantly when executed across distinct exit IPs or rotated connections.
* **Testable Prediction**: For identical query $q$, pair-wise Rank-Biased Overlap (RBO, $p=0.9$) and Jaccard similarity ($J@20$) will satisfy:
  $$\text{RBO}_{\text{same\_ip}} > \text{RBO}_{\text{new\_ip}} \ge \text{RBO}_{\text{forced\_rotate}}$$
* **Falsification Condition**: If $\text{RBO}_{\text{same\_ip}} \approx \text{RBO}_{\text{new\_ip}}$, Google Maps SERP generation is strictly query-driven and invariant to egress IP identity or client session history.

### Hypothesis 4 (Concurrency & Gateway Congestion)
* **Statement**: Increasing concurrency within a single sticky session accelerates Google block onset and increases proxy gateway errors (502 Bad Gateway / 504 Gateway Timeout) compared to distributed concurrency across independent sessions.
* **Testable Prediction**: Bursting $C$ parallel requests within one session will yield a higher block rate and higher proxy error rate than pacing $C$ requests sequentially across $C$ independent sessions.
* **Falsification Condition**: If block rate and 502/504 error rates remain identical across concurrency levels $C \in \{1, 2, 5, 10\}$, neither AnyIP gateways nor Google anti-bot filters penalize concurrent request bursts.

### Hypothesis 5 (Residential vs. Mobile Pool Discrepancy)
* **Statement**: Mobile proxy pools (routed through cellular carrier ASNs) demonstrate greater resilience against Google bot-detection (lower block and consent rates) than standard residential broadband pools for identical request volumes, offset by higher connection latency and carrier NAT churn.
* **Testable Prediction**: The block rate proportion for mobile proxies will be statistically significantly lower than for residential proxies ($\hat{p}_{\text{block, mobile}} < \hat{p}_{\text{block, res}}$), with non-overlapping 95% Wilson confidence intervals under high-load scraping.
* **Falsification Condition**: If $\hat{p}_{\text{block, mobile}} \ge \hat{p}_{\text{block, res}}$, cellular carrier IP reputation confers no anti-bot advantage for Google Maps scraping.

---

## 3. Unit of Analysis: The Sticky Session

### 3.1 Why Individual Requests or Queries Are Flawed Units of Analysis
In standard HTTP benchmarking, the atomic unit of analysis is frequently the individual request (URL, status code, latency). In web scraping of modern SPAs, treating the request as the unit of analysis introduces severe measurement bias:
1. **Multi-Step Scrape Workflow**: Scraping a single query requires an initial SPA bootstrap request (downloading HTML shell, scripts, session state) followed by 1 to $N$ asynchronous pagination calls (`/search?tbm=map...`).
2. **Censored Yields**: If page 1 succeeds (extracting 20 places) and page 2 encounters an HTTP 429 rate limit, reporting an aggregate 50% request success rate masks the fact that the scrape failed to achieve its target depth.
3. **Session State Binding**: Google issues session cookies and client tokens tied to the initial connection. Evaluating requests independently conceals the causal mechanism of blocks caused by IP drift mid-scrape.

### 3.2 Formal Definition of a Sticky Session
In this framework, the fundamental unit of analysis is the **Sticky Session** $\mathcal{S}$, formally defined as the tuple:
$$\mathcal{S} = \langle \text{id}, \text{run\_id}, \text{arm}, \text{variant}, \text{pool}, \text{country}, \tau_{\text{start}}, \tau_{\text{end}}, \mathcal{R}, \mathcal{P}, \mathcal{W}, \mathcal{E}, \text{status}, \text{stop\_reason} \rangle$$
Where:
* $\text{id} \in \Sigma^{1..32}$: The alphanumeric AnyIP session token (`[A-Za-z0-9]`).
* $\text{arm} \in \{\text{ttl}, \text{block\_onset}, \text{variance}, \text{load}, \text{pool\_compare}\}$: The experimental condition.
* $\text{variant}$: The sub-condition (e.g., `same_ip`, `new_ip`, `forced_rotate`, `local`, `load_4_2`).
* $\text{pool} \in \{\text{residential}, \text{mobile}, \text{mixed}, \text{local}\}$: The proxy pool.
* $\tau_{\text{start}}, \tau_{\text{end}} \in \mathbb{R}^+$: Monotonic session start and completion timestamps.
* $\mathcal{R} = [r_1, r_2, \dots, r_m]$: Ordered sequence of all request attempts, including retried gateway errors.
* $\mathcal{P} = \{p_1, p_2, \dots, p_k\}$: Set of validated places extracted, preserving result rank.
* $\mathcal{W} = [w_1, w_2, \dots, w_j]$: Time-series of IP witness observations recorded across the session lifetime.
* $\mathcal{E} = [e_1, e_2, \dots]$: Sequence of discrete lifecycle events (e.g., `block`, `ip_change`, `ttl_expiry`).
* $\text{status} \in \{\text{success}, \text{partial}, \text{blocked}, \text{empty}, \text{error}\}$: Aggregate session outcome.
* $\text{stop\_reason} \in \{\text{limit}, \text{no\_new}, \text{block}, \text{parse\_error}, \text{network}, \text{session\_lost}, \text{proxy\_error}, \text{other\_error}, \text{quota}, \text{stopped}\}$.

---

## 4. Experimental Arms

The framework evaluates proxy behavior across five specialized arms, configured declaratively via TOML manifests.

```
                    ┌──────────────────────────────────────────────┐
                    │               AnyIP Evaluation               │
                    └──────────────────────┬───────────────────────┘
                                           │
         ┌───────────────┬─────────────────┼────────────────┬───────────────┐
         ▼               ▼                 ▼                ▼               ▼
   ┌───────────┐  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  ┌──────────────┐
   │  Arm 1:   │  │    Arm 2:    │  │    Arm 3:    │  │  Arm 4:   │  │    Arm 5:    │
   │    TTL    │  │ Block Onset  │  │   Variance   │  │   Load    │  │ Pool Compare │
   └───────────┘  └──────────────┘  └──────────────┘  └───────────┘  └──────────────┘
   Pure IP drift  Google scrape     SERP stability    Concurrency    Head-to-head
   (no Google)    survival curve    (4 conditions)    scaling        res vs mob
```

### Arm 1: `ttl` (Session Retention & Pure IP Drift)
* **Objective**: Measure egress IP stability and survival over time without scraping traffic.
* **Protocol**:
  1. Open an AnyIP sticky session with configured `sesstime_min` (e.g., 5 to 30 min).
  2. Poll dual-stack IP witnesses (`ipify_v4`, `ipify_v6`) every `probe_interval_s` (e.g., 20s).
  3. Record timestamp, IPv4, IPv6, witness latency, and connection status.
  4. Detect first IP change: record discrete event `ttl_expiry` if change occurs at or after `sesstime_min`, or `ip_change` if peer drift occurs prematurely.
* **Target Sample Size**: Pilot: $n = 2$ sessions; Benchmark: $n = 30$ sessions per pool/country combination.

### Arm 2: `block_onset` (Google Rate-Limit & Block Survival)
* **Objective**: Measure how many consecutive requests/pages a sticky session can execute before Google issues an anti-bot challenge.
* **Protocol**:
  1. Open a sticky session bound to a single country panel.
  2. Cycle standardized panel queries sequentially through the session up to `requests_per_session` (e.g., 150 requests) or until first block.
  3. For each query, execute the initial SPA load and subsequent pagination calls up to `depth_pages`.
  4. On encountering any block outcome (`403`, `429`, `consent`, `sorry`, `soft_block`), record `block` event with exact request index, page number, and terminate the session.
* **Target Sample Size**: Pilot: $n = 2$ sessions; Benchmark: $n = 20$ sessions $\times 150$ requests per arm.

### Arm 3: `variance` (Search Result & SERP Stability)
* **Objective**: Quantify ranking stability, place membership overlap, and geo-localization effects across 4 controlled conditions for identical queries:
  1. `same_ip`: Query executed $N$ times through the same sticky session with an inter-query pause (e.g., 60s).
  2. `new_ip`: Query executed $N$ times across $N$ distinct fresh sticky sessions.
  3. `forced_rotate`: Query executed across $N$ fresh sessions with non-sticky connections (`sticky=False, force_close=True`), forcing TCP connection resets per request to break Google session continuity.
  4. `local`: Query executed directly from the benchmark host network without proxy.
* **Target Sample Size**: 10 distinct queries evaluated across the 4 conditions, each with $N = 3$ to $5$ repeats.

### Arm 4: `load` (Concurrency & Throughput Degradation)
* **Objective**: Measure performance degradation, gateway errors, and block onset acceleration under varied offered load.
* **Protocol**:
  1. Define load levels $\mathcal{L} = \{(\text{sessions}, \text{concurrency})\} = \{(1, 1), (2, 2), (4, 4), (8, 8)\}$.
  2. Concurrently drive scraping loops through configured sessions for a fixed duration $D$ (e.g., 5 min).
  3. Measure offered RPS vs. achieved RPS, latency percentiles (p50, p95), 502/504 gateway error rates, and block incidence.
* **Target Sample Size**: 4 load levels tested sequentially per pool.

### Arm 5: `pool_compare` (Head-to-Head Pool Comparison)
* **Objective**: Compare Residential, Mobile, Mixed, and Local pools on identical queries under standardized concurrency ($C=20$, Depth 2).
* **Protocol**:
  1. Run identical panel queries across all four pools.
  2. Measure query success rate, block rate, consent rate, proxy error rate, ASN diversity, and traffic per 1,000 places.

---

## 5. Statistical Framework & Metrics

To avoid the pitfalls of naive point estimates and small-sample distortions, all metrics are computed with formal statistical estimators.

### 5.1 Proportions and Confidence Intervals (Wilson Score Interval)
For any binomial proportion $p = k/n$ (e.g., query success rate, block rate, proxy error rate), we compute the **Wilson score interval** with 95% confidence ($z = 1.96$):

$$\hat{p}_{\text{Wilson}} = \frac{k + \frac{z^2}{2}}{n + z^2} \pm \frac{z}{n + z^2}\sqrt{\frac{k(n-k)}{n} + \frac{z^2}{4}}$$

* **Properties**: Asymmetric around small $k$, never produces bounds outside $[0, 1]$, and remains mathematically valid when $k = 0$ or $k = n$.

### 5.2 Zero-Event Bound (Rule of Three)
When an outcome is observed zero times ($k = 0$) across $n$ independent trials (e.g., 0 blocks observed across 30 sessions), point estimates stating a "0% block rate" are unscientific. We report the **Rule of Three** 95% upper bound:

$$\text{Upper Bound}_{95\%} = \frac{3}{n}$$

* **Interpretation**: If zero blocks occur across $n = 30$ sessions, we conclude with 95% confidence that the true underlying block rate is less than $3/30 = 10.0\%$.

### 5.3 Survival Analysis (Kaplan-Meier Estimator)
To model session longevity (requests-to-first-block or time-to-first-IP-change), sessions that terminate without experiencing an event (e.g., reaching the request budget, hitting the quota cap, or stopping at session limit) are treated as **right-censored**.

The empirical survival function $\hat{S}(t)$ is estimated via the **Kaplan-Meier product-limit estimator**:

$$\hat{S}(t) = \prod_{t_i \le t} \left(1 - \frac{d_i}{n_i}\right)$$

Where:
* $t_i$: Ordered distinct event times (request index or elapsed seconds).
* $d_i$: Number of events (blocks or IP changes) occurring at time $t_i$.
* $n_i$: Number of active sessions at risk just prior to $t_i$.

**Median Survival Time**:
$$\tilde{t} = \inf \{ t : \hat{S}(t) \le 0.50 \}$$
If $\hat{S}(t) > 0.50$ across all observed times, the median is undefined ($> t_{\text{max}}$), and we report 100% survival over the observed horizon.

### 5.4 Search Result Similarity & SERP Metrics
To compare place result sets $A$ and $B$ extracted for the same query under different conditions, we compute three complementary metrics:

#### 1. Jaccard Similarity Index at Depth $K$ ($J@K$)
Measures pure set membership agreement regardless of ordering:
$$J@K(A, B) = \frac{|A_{1:K} \cap B_{1:K}|}{|A_{1:K} \cup B_{1:K}|}$$
Evaluated at $K = 20$ (page 1) and $K = 40$ (page 2).

#### 2. Rank-Biased Overlap (RBO)
Standard Jaccard ignores ranking order, while Spearman/Kendall coefficients require identical disjoint item sets. **Rank-Biased Overlap** evaluates indefinite, non-conjoint ranked lists with a geometric top-weighted persistence parameter $p \in (0, 1)$:

$$\text{RBO}(A, B, p) = (1 - p) \sum_{d=1}^\infty p^{d-1} \cdot A_d$$

Where $A_d = \frac{|A_{1:d} \cap B_{1:d}|}{d}$ is the agreement proportion at depth $d$. We set $p = 0.90$, placing approximately 86% of the evaluation weight on the top 10 ranked places.

#### 3. Kendall’s Rank Correlation Coefficient ($\tau$)
For the intersection set $I = A \cap B$ of items present in both result lists, Kendall's $\tau$ measures relative ordering concordance:
$$\tau = \frac{C - D}{\frac{1}{2} |I| (|I| - 1)}$$
Where $C$ is the number of concordant pairs and $D$ is the number of discordant pairs.

### 5.5 Traffic Accounting & Bandwidth Metrics
AnyIP bills proxy usage based on total compressed bytes transferred over the proxy connection. We differentiate four distinct byte measurements:
1. **Wire Bytes ($B_{\text{wire}}$)**: Compressed payload received over the wire (`Content-Length` or raw compressed stream). **This is what AnyIP bills**.
2. **Decompressed Payload ($B_{\text{rx}}$)**: Uncompressed HTML/JSON text (used for scraping analysis).
3. **Estimated Upload ($B_{\text{tx}}$)**: Calculated request headers and query string transmitted through the proxy tunnel ($\approx 430\text{ bytes} + \text{len(URL)}$).
4. **Probe Bytes ($B_{\text{probe}}$)**: Bandwidth consumed by IP witness observation calls ($\approx 800\text{ bytes}$ per probe).

**Efficiency KPI**:
$$\text{Traffic per 1,000 Places} = \frac{B_{\text{wire, total}} + B_{\text{tx, total}}}{\text{Total Valid Places Extracted}} \times 1,000$$

---

## 6. Comprehensive Block & Error Taxonomy

A primary contribution of this methodology is the rigorous separation of **Google anti-bot blocks**, **AnyIP proxy gateway errors**, **server 5xx errors**, and **network transport failures**.

```
                           HTTP Response Received
                                     │
      ┌──────────────────────────────┼──────────────────────────────┐
      ▼                              ▼                              ▼
HTTP 403 / 429                 HTTP 407 / 502+                HTTP 200 OK
 Google Block                   Proxy Gateway                  Body Check
┌──────────────┐               ┌──────────────┐               ┌──────────────┐
│  403 (Edge)  │               │ 407 (Auth)   │               │ Consent Wall │
│  429 (Rate)  │               │ 502/503/504  │               │ Sorry/Bot    │
└──────────────┘               │ X-Anyip-Err  │               │ Soft Block   │
                               └──────────────┘               │ Valid Scrape │
                                                              └──────────────┘
```

### 6.1 The 10 Specific Response Outcomes

| Outcome Code | Trigger Condition | Classification | Retried by Scraper? |
|---|---|---|---|
| `ok` | HTTP 200 and body contains valid search/place data without bot signatures | Successful Request | No |
| `consent` | HTTP 200 with consent markers: `consent.google.com`, `before you continue`, `agree to google` | Google Block | **Never** |
| `403` | HTTP 403 Forbidden from Google edge | Google Block | **Never** |
| `429` | HTTP 429 Too Many Requests from Google edge | Google Block | **Never** |
| `sorry` | HTTP 200 with bot markers: `/sorry/index`, `unusual traffic`, `recaptcha`, `automated queries` | Google Block | **Never** |
| `soft_block` | HTTP 200 with troubleshooting text (`why did this happen`, `troubleshooting`) and lacking `tbm=map` | Google Block | **Never** |
| `google_5xx` | HTTP 500, 501, 505+ originating from Google servers | Google Server Error | **Never** |
| `proxy_error` | HTTP 407 (auth failed), 400/409/413 (AnyIP client errors), 502/503/504 (peer unreachable / gateway timeout), or non-200 with `X-Anyip-Error` header | Proxy Infrastructure Failure | **Yes (max 2 retries)** |
| `other_error` | HTTP 300–399, unhandled 400–499 | Protocol / Request Error | No |
| `network_error` | Status 0: TCP reset, client timeout, DNS resolution failure | Transport Failure | **Yes (max 2 retries)** |

### 6.2 Query-Level Status Derivation
Because scraping a query involves multiple paginated requests, query outcomes are derived strictly from the full request sequence:
* **`success`**: Reached target depth or natural end (`limit` or `no_new`), encountered zero blocks or errors, and extracted places $\ge \text{expected\_min\_results}$.
* **`partial`**: Extracted $\ge 1$ places, but execution terminated prematurely due to a block, proxy error, network failure, or session loss.
* **`blocked`**: Extracted 0 places and terminated on an anti-bot block (`403`, `429`, `consent`, `sorry`, `soft_block`).
* **`empty`**: Extracted 0 places with zero blocks or errors, stopping naturally on `no_new` or `limit`.
* **`error`**: Extracted 0 places and terminated due to proxy failure, network failure, or parse failure.

### 6.3 Artifact Bundle Distinction
The output artifacts produced by the framework depend on the execution path:
* **Experiment Workflow (`experiment.py` / `bench.py --experiment`)**:
  - `run.db`: SQLite 3 WAL database recording full attempt audits (`requests` with `attempt`, `is_final`), `sessions`, `places`, `ip_observations`, and `events`.
  - `summary.json`: High-level aggregate metrics maintained for Next.js UI backward compatibility.
  - `analysis.json`: Comprehensive statistical metrics (Wilson CIs, Kaplan-Meier tables, RBO, Kendall tau, ASN breakdowns).
  - `report.md`: Publication-grade, self-contained Markdown report.
  - Raw CSVs: `sessions.csv`, `requests.csv`, `places.csv`, `ip_observations.csv`, `events.csv`.
* **Legacy Benchmark Workflow (`bench.py`)**:
  - `summary.json`: Aggregate metrics, country breakdown, and query list.
  - `queries.csv`: Basic per-query status, place counts, and latency.
  - `places.csv`: Extracted place metadata (name, address, rating, coordinates).

---

## 7. IP Witness Architecture & Measurement Limitations

### 7.1 The Single-Probe Fallacy
A common flaw in proxy benchmarking is calling a single IPv4 endpoint (e.g., `https://api.ipify.org`) and declaring the returned string as "the proxy IP". In production web scraping:
1. **Google Dual-Stack Routing**: Google Front-End (GFE) CDN edges advertise dual-stack IPv4 (`A`) and IPv6 (`AAAA`) DNS records. Depending on client resolver precedence, OS socket defaults, and proxy gateway routing, Google traffic may route over IPv6 while an external probe routes over IPv4.
2. **Carrier-Grade NAT (CGNAT) Multi-Homing**: Residential and mobile ISPs routinely route outbound traffic across pools of dynamic NAT gateways. An egress connection to AWS/ipify may present IP $A$, while a concurrent connection to Google presents IP $B$.
3. **Gateway Split-Tunneling**: Proxy providers often route traffic through intermediate egress proxies that balance outgoing TCP connections across multiple subnets.

### 7.2 Multi-Witness Observation Methodology
To construct an empirical egress identity without making invalid assumptions, the framework deploys asynchronous multi-witness probing:
* **Dual Witness Mode (Default)**:
  - `ipify_v4`: `https://api.ipify.org/?format=json` (IPv4 family)
  - `ipify_v6`: `https://api64.ipify.org/?format=json` (Dual-stack IPv4/IPv6 family)
* **Multi Witness Mode (Comprehensive)**:
  - `amazon`: `https://checkip.amazonaws.com` (AWS edge view)
  - `httpbin`: `https://httpbin.org/ip` (Origin reflection)
  - `ifconfig`: `https://ifconfig.co/ip` (Independent transit view)

Every observation records timestamp $\tau$, logical request index, witness name, address family, egress IP string, latency, and connection errors. Sessions are analyzed not as a single static string, but as an **observed IP witness set** $\mathcal{W}_{\text{session}}$.

### 7.3 Empirical Pilot Observations
- **Pilot Scope**: 2 TTL sessions, 2 block-onset sessions with 5-request budget, 1 variance query; 0 blocks; residential only; no load arm. This is a pipeline validation, not evidence of unblockability.

Initial real-world pilot runs on AnyIP revealed four critical empirical dynamics that inform valid statistical interpretation:
1. **Pipeline Validation vs Pool Quality**: The pilot was designed strictly to validate database writes, proxy connectivity, and analysis pipelines under minimal volume. It did **not** evaluate mobile pools, did **not** execute the load arm, and observed 0 blocks over a small 5-request budget. By the Rule of Three, observing 0 blocks in 2 sessions provides zero evidence that residential proxies are immune to Google rate-limiting.
2. **Dual-Stack Witness Discordance**: Within a single unbroken session, concurrent probes returned IPv4 addresses from `ipify_v4` and IPv6 addresses from `ipify_v6`. Evaluating dual-stack differences as an "IP rotation" is a methodological error; IPv4 and IPv6 witnesses must be tracked by their address family, and egress drift should only be assessed within the same address family.
3. **Provider ASN Label Noise**: Autonomous ISP and ASN enrichment of observed exit IPs showed that residential and mobile pool designations exhibit cross-contamination at the provider edge (e.g., cellular mobile carrier ASNs occasionally appearing in the residential pool and broadband ASNs in the mobile pool). Scientific comparisons must classify sessions by observed BGP Autonomous System Number (ASN) and carrier rather than solely trusting the user-configured `proxy_type` parameter.
4. **Transport Resilience vs Anti-Bot Blocks**: Transient TCP connection drops and gateway resets occur in real residential networks. In the pilot benchmark, an isolated network error occurred and was cleanly recovered via attempt-level retries. Classifying transport failures (status 0, 502/504) as anti-bot blocks distorts Google survival models; they must remain strictly segregated in the failure taxonomy.

---

## 8. Confounders & Threats to Validity

### 8.1 AnyIP "Sticky" Sessions Are NOT Static Dedicated IPs
* **The Mechanism**: AnyIP sticky sessions route traffic through peer-to-peer residential nodes (volunteers or SDK-monetized devices).
* **The Confounder**: A sticky session token (`session_<id>`) expresses an **intent** to route through the same residential peer node for the configured lifetime (`sesstime_<min>`). However, if that residential device turns off, disconnects from Wi-Fi, or loses cellular signal, AnyIP's proxy gateway will **automatically replace the peer node** and assign a new egress IP to the same session token.
* **Mitigation**: To detect this, the framework performs start-of-session, interval, and end-of-session witness probes. Furthermore, when strict stickiness is required, configuring `sessreplace_false` (`strict_replace=True` / `--strict-sticky`) instructs the AnyIP gateway to terminate the connection with an error rather than silently migrating the IP.

### 8.2 Client-Side Fingerprint Confounders
* **HTTP/1.1 vs HTTP/2**: `aiohttp` operates over HTTP/1.1. Google edge servers evaluate TLS Client Hello fingerprints (JA3/JA4) and HTTP/2 settings frames. Some blocks observed may reflect HTTP/1.1 client anomalies rather than IP reputation alone.
* **Browser Headers**: Request headers must accurately mimic modern desktop Chrome (including `User-Agent`, `Accept-Language`, and `Sec-CH-UA` headers). Inconsistent headers trigger bot challenges irrespective of proxy quality.

### 8.3 Geographical & Query Heterogeneity
* **Density Variances**: Searching "restaurants in Mumbai" produces dense listings yielding 20 items per page across 5+ pages. Searching "dentists in small_town" may exhaust all results after 1 page. Comparing raw place yields across heterogeneous queries introduces bias; all cross-pool comparisons must execute identical standardized panel CSVs (`queries/panel_all.csv`).

### 8.4 Network Latency & Gateway Congestion
* Residential proxy latency includes three legs: client $\to$ AnyIP gateway $\to$ residential peer $\to$ Google edge. Latency spikes may stem from residential peer uplink saturation rather than Google throttling.

### 8.5 TCP Connection Resets vs Sticky Continuity (`--rotation`)
* **The Mechanism**: Enabling `--rotation` sets `force_close=True` on `aiohttp.TCPConnector`, forcing the client to close the underlying TCP socket immediately after every HTTP request. Dropping the TCP connection breaks tunnel reuse, requiring AnyIP's forward proxy to establish a new egress circuit with a newly allocated exit IP on the subsequent connection.
* **Impact on Scraping**: Because modern Google Maps scraping relies on continuity of cookies, SPA session state, and CORS tokens generated during the initial page bootstrap, forcing IP rotation between page 1 and pagination page 2 destroys session continuity. Google's edge anomaly detectors flag the IP mismatch, resulting in immediate cookie consent re-interception, sorry captchas, or HTTP 403/429 rejections. High failure rates under `--rotation` reflect Google's session continuity enforcement rather than poor proxy IP reputation.

---

## 9. Acceptance Criteria & Falsification Conditions

An experimental or benchmark run is scientifically valid only if it satisfies all of the following criteria:

1. **Zero Unhandled Exceptions**: All HTTP requests, connection timeouts, and database transactions complete within structured exception handlers.
2. **Reconciled Accounting Equations**:
   - $\text{Total Requests} = \sum \text{Request Outcomes} = \text{ok} + \text{consent} + 403 + 429 + \text{sorry} + \text{soft\_block} + \text{google\_5xx} + \text{proxy\_error} + \text{other\_error} + \text{network\_error}$
   - $\text{Total Queries} = \text{success} + \text{partial} + \text{blocked} + \text{empty} + \text{error}$
   - $\text{Total Recorded Requests} \ge \text{Final Requests}$ (attempts accurately logged).
3. **Statistical Integrity**:
   - Zero point estimates reported without sample size $n$.
   - All rates include 95% Wilson score confidence intervals.
   - Zero-event rates report Rule-of-Three upper bounds.
   - Kaplan-Meier calculations explicitly account for right-censoring.
4. **Quota Boundary Enforcement**:
   - The run halted immediately upon reaching the configured `quota_cap_mb` threshold (or `--quota-cap-mb` CLI override) or when AnyIP account remaining bandwidth fell below 200 MB.

---

## 10. Epistemic Scope: What Conclusions Are and Aren't Valid

To maintain scientific integrity and prevent overclaiming, researchers and practitioners must adhere to strict interpretive boundaries:

### What Conclusions ARE Valid
* **Empirical Session Survival**: "Under sequential scraping at 1 request/sec with AnyIP residential proxies in India, sticky sessions achieved a median survival of $X$ requests before encountering a Google block (95% CI: $[A, B]$)."
* **Observed Peer Replacement Frequency**: "During a 15-minute observation window without scraping traffic, AnyIP sticky sessions experienced a peer replacement rate of $Y\%$ (95% CI: $[C, D]$)."
* **Within-Session SERP Stability**: "Identical queries repeated under the same exit IP yielded an average RBO of $0.94$, compared to $0.71$ across distinct exit IPs, confirming significant localized ranking variance across proxy egress points."
* **Comparative Cost Efficiency**: "The mobile proxy pool yielded $M$ places per megabyte of billed wire traffic compared to $R$ places per megabyte on the residential pool."

### What Conclusions ARE NOT Valid
* ❌ **"AnyIP sticky sessions provide static residential IPs"**: False. AnyIP sticky sessions route through dynamic consumer devices subject to natural churn and automatic gateway replacement.
* ❌ **"A 0% block rate across 20 sessions proves the pool is unblockable"**: False. By the Rule of Three, 0 blocks in 20 sessions only establishes an upper bound of $3/20 = 15.0\%$ block probability at 95% confidence.
* ❌ **"An IPv4 ipify observation represents the exact IP Google evaluated"**: False. Google Maps routing is dual-stack and CDN-distributed; external witness probes are approximate indicators of egress identity.
* ❌ **"Performance on Google Maps predicts performance on other Google surfaces"**: False. Google Maps, Google Search, Google Scholar, and YouTube maintain distinct anti-bot thresholds, rate limits, and risk models.
