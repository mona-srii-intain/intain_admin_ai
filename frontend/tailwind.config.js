/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        primary: {
          50:  "#e8f5f0",
          100: "#c5e6d9",
          200: "#9dd4bf",
          300: "#71c2a5",
          400: "#4db390",
          500: "#2ea37c",
          600: "#1B8C66",
          700: "#1B5E45",
          800: "#164d39",
          900: "#0e3527",
        },
        surface: "#F4F7F5",
        card: "#FFFFFF",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      boxShadow: {
        card: "0 1px 4px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.04)",
        "card-hover": "0 4px 12px rgba(0,0,0,0.12), 0 8px 32px rgba(0,0,0,0.06)",
      },
    },
  },
  plugins: [],
};
