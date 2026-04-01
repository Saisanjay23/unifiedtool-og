/**
 * API client for the Unified Social Media Tool backend.
 * Axios instance with base URL and WebSocket helper.
 */

import axios from 'axios';

const API_BASE = import.meta.env.VITE_API_URL || '';

const api = axios.create({
    baseURL: API_BASE,
    timeout: 30000,
    headers: { 'Content-Type': 'application/json' },
});

// normalize error responses
api.interceptors.response.use(
    (res) => res,
    (err) => {
        const message = err.response?.data?.detail || err.message || 'Request failed';
        return Promise.reject(new Error(message));
    }
);

// -- Jobs --
export const createJob = (payload) => api.post('/jobs', payload);
export const getJobs = () => api.get('/jobs');
export const getJobStatus = (jobId) => api.get(`/jobs/${jobId}`);
export const cancelJob = (jobId) => api.delete(`/jobs/${jobId}`);

// -- Results --
export const getResults = (client, params) => api.get(`/results/${client}`, { params });
export const updateResultStatus = (docId, platform, status) =>
    api.patch(`/results/${docId}`, { status, platform });
export const updateResultFields = (docId, platform, fields) =>
    api.patch(`/results/${docId}/fields`, { platform, fields });
export const exportResults = (client, params) =>
    api.get(`/results/${client}/export`, { params, responseType: 'blob' });
export const exportMemoryResults = (client, data) =>
    api.post(`/results/${client}/export-memory`, data, { responseType: 'blob' });
export const getKeywords = (client, platform) =>
    api.get(`/results/${client}/keywords`, { params: { platform } });
export const getKnownUrls = (client, platform) =>
    api.get(`/results/${client}/known-urls`, { params: { platform } });

// -- Health --
export const getHealth = () => api.get('/health');

// -- Clients --
export const getClients = () => api.get('/clients');
export const createClient = (name) => api.post('/clients', { name });
export const deleteClient = (name) => api.delete(`/clients/${name}`);

// -- Keyword Presets --
export const getPresets = (client, platform) =>
    api.get(`/presets/${client}/${platform}`);
export const savePreset = (client, platform, presetName, keywords) =>
    api.post(`/presets/${client}/${platform}`, { preset_name: presetName, keywords });
export const deletePreset = (client, platform, presetName) =>
    api.delete(`/presets/${client}/${platform}/${encodeURIComponent(presetName)}`);

// -- Sessions --
export const launchSession = (platform) => api.post(`/sessions/${platform}/launch`);
export const importCookies = (platform, cookies) => api.post(`/sessions/${platform}/cookies`, cookies);
export const getSessionStatus = (platform) => api.get(`/sessions/${platform}/status`);
export const clearSession = (platform) => api.delete(`/sessions/${platform}`);
export const getCredentials = (platform) => api.get(`/sessions/${platform}/credentials`);
export const saveCredentials = (platform, payload) => api.post(`/sessions/${platform}/credentials`, payload);

// -- Telegram Interactive Auth --
export const sendTelegramCode = (payload) => api.post('/sessions/telegram/auth/send_code', payload);
export const verifyTelegramCode = (payload) => api.post('/sessions/telegram/auth/verify_code', payload);
export const verifyTelegramPassword = (payload) => api.post('/sessions/telegram/auth/verify_password', payload);

export const getAllSessionStatuses = async () => {
    const platforms = ['facebook', 'instagram', 'twitter', 'youtube', 'telegram', 'tiktok'];
    const results = {};
    await Promise.allSettled(
        platforms.map(async (p) => {
            try {
                const res = await getSessionStatus(p);
                results[p] = res.data;
            } catch {
                results[p] = { platform: p, logged_in: false, login_in_progress: false };
            }
        })
    );
    return results;
};

// -- WebSocket with auto-reconnect --
export function connectJobWebSocket(jobId, onEvent, { onReconnect, maxRetries = 10 } = {}) {
    const wsBase = API_BASE.replace(/^http/, 'ws') || `ws://${window.location.host}`;
    let retries = 0;
    let ws = null;
    let closed = false;

    function connect() {
        ws = new WebSocket(`${wsBase}/ws/jobs/${jobId}`);

        ws.onopen = () => {
            retries = 0; // reset on successful connection
            if (onReconnect && retries > 0) onReconnect(true);
        };

        ws.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data);
                onEvent(data);
            } catch (err) {
                console.error('WebSocket parse error:', err);
            }
        };

        ws.onerror = (e) => console.error('WebSocket error:', e);

        ws.onclose = () => {
            if (closed) return; // intentionally closed
            if (retries < maxRetries) {
                const delay = Math.min(1000 * Math.pow(2, retries), 30000);
                retries++;
                console.log(`WebSocket reconnecting in ${delay}ms (attempt ${retries}/${maxRetries})...`);
                if (onReconnect) onReconnect(false);
                setTimeout(connect, delay);
            } else {
                console.warn(`WebSocket gave up after ${maxRetries} retries`);
            }
        };
    }

    connect();

    // Return control object instead of raw ws
    return {
        close: () => { closed = true; ws?.close(); },
        get ws() { return ws; },
    };
}

export default api;
