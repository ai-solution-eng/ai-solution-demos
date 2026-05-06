import type { Config } from 'tailwindcss';
import { colors } from 'tailwindcss/colors';

const config: Config = {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        inter: ["Inter", "sans-serif"],
      },
  },
  plugins: [],
  darkMode: "class",
}

export default config;