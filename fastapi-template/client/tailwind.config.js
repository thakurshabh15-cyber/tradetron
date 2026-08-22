/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        surface: {
          50: "#f8fafc",
          100: "#f1f5f9",
          200: "#e2e8f0",
          700: "#1e2238",
          750: "#181b30",
          800: "#131627",
          850: "#0e1120",
          900: "#090b17",
          950: "#05060e",
        },
        brand: {
          red: "#f43f5e",
          coral: "#fb7185",
          crimson: "#e11d48",
          purple: "#8b5cf6",
          violet: "#7c3aed",
          indigo: "#6366f1",
          cyan: "#06b6d4",
          sky: "#38bdf8",
        },
        profit: {
          400: "#34d399",
          500: "#10b981",
          600: "#059669",
        },
        loss: {
          400: "#fb7185",
          500: "#f43f5e",
          600: "#e11d48",
        },
        warning: {
          400: "#fbbf24",
          500: "#f59e0b",
        },
      },
      fontFamily: {
        sans: ['"Inter"', "system-ui", "sans-serif"],
        display: ['"Outfit"', '"Inter"', "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "Consolas", "monospace"],
      },
      animation: {
        "fade-in": "fadeIn 0.25s ease-out",
        "slide-up": "slideUp 0.3s cubic-bezier(0.16, 1, 0.3, 1)",
        "slide-in-right": "slideInRight 0.3s cubic-bezier(0.16, 1, 0.3, 1)",
        "pulse-glow": "pulseGlow 2.5s ease-in-out infinite",
        "price-up": "priceUp 0.5s ease-out",
        "price-down": "priceDown 0.5s ease-out",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0", transform: "scale(0.98)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
        slideUp: {
          "0%": { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        slideInRight: {
          "0%": { opacity: "0", transform: "translateX(24px)" },
          "100%": { opacity: "1", transform: "translateX(0)" },
        },
        pulseGlow: {
          "0%, 100%": { opacity: "1", filter: "drop-shadow(0 0 8px rgba(139, 92, 246, 0.4))" },
          "50%": { opacity: "0.75", filter: "drop-shadow(0 0 2px rgba(139, 92, 246, 0.1))" },
        },
        priceUp: {
          "0%": { backgroundColor: "rgba(16, 185, 129, 0.3)" },
          "100%": { backgroundColor: "transparent" },
        },
        priceDown: {
          "0%": { backgroundColor: "rgba(244, 63, 94, 0.3)" },
          "100%": { backgroundColor: "transparent" },
        },
      },
      boxShadow: {
        "glass-sm": "0 2px 10px 0 rgba(0, 0, 0, 0.3)",
        "glass-md": "0 8px 30px 0 rgba(0, 0, 0, 0.4)",
        "glass-lg": "0 12px 40px 0 rgba(0, 0, 0, 0.5)",
        "glow-purple": "0 0 20px -3px rgba(139, 92, 246, 0.3)",
        "glow-red": "0 0 20px -3px rgba(244, 63, 94, 0.35)",
        "glow-cyan": "0 0 20px -3px rgba(6, 182, 212, 0.3)",
      },
    },
  },
  plugins: [],
};
