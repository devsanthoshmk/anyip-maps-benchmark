import * as React from 'react';
import { cn } from '@/lib/utils';

export interface BadgeProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: 'default' | 'success' | 'warning' | 'destructive' | 'secondary' | 'outline' | 'indigo' | 'cyan';
}

export function Badge({
  className,
  variant = 'default',
  ...props
}: BadgeProps) {
  const variantStyles = {
    default: 'border-[#262626] bg-[#1a1a1a] text-[#ededed]',
    success: 'border-[#10b981]/30 bg-[#10b981]/10 text-[#34d399]',
    warning: 'border-[#f59e0b]/30 bg-[#f59e0b]/10 text-[#fbbf24]',
    destructive: 'border-[#ef4444]/30 bg-[#ef4444]/10 text-[#f87171]',
    secondary: 'border-[#262626] bg-[#171717] text-[#a3a3a3]',
    outline: 'border-[#2e2e2e] text-[#d4d4d4] bg-transparent',
    indigo: 'border-[#6366f1]/30 bg-[#6366f1]/10 text-[#818cf8]',
    cyan: 'border-[#06b6d4]/30 bg-[#06b6d4]/10 text-[#38bdf8]',
  };

  return (
    <div
      className={cn(
        'inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[11px] font-medium tracking-wide uppercase transition-colors',
        variantStyles[variant] || variantStyles.default,
        className
      )}
      {...props}
    />
  );
}
