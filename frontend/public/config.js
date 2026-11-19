// Runtime configuration, served as a static file.
//
// Vite inlines import.meta.env values at build time, so the API URL baked
// into the bundle cannot be changed by editing an environment variable at
// run time. This file is copied verbatim into dist/ and is read by
// src/config.ts before the app boots.
//
// The container's entrypoint rewrites this file from the API_BASE_URL
// environment variable, so one image works against any backend address.
window.__API_BASE_URL__ = window.__API_BASE_URL__ || 'http://localhost:8001'
