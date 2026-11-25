import type { Config } from 'tailwindcss';

const config: Config = {
  darkMode: ['class'],
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        background: '#0f172a',
        foreground: '#f1f5f9',
        card: {
          DEFAULT: '#1e293b',
          elevated: '#263348',
          foreground: '#f1f5f9',
        },
        border: '#334155',
        muted: {
          DEFAULT: '#334155',
          foreground: '#94a3b8',
        },
        accent: {
          green: '#22c55e',
          amber: '#f59e0b',
          red: '#ef4444',
          orange: '#f97316',
          blue: '#3b82f6',
        },
      },
      borderRadius: {
        xl: '0.75rem',
      },
    },
  },
  plugins: [],
};

export default config;
