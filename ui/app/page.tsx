'use client';

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip as RechartsTooltip,
  Legend,
} from 'recharts';
import {
  Activity,
  CheckCircle2,
  AlertTriangle,
  ShieldAlert,
  Globe,
  RefreshCw,
  ExternalLink,
  Layers,
  Wifi,
  Clock,
  HardDrive,
  Info,
  TrendingUp,
  BarChart3,
  Flame,
  ArrowUpRight,
  ShieldCheck,
  Server,
  Zap,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card';
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
  formatBytesToMb,
  formatDuration,
} from '@/lib/utils';

const MANIFEST_URL =
  'https://raw.githubusercontent.com/devsanthoshmk/anyip-maps-benchmark/main/docs/public-runs/index.json';

const PALETTE = {
  emerald: '#10b981',
  amber: '#f59e0b',
  rose: '#ef4444',
  orange: '#f97316',
  indigo: '#6366f1',
  cyan: '#06b6d4',
  slate: '#64748b',
};

function resolveRelativeUrl(baseUrl: string, relativePath: string): string {
  try {
    return new URL(relativePath, baseUrl).toString();
  } catch {
    const lastSlash = baseUrl.lastIndexOf('/');
    const dir = lastSlash !== -1 ? baseUrl.slice(0, lastSlash + 1) : baseUrl;
    return `${dir}${relativePath.replace(/^\//, '')}`;
  }
}

