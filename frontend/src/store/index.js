/**
 * Global State Mutator & Pub-Sub Broker via Zustand.
 * Elevates volatile cross-component state (like active job tracking and global client filters)
 * out of the React rendering reconciliation cycle to prevent spurious prop-drilling and unnecessary re-renders.
 */

import { create } from 'zustand';

// Read persisted theme or default to 'dark'
const getInitialTheme = () => {
    try {
        return localStorage.getItem('usmt-theme') || 'dark';
    } catch {
        return 'dark';
    }
};

const useStore = create((set) => ({
    // -- Theme --
    theme: getInitialTheme(),
    toggleTheme: () =>
        set((state) => {
            const next = state.theme === 'dark' ? 'light' : 'dark';
            try { localStorage.setItem('usmt-theme', next); } catch {}
            return { theme: next };
        }),

    // -- Platform state --
    activePlatform: 'facebook',
    activeMode: 'discovery',
    setActivePlatform: (platform) => set({ activePlatform: platform }),
    setActiveMode: (mode) => set({ activeMode: mode }),

    // -- Active jobs --
    activeJobs: {},
    addJob: (jobId, jobData) =>
        set((state) => ({
            activeJobs: { ...state.activeJobs, [jobId]: jobData },
        })),
    updateJob: (jobId, updates) =>
        set((state) => ({
            activeJobs: {
                ...state.activeJobs,
                [jobId]: { ...state.activeJobs[jobId], ...updates },
            },
        })),
    removeJob: (jobId) =>
        set((state) => {
            const copy = { ...state.activeJobs };
            delete copy[jobId];
            return { activeJobs: copy };
        }),

    // -- Health data --
    healthData: {},
    setHealthData: (data) => set({ healthData: data }),

    // -- Client selection --
    selectedClient: null,
    setSelectedClient: (client) => set({ selectedClient: client }),

    // -- Results filters --
    filters: {
        status: null,
        dateRange: null,
        aiScoreMin: null,
    },
    setFilters: (filters) =>
        set((state) => ({
            filters: { ...state.filters, ...filters },
        })),
}));

export default useStore;
