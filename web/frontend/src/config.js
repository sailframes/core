// API configuration
// In production, window.SAILFRAMES_API_URL is injected by deploy script
// In development, Vite proxy handles /api routes (empty string)
export const API_URL =
  (typeof window !== "undefined" && window.SAILFRAMES_API_URL) ||
  import.meta.env.VITE_API_URL ||
  "";