function getCountryFlag(code: string): string {
  const flags: Record<string, string> = {
    IN: '🇮🇳',
    US: '🇺🇸',
    GB: '🇬🇧',
    DE: '🇩🇪',
    FR: '🇫🇷',
    CA: '🇨🇦',
    AU: '🇦🇺',
    JP: '🇯🇵',
    BR: '🇧🇷',
    SG: '🇸🇬',
  };
  return flags[code.toUpperCase()] || '🌐';
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

export default function BenchmarkDashboard() {
  const [manifest, setManifest] = useState<RunIndexManifest | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runData, setRunData] = useState<RunSummary | null>(null);
  const [loadingManifest, setLoadingManifest] = useState(true);
  const [loadingRun, setLoadingRun] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [activeTab, setActiveTab] = useState<'sticky' | 'variance' | 'limitations'>('sticky');
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  // Fetch Manifest
  const loadManifest = useCallback(async () => {
    setLoadingManifest(true);
    setError(null);
    try {
      const res = await fetch(MANIFEST_URL, { cache: 'no-cache' });
      if (!res.ok) {
        throw new Error(`Failed to load benchmark index (HTTP ${res.status})`);
      }
      const data: RunIndexManifest = await res.json();
      setManifest(data);
      if (data.runs && data.runs.length > 0) {
        setSelectedRunId((prev) => {
          if (prev && data.runs.some((r) => r.id === prev)) return prev;
          return data.runs[0].id;
        });
      }
      setLastRefreshed(new Date());
    } catch (err: any) {
      console.error('Failed to fetch manifest:', err);
      setError(err?.message || 'Could not connect to benchmark manifest');
    } finally {
      setLoadingManifest(false);
    }
  }, []);

  useEffect(() => {
    loadManifest();
  }, [loadManifest]);

  // Fetch Active Run Data
  const loadRun = useCallback(async (runItem: RunIndexItem) => {
    setLoadingRun(true);
    setError(null);
    try {
      const runUrl = resolveRelativeUrl(MANIFEST_URL, runItem.path);
      const res = await fetch(runUrl, { cache: 'no-cache' });
      if (!res.ok) {
        throw new Error(`Failed to load run ${runItem.id} (HTTP ${res.status})`);
      }
      const data: RunSummary = await res.json();
      setRunData(data);
      setLastRefreshed(new Date());
    } catch (err: any) {
      console.error(`Failed to fetch run data for ${runItem.id}:`, err);
      setError(err?.message || `Failed to fetch run data for ${runItem.id}`);
    } finally {
      setLoadingRun(false);
    }
  }, []);

  // When selected run changes, trigger fetch
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

  // Derived Telemetry Calculations (Strictly dynamic from runData)
  const stats = useMemo(() => {
    if (!runData || !runData.totals) return null;
    const totals = runData.totals;
    const reqTotal = totals.total_requests || 1;
    const queryTotal = totals.total_queries || 1;

    const okPct = (totals.ok_requests / reqTotal) * 100;
    const consentPct = (totals.consent_requests / reqTotal) * 100;
    const hardBlocks = (totals.block_403 || 0) + (totals.block_429 || 0);
    const hardBlockPct = (hardBlocks / reqTotal) * 100;

    const querySuccessPct = (totals.successful_queries / queryTotal) * 100;
    const placesPerQuery = totals.successful_queries > 0
      ? totals.total_places / totals.successful_queries
      : 0;
    const placesPerSec = runData.elapsed_s > 0
      ? totals.total_places / runData.elapsed_s
      : 0;

    // Account usage delta vs Wire transfer
    const wireMb = totals.traffic_mb_wire || 0;
    const accountBytes = runData.account_usage_delta_bytes;
    const accountMb = typeof accountBytes === 'number' ? accountBytes / (1024 * 1024) : null;
    const overheadPct = accountMb && wireMb > 0 ? ((accountMb - wireMb) / wireMb) * 100 : null;

    // Sticky continuity
    const sc = runData.sticky_continuity;
    const stickyTotal = sc ? sc.query_ip_matched + sc.query_ip_rotated + sc.query_ip_unknown : 0;
    const stickyMatchedPct = stickyTotal > 0 && sc ? (sc.query_ip_matched / stickyTotal) * 100 : 0;
    const stickyRotatedPct = stickyTotal > 0 && sc ? (sc.query_ip_rotated / stickyTotal) * 100 : 0;
    const stickyUnknownPct = stickyTotal > 0 && sc ? (sc.query_ip_unknown / stickyTotal) * 100 : 0;

    return {
      totals,
      reqTotal,
      queryTotal,
      okPct,
      consentPct,
      hardBlocks,
      hardBlockPct,
      querySuccessPct,
      placesPerQuery,
      placesPerSec,
      wireMb,
      accountMb,
      overheadPct,
      sticky: sc,
      stickyTotal,
      stickyMatchedPct,
      stickyRotatedPct,
      stickyUnknownPct,
    };
  }, [runData]);

  // Request Outcome Pie Chart Data
  const outcomeData = useMemo(() => {
    if (!runData || !runData.totals) return [];
    const t = runData.totals;
    const items = [
      { name: 'Clean 200 OK', value: t.ok_requests, color: PALETTE.emerald },
      { name: 'Consent Interstitial (200)', value: t.consent_requests, color: PALETTE.amber },
      { name: 'HTTP 403 Forbidden', value: t.block_403, color: PALETTE.rose },
      { name: 'HTTP 429 Rate Limit', value: t.block_429, color: PALETTE.orange },
    ];
    return items.filter((d) => d.value > 0);
  }, [runData]);

  // Country Breakdown Array
  const countryList = useMemo(() => {
    if (!runData || !runData.by_country) return [];
    return Object.entries(runData.by_country).map(([code, data]) => {
      const q = data.queries || 1;
      const successRate = (data.successful_queries / q) * 100;
      const consentRate = ((data.consent || 0) / q) * 100;
      const b403 = data.block_403 ?? data['403'] ?? 0;
      const b429 = data.block_429 ?? data['429'] ?? 0;
      const softBlocks = data.soft_block || 0;
      const matched = data.ip_matched_queries ?? 0;
      const changed = data.ip_changed_queries ?? 0;
      const unknown = data.ip_unknown_queries ?? 0;
      const continuityTotal = matched + changed + unknown;
      const continuityPct = continuityTotal > 0 ? (matched / continuityTotal) * 100 : 0;

      return {
        code,
        name: getCountryName(code),
        flag: getCountryFlag(code),
        raw: data,
        successRate,
        consentRate,
        b403,
        b429,
        softBlocks,
        matched,
        changed,
        unknown,
        continuityPct,
      };
    });
  }, [runData]);

  // Variance Comparison Data
  const varianceList = useMemo(() => {
    if (!runData || !runData.variance) return [];
    const pairObj = runData.variance.by_pair_type || runData.variance;
    if (!pairObj || typeof pairObj !== 'object') return [];

    return Object.entries(pairObj)
      .filter(([_, val]) => val && typeof val === 'object' && 'jaccard_20_mean' in val)
      .map(([type, val]: [string, any]) => ({
        type,
        label:
          type === 'same_ip'
            ? 'Same IP (Within Sticky Session)'
            : type === 'new_ip'
              ? 'New IP (Rotated Cross-Session)'
              : type,
        data: val as PairVarianceItem,
      }));
  }, [runData]);

  return (
    <div className="min-h-screen bg-[#0c0c0c] text-[#ededed]">
      {/* Top Banner Navigation */}
      <header className="border-b border-[#1f1f1f] bg-[#121212]/80 backdrop-blur-md sticky top-0 z-50">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 py-3.5 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[#181818] border border-[#2b2b2b] text-[#6366f1]">
              <Globe className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-sm font-semibold tracking-tight text-[#f5f5f5]">
                  AnyIP Maps Benchmark
                </h1>
                <Badge variant="indigo" className="text-[10px] py-0 px-1.5 font-mono">
                  Cloudflare Static Export
                </Badge>
              </div>
              <p className="text-[11px] text-[#808080]">
                Google Maps Residential Telemetry & Sticky Session Resilience
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 sm:gap-3 text-xs">
            <div className="flex items-center gap-1.5 rounded-md bg-[#161616] border border-[#262626] px-2.5 py-1 text-[#8c8c8c]">
              <span
                className={`h-2 w-2 rounded-full ${
                  loadingManifest || loadingRun
                    ? 'bg-[#f59e0b] animate-ping'
                    : error
                      ? 'bg-[#ef4444]'
                      : 'bg-[#10b981]'
                }`}
              />
              <span className="font-mono text-[11px]">
                {loadingManifest || loadingRun
                  ? 'Fetching feed...'
                  : error
                    ? 'Sync Error'
                    : 'Feed Connected'}
              </span>
            </div>

            {lastRefreshed && (
              <span className="text-[11px] text-[#6b6b6b] hidden lg:inline font-mono">
                Updated {lastRefreshed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
              </span>
            )}

            <a
              href="https://github.com/devsanthoshmk/anyip-maps-benchmark"
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-1 rounded-md border border-[#2a2a2a] bg-[#161616] px-2.5 py-1 text-[11px] text-[#a3a3a3] hover:border-[#444] hover:text-[#ededed] transition"
            >
              <span>GitHub</span>
              <ExternalLink className="h-3 w-3" />
            </a>

            <button
              onClick={() => {
                loadManifest();
                if (activeRunItem) loadRun(activeRunItem);
              }}
              disabled={loadingManifest || loadingRun}
              className="flex items-center gap-1.5 rounded-md border border-[#333] bg-[#1f1f1f] px-3 py-1 text-[11px] font-medium text-[#ededed] hover:border-[#555] hover:bg-[#282828] disabled:opacity-50 transition"
            >
              <RefreshCw
                className={`h-3 w-3 ${loadingManifest || loadingRun ? 'animate-spin' : ''}`}
              />
              <span>Refresh</span>
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8 py-6 space-y-6">
        {/* Error Notice */}
        {error && (
          <div className="rounded-xl border border-[#ef4444]/40 bg-[#ef4444]/10 p-4 text-sm text-[#fca5a5] flex items-start gap-3">
            <AlertTriangle className="h-5 w-5 text-[#ef4444] shrink-0 mt-0.5" />
            <div className="space-y-1">
              <div className="font-semibold text-[#f87171]">Telemetry Feed Exception</div>
              <p className="text-xs text-[#fca5a5]/90">{error}</p>
              <div className="pt-1">
                <a
                  href={MANIFEST_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="font-mono text-[11px] underline text-[#fca5a5] hover:text-white"
                >
                  Inspect raw manifest JSON &rarr;
                </a>
              </div>
            </div>
          </div>
        )}

        {/* Run Selector (Manifest Entries) */}
        {manifest && manifest.runs && manifest.runs.length > 0 && (
          <div className="rounded-xl border border-[#222222] bg-[#131313] p-3 sm:p-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 pb-2.5 border-b border-[#1f1f1f]">
              <div className="flex items-center gap-2">
                <Layers className="h-4 w-4 text-[#6366f1]" />
                <span className="text-xs font-semibold uppercase tracking-wider text-[#999999]">
                  Evaluated Benchmark Runs
                </span>
                <span className="text-xs text-[#595959]">
                  ({manifest.runs.length} public runs in manifest)
                </span>
              </div>
              <span className="text-[11px] text-[#737373]">
                Client-side static fetch with no-cache revalidation
              </span>
            </div>

            <div className="mt-3 flex flex-wrap gap-2">
              {manifest.runs.map((r) => {
                const isActive = r.id === selectedRunId;
                return (
                  <button
                    key={r.id}
                    onClick={() => setSelectedRunId(r.id)}
                    className={`group text-left px-3.5 py-2 rounded-lg border transition text-xs ${
                      isActive
                        ? 'border-[#6366f1] bg-[#6366f1]/15 text-[#f5f5f5]'
                        : 'border-[#262626] bg-[#171717] text-[#a3a3a3] hover:border-[#404040] hover:text-[#ededed]'
                    }`}
                  >
                    <div className="font-semibold flex items-center gap-1.5">
                      <span>{r.title || r.id}</span>
                      {isActive && <span className="h-1.5 w-1.5 rounded-full bg-[#6366f1]" />}
                    </div>
                    <div className="font-mono text-[10px] text-[#6b6b6b] group-hover:text-[#888] mt-0.5">
                      ID: {r.id}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* Loading Skeleton */}
        {(loadingManifest || loadingRun) && !runData && (
          <div className="space-y-4 animate-pulse">
            <div className="h-32 rounded-xl bg-[#161616] border border-[#222]" />
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="h-28 rounded-xl bg-[#161616] border border-[#222]" />
              <div className="h-28 rounded-xl bg-[#161616] border border-[#222]" />
              <div className="h-28 rounded-xl bg-[#161616] border border-[#222]" />
              <div className="h-28 rounded-xl bg-[#161616] border border-[#222]" />
            </div>
            <div className="h-64 rounded-xl bg-[#161616] border border-[#222]" />
          </div>
        )}

        {/* Main Telemetry View */}
        {runData && stats && (
          <>
            {/* Active Run Header Hero */}
            <div className="rounded-xl border border-[#222222] bg-[#141414] p-5 sm:p-6 shadow-sm">
              <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
                <div className="space-y-1.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="text-lg sm:text-xl font-bold tracking-tight text-[#f5f5f5]">
                      {activeRunItem?.title || runData.run_id}
                    </h2>
                    <Badge variant="indigo" className="font-mono text-[10px]">
                      Residential Trial
                    </Badge>
                    <Badge variant="outline" className="font-mono text-[10px]">
                      AnyIP Gateway
                    </Badge>
                  </div>
                  <div className="flex flex-wrap items-center gap-y-1 gap-x-3 text-xs text-[#8c8c8c]">
                    <span className="font-mono text-[#6b6b6b]">Run ID: {runData.run_id}</span>
                    <span className="text-[#3a3a3a]">•</span>
                    <span className="flex items-center gap-1">
                      <Clock className="h-3.5 w-3.5 text-[#6366f1]" />
                      <span className="font-mono text-[#ededed]">
                        {formatDuration(runData.elapsed_s)}
                      </span>{' '}
                      elapsed
                    </span>
                    <span className="text-[#3a3a3a]">•</span>
                    <span>
                      {countryList.length} Geo Jurisdictions ({countryList.map((c) => c.code).join(', ')})
                    </span>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-3 bg-[#1a1a1a] p-3 rounded-lg border border-[#262626]">
                  <div className="text-left sm:text-right">
                    <div className="text-[10px] uppercase font-semibold text-[#737373]">
                      Query Success
                    </div>
                    <div className="font-mono text-xl font-bold text-[#10b981] tabular-nums">
                      {formatPercent(stats.querySuccessPct, 1)}
                    </div>
                    <div className="text-[10px] text-[#6b6b6b] font-mono">
                      {stats.totals.successful_queries} of {stats.totals.total_queries} queries
                    </div>
                  </div>
                  <div className="h-8 w-px bg-[#2b2b2b]" />
                  <div className="text-left sm:text-right">
                    <div className="text-[10px] uppercase font-semibold text-[#737373]">
                      Place Extraction
                    </div>
                    <div className="font-mono text-xl font-bold text-[#f5f5f5] tabular-nums">
                      {formatNumber(stats.totals.total_places)}
                    </div>
                    <div className="text-[10px] text-[#10b981] font-mono">
                      {formatNumber(stats.placesPerSec, 2)} places/sec
                    </div>
                  </div>
                </div>
              </div>
            </div>

            {/* Signal KPI Matrix */}
            <section className="space-y-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-[#a3a3a3]">
                    01 / HTTP Scrape Signals & Telemetry
                  </span>
                  <span className="h-1 w-1 rounded-full bg-[#383838]" />
                  <span className="text-[11px] text-[#737373]">
                    {formatNumber(stats.totals.total_requests)} total HTTP transmissions measured
                  </span>
                </div>
                <div className="text-[11px] text-[#737373] hidden sm:block">
                  Live response classification
                </div>
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
                {/* 1. Clean OK */}
                <Card className="border border-[#1e2a22] bg-[#121613] p-4.5">
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="text-[10px] font-bold uppercase tracking-wider text-[#10b981]">
                        Clean Transmissions
                      </div>
                      <div className="mt-1 flex items-baseline gap-2">
                        <span className="font-mono text-3xl font-extrabold text-[#f5f5f5] tabular-nums">
                          {formatNumber(stats.totals.ok_requests)}
                        </span>
                        <span className="font-mono text-xs font-semibold text-[#10b981] tabular-nums">
                          ({formatPercent(stats.okPct, 1)})
                        </span>
                      </div>
                    </div>
                    <div className="flex h-8 w-8 items-center justify-center rounded border border-[#10b981]/30 bg-[#10b981]/10 text-[#10b981]">
                      <CheckCircle2 className="h-4 w-4" />
                    </div>
                  </div>
                  <div className="mt-3 flex items-center justify-between border-t border-[#1e2a22] pt-2 text-[11px]">
                    <span className="text-[#8a8a8a]">Places Extracted</span>
                    <span className="font-mono font-bold text-[#10b981] tabular-nums">
                      {formatNumber(stats.totals.total_places)}
                    </span>
                  </div>
                  <p className="mt-1 text-[10px] text-[#6b7280]">
                    HTTP 200 OK with valid Google place payloads.
                  </p>
                </Card>

                {/* 2. Consent Interstitials */}
                <Card className="border border-[#2e2615] bg-[#161410] p-4.5">
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="text-[10px] font-bold uppercase tracking-wider text-[#f59e0b]">
                        Consent Interstitials
                      </div>
                      <div className="mt-1 flex items-baseline gap-2">
                        <span className="font-mono text-3xl font-extrabold text-[#f5f5f5] tabular-nums">
                          {formatNumber(stats.totals.consent_requests)}
                        </span>
                        <span className="font-mono text-xs font-semibold text-[#f59e0b] tabular-nums">
                          ({formatPercent(stats.consentPct, 1)})
                        </span>
                      </div>
                    </div>
                    <div className="flex h-8 w-8 items-center justify-center rounded border border-[#f59e0b]/30 bg-[#f59e0b]/10 text-[#f59e0b]">
                      <AlertTriangle className="h-4 w-4" />
                    </div>
                  </div>
                  <div className="mt-3 flex items-center justify-between border-t border-[#2e2615] pt-2 text-[11px]">
                    <Badge variant="warning" className="text-[9px] py-0 px-1 font-mono">
                      HTTP 200 Interstitial
                    </Badge>
                    <span className="font-mono text-[10px] text-[#f59e0b]">
                      0 places parsed
                    </span>
                  </div>
                  <p className="mt-1 text-[10px] text-[#928265]">
                    Google cookie challenge dialog. Not a block, but yields zero places without cookie handling.
                  </p>
                </Card>

                {/* 3. Hard Blocks & Rate Limits */}
                <Card className="border border-[#2a1b1b] bg-[#161111] p-4.5">
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="text-[10px] font-bold uppercase tracking-wider text-[#ef4444]">
                        Hard Blocks / 429
                      </div>
                      <div className="mt-1 flex items-baseline gap-2">
                        <span className="font-mono text-3xl font-extrabold text-[#f5f5f5] tabular-nums">
                          {stats.hardBlocks}
                        </span>
                        <span className="font-mono text-xs font-semibold text-[#ef4444] tabular-nums">
                          ({formatPercent(stats.hardBlockPct, 1)})
                        </span>
                      </div>
                    </div>
                    <div className="flex h-8 w-8 items-center justify-center rounded border border-[#ef4444]/30 bg-[#ef4444]/10 text-[#ef4444]">
                      <ShieldAlert className="h-4 w-4" />
                    </div>
                  </div>
                  <div className="mt-3 flex items-center justify-between border-t border-[#2a1b1b] pt-2 text-[11px]">
                    <span className="text-[#8a8a8a]">403 Forbidden / 429</span>
                    <span className="font-mono font-bold text-[#ef4444] tabular-nums">
                      {stats.totals.block_403} / {stats.totals.block_429}
                    </span>
                  </div>
                  <p className="mt-1 text-[10px] text-[#735353]">
                    Outright bot mitigation. Triggers automated sticky node rotation.
                  </p>
                </Card>

                {/* 4. Wire vs Metered Traffic Delta */}
                <Card className="border border-[#17252a] bg-[#101719] p-4.5">
                  <div className="flex items-start justify-between">
                    <div>
                      <div className="text-[10px] font-bold uppercase tracking-wider text-[#06b6d4]">
                        Bandwidth & Metering
                      </div>
                      <div className="mt-1 flex items-baseline gap-2">
                        <span className="font-mono text-2xl font-extrabold text-[#f5f5f5] tabular-nums">
                          {formatNumber(stats.wireMb, 2)}
                        </span>
                        <span className="text-xs text-[#06b6d4] font-semibold">MB wire</span>
                      </div>
                    </div>
                    <div className="flex h-8 w-8 items-center justify-center rounded border border-[#06b6d4]/30 bg-[#06b6d4]/10 text-[#06b6d4]">
                      <HardDrive className="h-4 w-4" />
                    </div>
                  </div>
                  <div className="mt-3 flex items-center justify-between border-t border-[#17252a] pt-2 text-[11px]">
                    <span className="text-[#8a8a8a]">Account Metered</span>
                    <span className="font-mono font-bold text-[#38bdf8] tabular-nums">
                      {stats.accountMb ? `${stats.accountMb.toFixed(2)} MB` : '—'}
                    </span>
                  </div>
                  <p className="mt-1 text-[10px] text-[#557b85]">
                    {stats.overheadPct !== null
                      ? `Account meter differs from the client estimate by ${stats.overheadPct.toFixed(1)}%. The run does not isolate why.`
                      : 'The account-meter comparison is unavailable.'}
                  </p>
                </Card>
              </div>
            </section>

            {/* Sticky Session Architecture & Operational Limitations Section */}
            <section className="space-y-3">
              <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-1">
                <div className="flex items-center gap-2">
                  <span className="text-[11px] font-bold uppercase tracking-wider text-[#a3a3a3]">
                    02 / Proxy Sticky Flow Architecture & Limitations
                  </span>
                  <span className="h-1 w-1 rounded-full bg-[#383838]" />
                  <span className="text-[11px] text-[#737373]">
                    Session retention mechanics, peer churn, and Google Maps scraping dynamics
                  </span>
                </div>

                {/* Sub tabs */}
                <div className="flex items-center gap-1 rounded-lg bg-[#171717] p-1 border border-[#242424]">
                  <button
                    onClick={() => setActiveTab('sticky')}
                    className={`px-2.5 py-1 text-[11px] font-medium rounded transition ${
                      activeTab === 'sticky'
                        ? 'bg-[#6366f1] text-white'
                        : 'text-[#8a8a8a] hover:text-[#ededed]'
                    }`}
                  >
                    Sticky Telemetry
                  </button>
                  <button
                    onClick={() => setActiveTab('limitations')}
                    className={`px-2.5 py-1 text-[11px] font-medium rounded transition ${
                      activeTab === 'limitations'
                        ? 'bg-[#6366f1] text-white'
                        : 'text-[#8a8a8a] hover:text-[#ededed]'
                    }`}
                  >
                    Proxy Limitations
                  </button>
                  <button
                    onClick={() => setActiveTab('variance')}
                    className={`px-2.5 py-1 text-[11px] font-medium rounded transition ${
                      activeTab === 'variance'
                        ? 'bg-[#6366f1] text-white'
                        : 'text-[#8a8a8a] hover:text-[#ededed]'
                    }`}
                  >
                    Rank Variance (RBO)
                  </button>
                </div>
              </div>

              {/* Tab 1: Sticky Telemetry Breakdown */}
              {activeTab === 'sticky' && stats.sticky && (
                <div className="rounded-xl border border-[#242424] bg-[#141414] p-5 space-y-5">
                  <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 border-b border-[#202020] pb-4">
                    <div>
                      <h3 className="text-sm font-bold text-[#f5f5f5]">
                        Sticky IP Retention Telemetry
                      </h3>
                      <p className="text-xs text-[#8a8a8a] mt-0.5">
                        Measuring whether consecutive requests under a designated sticky session token maintained the identical egress residential node.
                      </p>
                    </div>

                    <div className="flex items-center gap-2">
                      <Badge variant="success" className="font-mono text-xs">
                        {formatPercent(stats.stickyMatchedPct, 1)} Sticky Stability
                      </Badge>
                    </div>
                  </div>

                  {/* Multi-segment visual bar */}
                  <div className="space-y-2">
                    <div className="flex items-center justify-between text-xs">
                      <span className="text-[#8a8a8a]">Continuity Distribution:</span>
                      <span className="font-mono text-[#ededed]">
                        {stats.stickyTotal} total verified queries
                      </span>
                    </div>

                    <div className="h-4 w-full overflow-hidden rounded-md bg-[#1f1f1f] flex">
                      <div
                        style={{ width: `${stats.stickyMatchedPct}%` }}
                        className="bg-[#10b981] transition-all duration-500"
                        title={`Matched: ${stats.sticky.query_ip_matched} (${stats.stickyMatchedPct.toFixed(1)}%)`}
                      />
                      <div
                        style={{ width: `${stats.stickyRotatedPct}%` }}
                        className="bg-[#f59e0b] transition-all duration-500"
                        title={`Rotated: ${stats.sticky.query_ip_rotated} (${stats.stickyRotatedPct.toFixed(1)}%)`}
                      />
                      <div
                        style={{ width: `${stats.stickyUnknownPct}%` }}
                        className="bg-[#64748b] transition-all duration-500"
                        title={`Unknown: ${stats.sticky.query_ip_unknown} (${stats.stickyUnknownPct.toFixed(1)}%)`}
                      />
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-2">
                      {/* Matched */}
                      <div className="rounded-lg bg-[#121613] border border-[#1e2a22] p-3">
                        <div className="flex items-center gap-1.5 text-xs font-semibold text-[#10b981]">
                          <span className="h-2 w-2 rounded-full bg-[#10b981]" />
                          <span>Matched Egress IP</span>
                        </div>
                        <div className="mt-1 font-mono text-2xl font-bold text-[#f5f5f5]">
                          {stats.sticky.query_ip_matched}
                        </div>
                        <div className="text-[11px] text-[#10b981] font-mono">
                          {formatPercent(stats.stickyMatchedPct, 1)} of queries
                        </div>
                        <p className="mt-1 text-[10px] text-[#6b7280]">
                          Target residential IP maintained across the session. Cookies & TLS state remained trusted.
                        </p>
                      </div>

                      {/* Rotated */}
                      <div className="rounded-lg bg-[#161410] border border-[#2e2615] p-3">
                        <div className="flex items-center gap-1.5 text-xs font-semibold text-[#f59e0b]">
                          <span className="h-2 w-2 rounded-full bg-[#f59e0b]" />
                          <span>Involuntary Peer Rotation</span>
                        </div>
                        <div className="mt-1 font-mono text-2xl font-bold text-[#f5f5f5]">
                          {stats.sticky.query_ip_rotated}
                        </div>
                        <div className="text-[11px] text-[#f59e0b] font-mono">
                          {formatPercent(stats.stickyRotatedPct, 1)} of queries
                        </div>
                        <p className="mt-1 text-[10px] text-[#8a7a5a]">
                          Residential node went offline during session. AnyIP pool transparently replaced the peer.
                        </p>
                      </div>

                      {/* Unknown */}
                      <div className="rounded-lg bg-[#16181b] border border-[#222831] p-3">
                        <div className="flex items-center gap-1.5 text-xs font-semibold text-[#94a3b8]">
                          <span className="h-2 w-2 rounded-full bg-[#64748b]" />
                          <span>Untracked / Canary</span>
                        </div>
                        <div className="mt-1 font-mono text-2xl font-bold text-[#f5f5f5]">
                          {stats.sticky.query_ip_unknown}
                        </div>
                        <div className="text-[11px] text-[#94a3b8] font-mono">
                          {formatPercent(stats.stickyUnknownPct, 1)} of queries
                        </div>
                        <p className="mt-1 text-[10px] text-[#64748b]">
                          Header canary probe omitted or external egress IP inspection unavailable.
                        </p>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {/* Tab 2: Architectural Limitations Guide */}
              {activeTab === 'limitations' && (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {/* Card 1: Peer Churn & Sticky TTL */}
                  <Card className="border border-[#222] bg-[#141414] p-5 space-y-3">
                    <div className="flex items-center gap-2 text-sm font-bold text-[#f5f5f5]">
                      <Server className="h-4 w-4 text-[#6366f1]" />
                      <span>1. Residential Peer Churn & Sticky TTL</span>
                    </div>
                    <p className="text-xs text-[#a3a3a3] leading-relaxed">
                      Residential IPs originate from real consumer hardware (home routers, cable/fiber modems, Wi-Fi endpoints). Unlike datacenter servers with 99.99% continuous connection leases, residential endpoints disconnect dynamically due to:
                    </p>
                    <ul className="text-xs text-[#8c8c8c] space-y-1.5 list-disc pl-4">
                      <li>ISP dynamic DHCP lease renewals and daily carrier IP resets.</li>
                      <li>Consumer device sleep cycles or local network restarts.</li>
                      <li>
                        AnyIP sticky session lifetimes typically extend up to 7 days, but individual peer uptime averages 10 to 45 minutes before auto-replacement takes over.
                      </li>
                    </ul>
                  </Card>

                  {/* Card 2: Session Renaming & Strict Allocation */}
                  <Card className="border border-[#222] bg-[#141414] p-5 space-y-3">
                    <div className="flex items-center gap-2 text-sm font-bold text-[#f5f5f5]">
                      <Zap className="h-4 w-4 text-[#f59e0b]" />
                      <span>2. Session Renaming vs Strict Allocation</span>
                    </div>
                    <p className="text-xs text-[#a3a3a3] leading-relaxed">
                      Changing a proxy username’s session ID (e.g. <code className="text-[#38bdf8]">session-abc</code> &rarr; <code className="text-[#38bdf8]">session-xyz</code>) issues a rotation request to the AnyIP gateway, but provider documentation notes:
                    </p>
                    <ul className="text-xs text-[#8c8c8c] space-y-1.5 list-disc pl-4">
                      <li>Changing session names alone does not guarantee a globally unique IP across concurrent workers.</li>
                      <li>
                        Explicit cross-session uniqueness requires the <code className="text-[#38bdf8]">sessipcollision_strict</code> parameter.
                      </li>
                    </ul>
                  </Card>

                  {/* Card 3: Consent Interstitials vs Blunt 403s */}
                  <Card className="border border-[#222] bg-[#141414] p-5 space-y-3">
                    <div className="flex items-center gap-2 text-sm font-bold text-[#f5f5f5]">
                      <ShieldCheck className="h-4 w-4 text-[#10b981]" />
                      <span>3. Consent Interstitials vs Blunt HTTP 403s</span>
                    </div>
                    <p className="text-xs text-[#a3a3a3] leading-relaxed">
                      Google Maps rarely throws a blunt <code className="text-[#ef4444]">HTTP 403 Forbidden</code> or <code className="text-[#f97316]">HTTP 429</code> to residential residential peers. Instead, anti-scraping defenses manifest as:
                    </p>
                    <ul className="text-xs text-[#8c8c8c] space-y-1.5 list-disc pl-4">
                      <li>
                        <strong className="text-[#f59e0b]">Consent Interstitials (HTTP 200):</strong> Intercepts search with a cookie/privacy consent wall. Naive scrapers checking only <code className="text-[#10b981]">status == 200</code> report false positives with 0 extracted records.
                      </li>
                      <li>
                        <strong className="text-[#ededed]">Soft Blocks:</strong> Empty JSON or stripped search results with zero places parsed despite 200 OK.
                      </li>
                    </ul>
                  </Card>

                  {/* Card 4: Wire vs Metered Traffic Realities */}
                  <Card className="border border-[#222] bg-[#141414] p-5 space-y-3">
                    <div className="flex items-center gap-2 text-sm font-bold text-[#f5f5f5]">
                      <HardDrive className="h-4 w-4 text-[#06b6d4]" />
                      <span>4. Client Wire vs Provider Metered Overhead</span>
                    </div>
                    <p className="text-xs text-[#a3a3a3] leading-relaxed">
                      Proxy users often observe that account bandwidth drops faster than raw payload downloads:
                    </p>
                    <ul className="text-xs text-[#8c8c8c] space-y-1.5 list-disc pl-4">
                      <li>
                        <strong className="text-[#ededed]">CONNECT Tunneling:</strong> Every proxy session starts with an uncompressed HTTP CONNECT handshake.
                      </li>
                      <li>
                        <strong className="text-[#ededed]">TLS Negotiation:</strong> Client certificates, ALPN, and TLS session handshakes are metered through the tunnel.
                      </li>
                      <li>
                        In this benchmark run, metered usage was{' '}
                        <span className="font-mono text-[#38bdf8]">
                          {stats.overheadPct !== null ? `+${stats.overheadPct.toFixed(1)}%` : 'elevated'}
                        </span>{' '}
                        above raw scraper HTTP wire transfer.
                      </li>
                    </ul>
                  </Card>
                </div>
              )}

              {/* Tab 3: Variance & Rank Drift */}
              {activeTab === 'variance' && (
                <div className="rounded-xl border border-[#242424] bg-[#141414] p-5 space-y-4">
                  <div>
                    <h3 className="text-sm font-bold text-[#f5f5f5]">
                      Search Result Ranking Variance (Same-IP vs New-IP)
                    </h3>
                    <p className="text-xs text-[#8a8a8a] mt-0.5">
                      Empirical evidence: Google Maps search results drift when rotated to a new residential IP. Sticky sessions preserve result consistency.
                    </p>
                  </div>

                  {varianceList.length > 0 ? (
                    <div className="overflow-x-auto rounded-lg border border-[#262626]">
                      <table className="w-full text-left text-sm">
                        <thead className="bg-[#181818] text-[11px] font-semibold uppercase tracking-wider text-[#8a8a8a]">
                          <tr>
                            <th className="px-4 py-2.5">Pair Comparison</th>
                            <th className="px-4 py-2.5 text-right">Sample Pairs (n)</th>
                            <th className="px-4 py-2.5 text-right">Jaccard @ 20</th>
                            <th className="px-4 py-2.5 text-right">Jaccard @ 40</th>
                            <th className="px-4 py-2.5 text-right">RBO (p=0.90)</th>
                            <th className="px-4 py-2.5 text-right">Kendall &tau;</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-[#222] font-mono text-xs">
                          {varianceList.map((item) => (
                            <tr key={item.type} className="hover:bg-[#1a1a1a]/50">
                              <td className="px-4 py-3 font-sans font-medium text-[#ededed]">
                                <span className="font-mono text-[#6366f1]">{item.type}</span> —{' '}
                                {item.label}
                              </td>
                              <td className="px-4 py-3 text-right text-[#a3a3a3]">
                                {item.data.n_pairs}
                              </td>
                              <td className="px-4 py-3 text-right font-bold text-[#10b981]">
                                {formatPercent(item.data.jaccard_20_mean * 100, 1)}
                              </td>
                              <td className="px-4 py-3 text-right font-bold text-[#10b981]">
                                {formatPercent(item.data.jaccard_40_mean * 100, 1)}
                              </td>
                              <td className="px-4 py-3 text-right text-[#38bdf8]">
                                {item.data.rbo_mean ? item.data.rbo_mean.toFixed(3) : '—'}
                              </td>
                              <td className="px-4 py-3 text-right text-[#f59e0b]">
                                {item.data.kendall_tau_mean
                                  ? item.data.kendall_tau_mean.toFixed(3)
                                  : '—'}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <div className="rounded-lg border border-[#262626] bg-[#181818] p-4 text-xs text-[#8a8a8a] italic">
                      Variance metrics not computed for this run.
                    </div>
                  )}

                  <div className="rounded-lg bg-[#181818] border border-[#2b2b2b] p-3 text-xs text-[#a3a3a3] leading-relaxed">
                    These scores describe the comparable pairs for this run. They do not establish that the exit IP caused the differences.
                  </div>
                </div>
              )}
            </section>

            {/* Geo Jurisdictions & Outcome Distribution Grid */}
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              {/* Outcome Donut (1 Column) */}
              <div className="rounded-xl border border-[#242424] bg-[#141414] p-5 flex flex-col justify-between">
                <div>
                  <div className="flex items-center justify-between pb-3 border-b border-[#202020]">
                    <h3 className="text-sm font-bold text-[#f5f5f5]">
                      Response Code Breakdown
                    </h3>
                    <Badge variant="outline" className="font-mono text-[10px]">
                      {stats.totals.total_requests} reqs
                    </Badge>
                  </div>
                  <p className="text-xs text-[#8a8a8a] mt-2">
                    Transmission status classification across all queries in this benchmark.
                  </p>
                </div>

                <div className="relative flex h-52 w-full items-center justify-center my-2">
                  {mounted ? (
                    <ResponsiveContainer width="100%" height="100%">
                      <PieChart>
                        <Pie
                          data={outcomeData.length > 0 ? outcomeData : [{ name: 'None', value: 1, color: '#333' }]}
                          cx="50%"
                          cy="50%"
                          innerRadius={55}
                          outerRadius={80}
                          paddingAngle={outcomeData.length > 1 ? 4 : 0}
                          dataKey="value"
                          stroke="#141414"
                          strokeWidth={2}
                        >
                          {outcomeData.map((entry, idx) => (
                            <Cell key={`cell-${idx}`} fill={entry.color} />
                          ))}
                        </Pie>
                        <RechartsTooltip
                          contentStyle={{
                            background: '#1a1a1a',
                            border: '1px solid #333',
                            borderRadius: '8px',
                            fontSize: '12px',
                            color: '#ededed',
                          }}
                        />
                      </PieChart>
                    </ResponsiveContainer>
                  ) : (
                    <div className="h-44 w-44 rounded-full border-4 border-[#222] animate-pulse" />
                  )}
                </div>

                <div className="space-y-1.5 border-t border-[#202020] pt-3 text-xs">
                  {outcomeData.map((item) => (
                    <div key={item.name} className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <span
                          className="h-2 w-2 rounded-full"
                          style={{ backgroundColor: item.color }}
                        />
                        <span className="text-[#b3b3b3]">{item.name}</span>
                      </div>
                      <span className="font-mono font-medium text-[#ededed] tabular-nums">
                        {item.value} ({formatPercent((item.value / stats.reqTotal) * 100, 1)})
                      </span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Country Comparison Table & Cards (2 Columns) */}
              <div className="lg:col-span-2 rounded-xl border border-[#242424] bg-[#141414] p-5 space-y-4">
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 pb-3 border-b border-[#202020]">
                  <div>
                    <h3 className="text-sm font-bold text-[#f5f5f5]">
                      Geo Target Jurisdiction Telemetry
                    </h3>
                    <p className="text-xs text-[#8a8a8a] mt-0.5">
                      Evaluating residential IP latency, consent wall behavior, and soft blocks by country.
                    </p>
                  </div>
                  <Badge variant="indigo" className="font-mono text-[10px]">
                    {countryList.length} Targets
                  </Badge>
                </div>

                {/* Country Cards */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {countryList.map((c) => (
                    <div
                      key={c.code}
                      className="rounded-lg border border-[#262626] bg-[#181818] p-4 space-y-3"
                    >
                      <div className="flex items-start justify-between">
                        <div className="flex items-center gap-2.5">
                          <span className="text-2xl">{c.flag}</span>
                          <div>
                            <div className="font-bold text-sm text-[#ededed]">{c.name}</div>
                            <div className="font-mono text-[11px] text-[#737373]">
                              {c.code} • {c.raw.queries} queries • {c.raw.traffic_mb.toFixed(2)} MB
                            </div>
                          </div>
                        </div>

                        <div className="text-right">
                          <div className="text-[10px] uppercase font-semibold text-[#737373]">
                            Success
                          </div>
                          <div className="font-mono text-base font-bold text-[#10b981]">
                            {formatPercent(c.successRate, 1)}
                          </div>
                        </div>
                      </div>

                      <div className="grid grid-cols-2 gap-2 text-xs border-t border-[#222] pt-2 font-mono">
                        <div>
                          <span className="text-[10px] text-[#737373] uppercase font-sans">
                            Avg Latency
                          </span>
                          <div className="text-[#ededed] font-semibold">
                            {formatNumber(c.raw.avg_latency_ms)} ms
                          </div>
                        </div>
                        <div>
                          <span className="text-[10px] text-[#737373] uppercase font-sans">
                            Places Extracted
                          </span>
                          <div className="text-[#10b981] font-semibold">
                            {formatNumber(c.raw.places)}
                          </div>
                        </div>
                        <div>
                          <span className="text-[10px] text-[#737373] uppercase font-sans">
                            Consent Walls
                          </span>
                          <div
                            className={c.raw.consent > 0 ? 'text-[#f59e0b] font-semibold' : 'text-[#737373]'}
                          >
                            {c.raw.consent} ({formatPercent(c.consentRate, 1)})
                          </div>
                        </div>
                        <div>
                          <span className="text-[10px] text-[#737373] uppercase font-sans">
                            Soft Blocks / 403
                          </span>
                          <div
                            className={
                              c.softBlocks > 0 || c.b403 > 0
                                ? 'text-[#ef4444] font-semibold'
                                : 'text-[#737373]'
                            }
                          >
                            {c.softBlocks} soft / {c.b403 + c.b429} hard
                          </div>
                        </div>
                      </div>

                      <div className="flex items-center justify-between text-[11px] border-t border-[#222] pt-2">
                        <span className="text-[#8c8c8c]">IP Retention Stability:</span>
                        <span className="font-mono text-[#38bdf8]">
                          {c.matched}/{c.matched + c.changed + c.unknown} matched (
                          {formatPercent(c.continuityPct, 1)})
                        </span>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Country Comparison Latency Chart */}
                {countryList.length > 1 && mounted && (
                  <div className="rounded-lg border border-[#262626] bg-[#181818] p-4 space-y-2">
                    <div className="text-xs font-semibold uppercase tracking-wider text-[#8a8a8a]">
                      Average Response Latency (ms) by Country
                    </div>
                    <div style={{ height: 160 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart
                          data={countryList.map((c) => ({
                            name: `${c.code} - ${c.name}`,
                            latency: Math.round(c.raw.avg_latency_ms),
                            places: c.raw.places,
                          }))}
                          margin={{ top: 10, right: 10, left: -20, bottom: 0 }}
                        >
                          <XAxis
                            dataKey="name"
                            tick={{ fill: '#8a8a8a', fontSize: 11 }}
                          />
                          <YAxis
                            tick={{ fill: '#8a8a8a', fontSize: 11 }}
                            unit="ms"
                          />
                          <RechartsTooltip
                            contentStyle={{
                              background: '#1a1a1a',
                              border: '1px solid #333',
                              borderRadius: '8px',
                              fontSize: '12px',
                              color: '#ededed',
                            }}
                          />
                          <Bar
                            dataKey="latency"
                            name="Latency (ms)"
                            fill="#6366f1"
                            radius={[4, 4, 0, 0]}
                          />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </>
        )}

        {/* Empty State */}
        {!loadingManifest && !loadingRun && (!manifest || manifest.runs.length === 0) && (
          <div className="rounded-xl border border-[#242424] bg-[#141414] p-12 text-center text-[#8a8a8a] space-y-3">
            <Info className="h-8 w-8 text-[#6366f1] mx-auto" />
            <h3 className="text-sm font-semibold text-[#ededed]">No Benchmark Runs Recorded</h3>
            <p className="text-xs max-w-md mx-auto">
              The public runs manifest at{' '}
              <code className="text-[#38bdf8] font-mono">{MANIFEST_URL}</code> returned an empty list.
            </p>
          </div>
        )}

        {/* Cloudflare Pages Static Telemetry Footer */}
        <footer className="border-t border-[#1f1f1f] pt-6 pb-12 text-center text-xs text-[#666666] space-y-2">
          <div className="flex flex-wrap items-center justify-center gap-3 font-mono text-[11px]">
            <span>Cloudflare Pages Static Export</span>
            <span>•</span>
            <span>Client Fetch Revalidation (no-cache)</span>
            <span>•</span>
            <a
              href={MANIFEST_URL}
              target="_blank"
              rel="noreferrer"
              className="text-[#6366f1] hover:underline"
            >
              Public Runs Feed
            </a>
          </div>
          <p className="text-[11px] text-[#555]">
            Benchmark telemetry generated by Gmap-Bench test harness on AnyIP residential proxies.
          </p>
        </footer>
      </main>
    </div>
  );
}
