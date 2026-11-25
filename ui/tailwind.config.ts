import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        base: '#101114',
        surface: '#17181c',
        border: '#272930',
        muted: '#8a8f9c',
        primary: '#3b82f6',
      },
    },
  },
  plugins: [],
};

export default config;
