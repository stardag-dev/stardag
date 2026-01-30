// API configuration
// In production, this points to the API domain. In development, uses relative URLs.
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || window.location.origin;

export const API_V1 = `${API_BASE_URL}/api/v1`;
export const API_V1_UI = `${API_BASE_URL}/api/v1/ui`;
