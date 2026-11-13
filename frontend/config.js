// Where the API lives. Empty means this page is served by the API itself
// (locally, or straight from the server). The Vercel copy calls the server.
window.API_ORIGIN = location.hostname.endsWith(".vercel.app") ? "https://3-7-91-183.sslip.io" : "";
