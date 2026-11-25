import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'AnyIP Benchmark | Google Maps Residential Telemetry',
  description: 'Precision scraping resilience, sticky session telemetry, and consent wall detection on AnyIP proxies.',
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-[#0c0c0c] text-[#ededed] antialiased selection:bg-[#6366f1]/30 selection:text-white">
        {children}
      </body>
    </html>
  );
}
