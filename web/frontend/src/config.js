// API configuration
// In production, uses the API Gateway URL
// In development, Vite proxy handles /api routes
export const API_URL = import.meta.env.VITE_API_URL || "";
