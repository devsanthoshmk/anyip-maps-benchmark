import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

export function formatNumber(val: number | undefined | null, digits = 0): string {
  if (val === undefined || val === null || isNaN(val)) return '—';
  return val.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatPercent(val: number | undefined | null, digits = 1): string {
  if (val === undefined || val === null || isNaN(val)) return '—';
  return `${val.toFixed(digits)}%`;
}

export function formatBytesToMb(bytes: number | undefined | null, digits = 2): string {
  if (bytes === undefined || bytes === null || isNaN(bytes)) return '—';
  const mb = bytes / (1024 * 1024);
  return `${mb.toFixed(digits)} MB`;
}

export function formatDuration(seconds: number | undefined | null): string {
  if (seconds === undefined || seconds === null || isNaN(seconds)) return '—';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return `${mins}m ${secs}s`;
}
