import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'AnyIP Google Maps Benchmark',
  description: 'Empirical scraping results and session continuity measurements on Google Maps using AnyIP residential proxies.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-[#101114] text-[#eeeff2] antialiased">
        {children}
      </body>
    </html>
  );
}
