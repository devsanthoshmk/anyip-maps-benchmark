export interface RunIndexItem {
  id: string;
  title: string;
  path: string;
}

export interface RunIndexManifest {
  runs: RunIndexItem[];
}

export interface TotalsData {
  total_queries: number;
  successful_queries: number;
  total_places: number;
  total_requests: number;
  ok_requests: number;
  consent_requests: number;
  block_403: number;
  block_429: number;
  traffic_mb_wire: number;
}

export interface CountryStatData {
  queries: number;
  successful_queries: number;
  places: number;
  avg_latency_ms: number;
  consent: number;
  '403'?: number;
  '429'?: number;
  block_403?: number;
  block_429?: number;
  soft_block?: number;
  proxy_error?: number;
  ip_matched_queries?: number;
  ip_changed_queries?: number;
  ip_unknown_queries?: number;
  traffic_mb: number;
}

export interface PairVarianceItem {
  n_pairs: number;
  jaccard_20_mean: number;
  jaccard_40_mean: number;
  rbo_mean: number;
  kendall_tau_mean: number;
}

export interface VarianceData {
  by_pair_type?: Record<string, PairVarianceItem>;
  [key: string]: any;
}

export interface StickyContinuityData {
  query_ip_matched: number;
  query_ip_rotated: number;
  query_ip_unknown: number;
}

export interface RunSummary {
  run_id: string;
  elapsed_s: number;
  totals: TotalsData;
  by_country: Record<string, CountryStatData>;
  variance?: VarianceData;
  sticky_continuity?: StickyContinuityData;
  account_usage_delta_bytes?: number;
}
