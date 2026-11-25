'use client';

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  RunIndexManifest,
  RunIndexItem,
  RunSummary,
  CountryStatData,
  PairVarianceItem,
} from '@/lib/types';
import {
  formatNumber,
  formatPercent,
  formatDuration,
} from '@/lib/utils';

const MANIFEST_URL =
  'https://raw.githubusercontent.com/devsanthoshmk/anyip-maps-benchmark/main/docs/public-runs/index.json';

function resolveRelativeUrl(baseUrl: string, relativePath: string): string {
  try {
    return new URL(relativePath, baseUrl).toString();
  } catch {
    const lastSlash = baseUrl.lastIndexOf('/');
    const dir = lastSlash !== -1 ? baseUrl.slice(0, lastSlash + 1) : baseUrl;
    return `${dir}${relativePath.replace(/^\//, '')}`;
  }
}

function getCountryName(code: string): string {
  const names: Record<string, string> = {
    IN: 'India',
    US: 'United States',
    GB: 'United Kingdom',
    DE: 'Germany',
    FR: 'France',
    CA: 'Canada',
    AU: 'Australia',
    JP: 'Japan',
    BR: 'Brazil',
    SG: 'Singapore',
  };
  return names[code.toUpperCase()] || code.toUpperCase();
}

export default function BenchmarkPage() {
  const [manifest, setManifest] = useState<RunIndexManifest | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runData, setRunData] = useState<RunSummary | null>(null);
  const [loadingManifest, setLoadingManifest] = useState(true);
  const [loadingRun, setLoadingRun] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fetch index manifest
  const loadManifest = useCallback(async () => {
    setLoadingManifest(true);
    setError(null);
    try {
      const res = await fetch(MANIFEST_URL, { cache: 'no-cache' });
      if (!res.ok) {
        throw new Error(`Failed to load benchmark index (${res.status})`);
      }
      const data: RunIndexManifest = await res.json();
      setManifest(data);
      if (data.runs && data.runs.length > 0) {
        setSelectedRunId((prev) => {
          if (prev && data.runs.some((r) => r.id === prev)) return prev;
          return data.runs[0].id;
        });
      }
    } catch (err: any) {
      console.error('Failed to fetch manifest:', err);
      setError(err?.message || 'Could not connect to benchmark data');
    } finally {
      setLoadingManifest(false);
    }
  }, []);

  useEffect(() => {
    loadManifest();
  }, [loadManifest]);

  // Fetch active run data
  const loadRun = useCallback(async (runItem: RunIndexItem) => {
    setLoadingRun(true);
    setError(null);
    try {
      const runUrl = resolveRelativeUrl(MANIFEST_URL, runItem.path);
      const res = await fetch(runUrl, { cache: 'no-cache' });
      if (!res.ok) {
        throw new Error(`Failed to load run ${runItem.id} (${res.status})`);
      }
      const data: RunSummary = await res.json();
      setRunData(data);
    } catch (err: any) {
      console.error(`Failed to fetch run data for ${runItem.id}:`, err);
      setError(err?.message || `Failed to load run ${runItem.id}`);
    } finally {
      setLoadingRun(false);
    }
  }, []);

  useEffect(() => {
    if (!manifest || !manifest.runs || manifest.runs.length === 0 || !selectedRunId) return;
    const currentItem = manifest.runs.find((r) => r.id === selectedRunId);
    if (currentItem) {
      loadRun(currentItem);
    }
  }, [manifest, selectedRunId, loadRun]);

  const activeRunItem = useMemo(() => {
    return manifest?.runs?.find((r) => r.id === selectedRunId) || null;
  }, [manifest, selectedRunId]);

  // Core metrics derived directly from run data
  const stats = useMemo(() => {
    if (!runData || !runData.totals) return null;
    const totals = runData.totals;
    const reqTotal = totals.total_requests || 1;
    const queryTotal = totals.total_queries || 1;

    const okPct = (totals.ok_requests / reqTotal) * 100;
    const consentPct = (totals.consent_requests / reqTotal) * 100;
    const hardBlocks = (totals.block_403 || 0) + (totals.block_429 || 0);
    const querySuccessPct = (totals.successful_queries / queryTotal) * 100;

    const wireMb = totals.traffic_mb_wire || 0;
    const accountBytes = runData.account_usage_delta_bytes;
    const accountMb = typeof accountBytes === 'number' ? accountBytes / (1024 * 1024) : null;
    const overheadPct = accountMb && wireMb > 0 ? ((accountMb - wireMb) / wireMb) * 100 : null;

    const sc = runData.sticky_continuity;
    const stickyTotal = sc ? sc.query_ip_matched + sc.query_ip_rotated + sc.query_ip_unknown : 0;
    const matchedPct = stickyTotal > 0 && sc ? (sc.query_ip_matched / stickyTotal) * 100 : 0;
    const rotatedPct = stickyTotal > 0 && sc ? (sc.query_ip_rotated / stickyTotal) * 100 : 0;
    const unknownPct = stickyTotal > 0 && sc ? (sc.query_ip_unknown / stickyTotal) * 100 : 0;

    return {
      totals,
      reqTotal,
      queryTotal,
      okPct,
      consentPct,
      hardBlocks,
      querySuccessPct,
      wireMb,
      accountMb,
      overheadPct,
      sc,
      stickyTotal,
      matchedPct,
      rotatedPct,
      unknownPct,
    };
  }, [runData]);

  // Country comparisons
  const countryList = useMemo(() => {
    if (!runData || !runData.by_country) return [];
    return Object.entries(runData.by_country).map(([code, data]) => {
      const q = data.queries || 1;
      const successRate = (data.successful_queries / q) * 100;
      const softBlocks = data.soft_block || 0;
      return {
        code,
        name: getCountryName(code),
        raw: data,
        successRate,
        softBlocks,
      };
    });
  }, [runData]);

  // Shared scale for country bars (based on maximum places extracted)
  const maxCountryPlaces = useMemo(() => {
    if (countryList.length === 0) return 2000;
    const maxPlaces = Math.max(...countryList.map((c) => c.raw.places));
    return Math.max(maxPlaces, 2000);
  }, [countryList]);

  // Variance data (same-IP vs new-IP rank overlap)
  const variance = useMemo(() => {
    if (!runData || !runData.variance) return null;
    const pairObj = runData.variance.by_pair_type || runData.variance;
    if (!pairObj || typeof pairObj !== 'object') return null;

    const sameIp = pairObj.same_ip as PairVarianceItem | undefined;
    const newIp = pairObj.new_ip as PairVarianceItem | undefined;

    if (!sameIp && !newIp) return null;

    return {
      sameIp,
      newIp,
    };
  }, [runData]);

  return (
    <div className="min-h-screen bg-[#101114] text-[#eeeff2]">
      <main className="mx-auto max-w-3xl px-4 sm:px-6 py-8 sm:py-14 space-y-12 sm:space-y-16">
        {/* Compact Masthead */}
        <header className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-6 border-b border-[#262930]">
          <div className="flex items-center gap-3">
            <span className="text-sm font-semibold tracking-tight text-[#eeeff2]">
              AnyIP Maps Benchmark
            </span>
            <span className="flex items-center gap-1.5 text-xs text-[#8a8f9c]">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  loadingManifest || loadingRun
                    ? 'bg-amber-400 animate-pulse'
                    : error
                      ? 'bg-rose-500'
                      : 'bg-emerald-400'
                }`}
                aria-hidden="true"
              />
              <span>
                {loadingManifest || loadingRun
                  ? 'Updating'
                  : error
                    ? 'Sync error'
                    : 'Synced'}
              </span>
            </span>
          </div>

          <nav className="flex items-center gap-4 text-xs text-[#8a8f9c]">
            {manifest?.runs && manifest.runs.length > 1 && (
              <div className="flex items-center gap-1.5">
                <label htmlFor="run-select" className="sr-only">
                  Select benchmark run
                </label>
                <select
                  id="run-select"
                  value={selectedRunId || ''}
                  onChange={(e) => setSelectedRunId(e.target.value)}
                  className="bg-[#17181c] border border-[#262930] text-[#eeeff2] text-xs rounded px-2 py-1 focus:border-[#3b82f6] outline-none"
                >
                  {manifest.runs.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.title || r.id}
                    </option>
                  ))}
                </select>
              </div>
            )}

            <a
              href="https://github.com/devsanthoshmk/anyip-maps-benchmark/blob/main/docs/first-report/README.md"
              target="_blank"
              rel="noreferrer"
              className="hover:text-[#eeeff2] transition-colors"
            >
              Report
            </a>

            <a
              href="https://github.com/devsanthoshmk/anyip-maps-benchmark"
              target="_blank"
              rel="noreferrer"
              className="hover:text-[#eeeff2] transition-colors"
            >
              Source
            </a>

            <button
              onClick={() => {
                loadManifest();
                if (activeRunItem) loadRun(activeRunItem);
              }}
              disabled={loadingManifest || loadingRun}
              className="hover:text-[#eeeff2] transition-colors disabled:opacity-40"
            >
              Refresh
            </button>
          </nav>
        </header>

        {/* Error message */}
        {error && (
          <div className="rounded border border-rose-900/60 bg-rose-950/20 px-4 py-3 text-xs text-rose-300">
            <div className="font-medium text-rose-200">Unable to load benchmark data</div>
            <p className="mt-0.5 text-rose-300/80">{error}</p>
          </div>
        )}

        {/* Loading placeholder */}
        {(loadingManifest || loadingRun) && !runData && (
          <div className="space-y-4 py-8 text-xs text-[#8a8f9c]">
            <div className="h-10 w-32 rounded bg-[#17181c] animate-pulse" />
            <div className="h-6 w-72 rounded bg-[#17181c] animate-pulse" />
            <div className="h-4 w-48 rounded bg-[#17181c] animate-pulse" />
          </div>
        )}

        {/* Active Telemetry View */}
        {runData && stats && (
          <>
            {/* Decisive Editorial Headline & Numbers */}
            <section className="space-y-4">
              <div className="flex flex-col gap-1.5">
                <div className="text-5xl sm:text-6xl font-medium tracking-tight text-[#eeeff2] tabular-nums font-mono">
                  {stats.totals.total_places.toLocaleString()}
                </div>
                <h1 className="text-lg sm:text-xl font-normal text-[#eeeff2] tracking-tight">
                  place records extracted across {stats.totals.successful_queries} completed searches
                </h1>
              </div>

              {/* Three small inline numbers */}
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-[#8a8f9c] pt-2">
                <div>
                  <span className="font-medium text-[#eeeff2] tabular-nums font-mono">
                    {stats.totals.successful_queries} / {stats.totals.total_queries}
                  </span>{' '}
                  <span>successful searches ({formatPercent(stats.querySuccessPct, 0)})</span>
                </div>

                <span className="text-[#3a3d48] select-none" aria-hidden="true">•</span>

                <div>
                  <span className="font-medium text-[#eeeff2] tabular-nums font-mono">
                    {formatDuration(runData.elapsed_s)}
                  </span>{' '}
                  <span>duration</span>
                </div>

                <span className="text-[#3a3d48] select-none" aria-hidden="true">•</span>

                <div>
                  <span className="font-medium text-[#eeeff2] tabular-nums font-mono">
                    {stats.totals.total_requests}
                  </span>{' '}
                  <span>HTTP attempts</span>
                </div>
              </div>
            </section>

            {/* Country Comparison */}
            <section className="space-y-4">
              <div>
                <h2 className="text-sm font-semibold text-[#eeeff2] tracking-tight">
                  Country comparison
                </h2>
                <p className="text-xs text-[#8a8f9c] mt-0.5">
                  Extracted place records on a common scale ({maxCountryPlaces.toLocaleString()} max).
                </p>
              </div>

              <div className="space-y-5 pt-1">
                {countryList.map((c) => {
                  const placePct = Math.min(100, (c.raw.places / maxCountryPlaces) * 100);
                  return (
                    <div key={c.code} className="space-y-1.5">
                      <div className="flex items-baseline justify-between text-xs">
                        <span className="font-medium text-[#eeeff2]">
                          {c.name}{' '}
                          <span className="font-mono text-[#5a5e6b] font-normal">
                            ({c.code})
                          </span>
                        </span>
                        <span className="font-mono tabular-nums text-[#eeeff2] font-medium">
                          {c.raw.places.toLocaleString()} places
                        </span>
                      </div>

                      {/* Horizontal bar on common scale */}
                      <div
                        className="h-2 w-full bg-[#18191e] rounded-sm overflow-hidden"
                        role="progressbar"
                        aria-valuenow={c.raw.places}
                        aria-valuemin={0}
                        aria-valuemax={maxCountryPlaces}
                        aria-label={`${c.name} extracted places`}
                      >
                        <div
                          className="h-full bg-[#3b82f6] rounded-sm transition-all duration-300"
                          style={{ width: `${placePct}%` }}
                        />
                      </div>

                      {/* Under-bar metadata */}
                      <div className="flex flex-wrap items-center justify-between gap-y-1 text-xs text-[#8a8f9c]">
                        <span className="font-mono tabular-nums">
                          {c.raw.successful_queries} of {c.raw.queries} searches ({formatPercent(c.successRate, 0)})
                        </span>
                        <div className="flex items-center gap-3 font-mono tabular-nums">
                          <span>{Math.round(c.raw.avg_latency_ms).toLocaleString()} ms latency</span>
                          {c.softBlocks > 0 && (
                            <span className="text-amber-400">
                              {c.softBlocks} soft {c.softBlocks === 1 ? 'block' : 'blocks'}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>

            {/* IP Continuity Strip */}
            {stats.sc && (
              <section className="space-y-4">
                <div>
                  <h2 className="text-sm font-semibold text-[#eeeff2] tracking-tight">
                    Session IP continuity
                  </h2>
                  <p className="text-xs text-[#8a8f9c] mt-0.5">
                    Proportion of queries retaining the designated residential egress IP during sticky sessions.
                  </p>
                </div>

                <div className="space-y-3 pt-1">
                  {/* Single segmented continuity strip */}
                  <div
                    className="h-2.5 w-full bg-[#18191e] rounded-sm overflow-hidden flex"
                    aria-label="IP continuity proportion bar"
                  >
                    <div
                      style={{ width: `${stats.matchedPct}%` }}
                      className="bg-[#3b82f6] h-full transition-all duration-300"
                      title={`Matched: ${stats.sc.query_ip_matched}`}
                    />
                    <div
                      style={{ width: `${stats.rotatedPct}%` }}
                      className="bg-amber-500 h-full transition-all duration-300"
                      title={`Rotated: ${stats.sc.query_ip_rotated}`}
                    />
                    <div
                      style={{ width: `${stats.unknownPct}%` }}
                      className="bg-[#3a3d48] h-full transition-all duration-300"
                      title={`Untracked: ${stats.sc.query_ip_unknown}`}
                    />
                  </div>

                  {/* Counts below strip */}
                  <div className="flex flex-wrap items-center gap-6 text-xs">
                    <div className="flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-full bg-[#3b82f6]" aria-hidden="true" />
                      <span className="text-[#eeeff2] font-mono tabular-nums">
                        {stats.sc.query_ip_matched} matched
                      </span>
                      <span className="text-[#8a8f9c]">({formatPercent(stats.matchedPct, 0)})</span>
                    </div>

                    <div className="flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-full bg-amber-500" aria-hidden="true" />
                      <span className="text-[#eeeff2] font-mono tabular-nums">
                        {stats.sc.query_ip_rotated} rotated
                      </span>
                      <span className="text-[#8a8f9c]">({formatPercent(stats.rotatedPct, 0)})</span>
                    </div>

                    <div className="flex items-center gap-1.5">
                      <span className="h-2 w-2 rounded-full bg-[#3a3d48]" aria-hidden="true" />
                      <span className="text-[#eeeff2] font-mono tabular-nums">
                        {stats.sc.query_ip_unknown} untracked
                      </span>
                      <span className="text-[#8a8f9c]">({formatPercent(stats.unknownPct, 0)})</span>
                    </div>
                  </div>

                  <p className="text-xs text-[#8a8f9c] leading-relaxed pt-1">
                    The counts compare the exit IP seen by an independent service before and after each search. A match does not rule out a change between checks.
                  </p>
                </div>
              </section>
            )}

            {/* Paired Rank Overlap Plots */}
            {variance && (
              <section className="space-y-4">
                <div>
                  <h2 className="text-sm font-semibold text-[#eeeff2] tracking-tight">
                    Search result rank overlap
                  </h2>
                  <p className="text-xs text-[#8a8f9c] mt-0.5">
                    Overlap metrics between identical search pairs executed on the same residential IP versus across rotated IPs.
                  </p>
                </div>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-8 pt-2">
                  {/* Same IP column */}
                  {variance.sameIp && (
                    <div className="space-y-3.5">
                      <div className="flex items-baseline justify-between text-xs pb-1 border-b border-[#262930]">
                        <span className="font-medium text-[#eeeff2]">Same IP</span>
                        <span className="font-mono text-[#8a8f9c]">
                          n = {variance.sameIp.n_pairs} pairs
                        </span>
                      </div>

                      <div className="space-y-2.5">
                        <OverlapMetricRow
                          label="Jaccard @ 20"
                          value={variance.sameIp.jaccard_20_mean}
                        />
                        <OverlapMetricRow
                          label="Jaccard @ 40"
                          value={variance.sameIp.jaccard_40_mean}
                        />
                        <OverlapMetricRow
                          label="RBO (p=0.90)"
                          value={variance.sameIp.rbo_mean}
                        />
                        <OverlapMetricRow
                          label="Kendall tau"
                          value={variance.sameIp.kendall_tau_mean}
                        />
                      </div>
                    </div>
                  )}

                  {/* New IP column */}
                  {variance.newIp && (
                    <div className="space-y-3.5">
                      <div className="flex items-baseline justify-between text-xs pb-1 border-b border-[#262930]">
                        <span className="font-medium text-[#eeeff2]">New IP</span>
                        <span className="font-mono text-[#8a8f9c]">
                          n = {variance.newIp.n_pairs} pairs
                        </span>
                      </div>

                      <div className="space-y-2.5">
                        <OverlapMetricRow
                          label="Jaccard @ 20"
                          value={variance.newIp.jaccard_20_mean}
                        />
                        <OverlapMetricRow
                          label="Jaccard @ 40"
                          value={variance.newIp.jaccard_40_mean}
                        />
                        <OverlapMetricRow
                          label="RBO (p=0.90)"
                          value={variance.newIp.rbo_mean}
                        />
                        <OverlapMetricRow
                          label="Kendall tau"
                          value={variance.newIp.kendall_tau_mean}
                        />
                      </div>
                    </div>
                  )}
                </div>

                <p className="text-xs text-[#8a8f9c] leading-relaxed pt-2">
                  Sample sizes: {variance.sameIp?.n_pairs ?? 0} same-IP pairs, {variance.newIp?.n_pairs ?? 0} new-IP pairs. These metrics compare result overlap for identical query pairs in this run. They describe observed overlap and do not imply that IP rotation caused the differences.
                </p>
              </section>
            )}

            {/* Methods, Limits, and Raw Evidence */}
            <section className="pt-6 border-t border-[#262930] space-y-4">
              <details className="group">
                <summary className="flex items-center justify-between text-xs font-medium text-[#8a8f9c] hover:text-[#eeeff2] cursor-pointer select-none py-1">
                  <span>Methodology and limitations</span>
                  <span className="text-[10px] text-[#5a5e6b] group-open:rotate-180 transition-transform">
                    ▾
                  </span>
                </summary>

                <div className="mt-3 space-y-3 text-xs text-[#8a8f9c] leading-relaxed pl-1">
                  <div>
                    <span className="text-[#eeeff2] font-medium">Soft blocks and consent dialogs.</span>
                    <p className="mt-0.5">
                      Google Maps returns cookie consent walls or empty results with HTTP 200 rather than HTTP 403 Forbidden. Scrapers must inspect parsed place arrays rather than HTTP response codes alone.
                    </p>
                  </div>

                  <div>
                    <span className="text-[#eeeff2] font-medium">Bandwidth and metering.</span>
                    <p className="mt-0.5">
                      The client estimated {formatNumber(stats.wireMb, 2)} MiB of HTTP traffic. The account meter {stats.accountMb !== null ? `increased by ${stats.accountMb.toFixed(2)} MiB` : 'was not recorded'}. The benchmark does not isolate the difference.
                    </p>
                  </div>

                  <div>
                    <span className="text-[#eeeff2] font-medium">Residential peer lifetime.</span>
                    <p className="mt-0.5">
                      Residential IPs run on consumer hardware with dynamic DHCP leases. Sticky sessions preserve the peer when reachable, but involuntary node turnover occurs when an endpoint disconnects.
                    </p>
                  </div>
                </div>
              </details>

              <div className="flex flex-wrap items-center justify-between gap-2 pt-2 text-xs text-[#5a5e6b]">
                <span>Evidence data</span>
                <div className="flex items-center gap-3">
                  <a
                    href={MANIFEST_URL}
                    target="_blank"
                    rel="noreferrer"
                    className="text-[#8a8f9c] hover:text-[#eeeff2] underline underline-offset-2 transition-colors"
                  >
                    index.json
                  </a>
                  {activeRunItem && (
                    <a
                      href={resolveRelativeUrl(MANIFEST_URL, activeRunItem.path)}
                      target="_blank"
                      rel="noreferrer"
                      className="text-[#8a8f9c] hover:text-[#eeeff2] underline underline-offset-2 transition-colors"
                    >
                      {activeRunItem.id}.json
                    </a>
                  )}
                  <a
                    href="https://github.com/devsanthoshmk/anyip-maps-benchmark"
                    target="_blank"
                    rel="noreferrer"
                    className="text-[#8a8f9c] hover:text-[#eeeff2] underline underline-offset-2 transition-colors"
                  >
                    source repository
                  </a>
                </div>
              </div>
            </section>
          </>
        )}
      </main>
    </div>
  );
}

function OverlapMetricRow({
  label,
  value,
}: {
  label: string;
  value: number | undefined | null;
}) {
  const numericVal = typeof value === 'number' && !isNaN(value) ? value : 0;
  const pct = Math.min(100, Math.max(0, numericVal * 100));

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-xs">
        <span className="text-[#8a8f9c]">{label}</span>
        <span className="font-mono tabular-nums text-[#eeeff2] font-medium">
          {typeof value === 'number' ? value.toFixed(3) : 'N/A'}
        </span>
      </div>

      <div
        className="h-1.5 w-full bg-[#18191e] rounded-sm overflow-hidden"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${label} overlap`}
      >
        <div
          className="h-full bg-[#3b82f6] rounded-sm transition-all duration-300"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
