/**
 * App — Root component with Stitch-inspired layout:
 * Glassmorphism sidebar + top header bar + main content area.
 */

import { useState, useEffect, useRef } from 'react';
import { QueryClient, QueryClientProvider, useQuery, useQueryClient as useQC } from '@tanstack/react-query';
import useStore from './store';
import { getClients, createClient } from './api/client';
import Dashboard from './components/Dashboard';
import PlatformView from './components/PlatformView';
import SessionLogin from './components/SessionLogin';
import ClientManager from './components/ClientManager';
import CronManager from './components/CronManager';
import PlatformIcon from './components/PlatformIcon';
import { ToastProvider } from './components/Toast';

const queryClient = new QueryClient({
    defaultOptions: {
        queries: {
            staleTime: 5000,
            retry: 1,
        },
    },
});

const PLATFORMS = [
    { id: 'facebook', label: 'Facebook' },
    { id: 'instagram', label: 'Instagram' },
    { id: 'twitter', label: 'Twitter' },
    { id: 'youtube', label: 'YouTube' },
    { id: 'telegram', label: 'Telegram' },
    { id: 'tiktok', label: 'TikTok' },
];

const PAGES = {
    dashboard: 'dashboard',
    platform: 'platform',
    sessions: 'sessions',
    cron: 'cron',
};

const PAGE_TITLES = {
    dashboard: { title: 'Dashboard', subtitle: 'Intelligence overview and metrics', icon: '📊' },
    sessions: { title: 'Sessions', subtitle: 'Browser session management', icon: '🔑' },
    cron: { title: 'Scheduler', subtitle: 'Automated cron job management', icon: '⏰' },
};

/* ─── Header Client Dropdown ─── */
function HeaderClientDropdown() {
    const qc = useQC();
    const { selectedClient, setSelectedClient } = useStore();
    const [open, setOpen] = useState(false);
    const [search, setSearch] = useState('');
    const [newName, setNewName] = useState('');
    const [creating, setCreating] = useState(false);
    const ref = useRef(null);

    const { data: clients = [] } = useQuery({
        queryKey: ['clients'],
        queryFn: async () => {
            const res = await getClients();
            return res.data.clients || [];
        },
        refetchInterval: 30000,
    });

    // Close dropdown on outside click
    useEffect(() => {
        const handler = (e) => {
            if (ref.current && !ref.current.contains(e.target)) setOpen(false);
        };
        if (open) document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [open]);

    const filtered = clients.filter((c) =>
        c.toLowerCase().includes(search.toLowerCase())
    );

    const handleCreate = async () => {
        const name = newName.trim();
        if (!name) return;
        setCreating(true);
        try {
            await createClient(name);
            qc.invalidateQueries({ queryKey: ['clients'] });
            setSelectedClient(name);
            setNewName('');
        } catch (err) {
            console.error('Failed to create client:', err);
        } finally {
            setCreating(false);
        }
    };

    return (
        <div className="header-client-dropdown" ref={ref}>
            <div
                className={`top-header__user ${open ? 'top-header__user--active' : ''}`}
                onClick={() => setOpen(!open)}
            >
                <div className="top-header__avatar">
                    {selectedClient ? selectedClient.charAt(0).toUpperCase() : 'U'}
                </div>
                <span className="top-header__name">{selectedClient || 'No Client'}</span>
                <span style={{ fontSize: 10, color: 'var(--text-muted)', transition: 'transform 0.2s', transform: open ? 'rotate(180deg)' : 'rotate(0)' }}>▼</span>
            </div>

            {open && (
                <div className="client-dropdown-menu">
                    <div className="client-dropdown-menu__header">
                        <span className="client-dropdown-menu__title">Switch Client</span>
                    </div>

                    {/* Search */}
                    <div className="client-dropdown-menu__search">
                        <input
                            type="text"
                            placeholder="Search clients..."
                            value={search}
                            onChange={(e) => setSearch(e.target.value)}
                            autoFocus
                        />
                    </div>

                    {/* Client list */}
                    <div className="client-dropdown-menu__list">
                        {filtered.length === 0 && (
                            <div className="client-dropdown-menu__empty">
                                {clients.length === 0 ? 'No clients yet' : 'No matches'}
                            </div>
                        )}
                        {filtered.map((c) => (
                            <button
                                key={c}
                                className={`client-dropdown-menu__item ${c === selectedClient ? 'client-dropdown-menu__item--active' : ''}`}
                                onClick={() => {
                                    setSelectedClient(c);
                                    setOpen(false);
                                    setSearch('');
                                }}
                            >
                                <span className="client-dropdown-menu__item-avatar">
                                    {c.charAt(0).toUpperCase()}
                                </span>
                                <span className="client-dropdown-menu__item-name">{c}</span>
                                {c === selectedClient && <span className="client-dropdown-menu__check">✓</span>}
                            </button>
                        ))}
                    </div>

                    {/* Quick-create */}
                    <div className="client-dropdown-menu__create">
                        <input
                            type="text"
                            placeholder="New client name..."
                            value={newName}
                            onChange={(e) => setNewName(e.target.value)}
                            onKeyDown={(e) => { if (e.key === 'Enter') handleCreate(); }}
                        />
                        <button
                            disabled={!newName.trim() || creating}
                            onClick={handleCreate}
                        >
                            {creating ? '...' : '+ Add'}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}

export default function App() {
    const [page, setPage] = useState(PAGES.dashboard);
    const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
    const activePlatform = useStore((s) => s.activePlatform);
    const setActivePlatform = useStore((s) => s.setActivePlatform);
    const selectedClient = useStore((s) => s.selectedClient);
    const theme = useStore((s) => s.theme);
    const toggleTheme = useStore((s) => s.toggleTheme);

    const activeJobs = useStore((s) => s.activeJobs);
    const activeJobCount = Object.keys(activeJobs).length;

    const [visitedPages, setVisitedPages] = useState(new Set([PAGES.dashboard, `${PAGES.platform}_${activePlatform}`]));

    useEffect(() => {
        setVisitedPages((prev) => {
            const next = new Set(prev);
            if (page === PAGES.platform) next.add(`${PAGES.platform}_${activePlatform}`);
            else next.add(page);
            return next;
        });
    }, [page, activePlatform]);

    useEffect(() => {
        document.documentElement.setAttribute('data-theme', theme);
    }, [theme]);

    const navigateToPlatform = (platformId) => {
        setActivePlatform(platformId);
        setPage(PAGES.platform);
    };

    // Get current page title info
    const getPageInfo = () => {
        if (page === PAGES.platform) {
            const p = PLATFORMS.find(p => p.id === activePlatform);
            return {
                title: p?.label || 'Platform',
                subtitle: 'Keyword social profile discovery',
                icon: null,
                platformId: activePlatform,
            };
        }
        return PAGE_TITLES[page] || PAGE_TITLES.dashboard;
    };

    const pageInfo = getPageInfo();

    return (
        <QueryClientProvider client={queryClient}>
            <ToastProvider>
            <div className={`app-container ${isSidebarCollapsed ? 'sidebar-collapsed' : ''}`}>
                {/* Sidebar */}
                <nav className={`app-sidebar ${isSidebarCollapsed ? 'app-sidebar--collapsed' : ''}`}>
                    <div className="sidebar-header">
                        <div className="logo">
                            UNIFIED<br />
                            <span>Social Media Tool v3</span>
                        </div>
                        <button
                            className="sidebar-toggle-btn"
                            onClick={() => setIsSidebarCollapsed(!isSidebarCollapsed)}
                            title={isSidebarCollapsed ? "Expand Sidebar" : "Collapse Sidebar"}
                        >
                            {isSidebarCollapsed ? '☰' : '⇦'}
                        </button>
                    </div>

                    {/* Theme Toggle */}
                    {!isSidebarCollapsed ? (
                        <button className="theme-toggle-btn" onClick={toggleTheme} title="Toggle Dark/Light Mode">
                            {theme === 'dark' ? '☀️' : '🌙'} {theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
                        </button>
                    ) : (
                        <button className="theme-toggle-btn" onClick={toggleTheme} title="Toggle Theme" style={{ fontSize: 18, padding: '6px' }}>
                            {theme === 'dark' ? '☀️' : '🌙'}
                        </button>
                    )}

                    {/* Main nav */}
                    <button
                        className={`nav-item ${page === PAGES.dashboard ? 'active' : ''}`}
                        onClick={() => setPage(PAGES.dashboard)}
                        title="Dashboard"
                    >
                        <span className="icon">📊</span> <span className="nav-text">Dashboard</span>
                    </button>

                    <div className="nav-section-title">Platforms</div>
                    {PLATFORMS.map((p) => (
                        <button
                            key={p.id}
                            className={`nav-item ${page === PAGES.platform && activePlatform === p.id ? 'active' : ''}`}
                            onClick={() => navigateToPlatform(p.id)}
                            title={p.label}
                        >
                            <span className="icon" style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}>
                                <PlatformIcon platform={p.id} size={18} />
                            </span>
                            <span className="nav-text">{p.label}</span>
                        </button>
                    ))}

                    <div className="nav-section-title">Settings</div>
                    <button
                        className={`nav-item ${page === PAGES.sessions ? 'active' : ''}`}
                        onClick={() => setPage(PAGES.sessions)}
                        title="Sessions"
                    >
                        <span className="icon">🔑</span> <span className="nav-text">Sessions</span>
                    </button>
                    <button
                        className={`nav-item ${page === PAGES.cron ? 'active' : ''}`}
                        onClick={() => setPage(PAGES.cron)}
                        title="Scheduler"
                    >
                        <span className="icon">⏰</span> <span className="nav-text">Scheduler</span>
                    </button>

                    {/* Client manager in sidebar */}
                    <div className="sidebar-client-manager" style={{ marginTop: 'auto', paddingTop: 'var(--space-lg)' }}>
                        {!isSidebarCollapsed && <ClientManager />}
                        {isSidebarCollapsed && (
                            <div title="Client Manager (Expand to view)" style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: 18, padding: '8px 0' }}>
                                👤
                            </div>
                        )}
                    </div>

                    {/* Watermark */}
                    <div className="sidebar-watermark">
                        {!isSidebarCollapsed ? (
                            <>
                                <span className="sidebar-watermark__label">Built by</span>
                                <span className="sidebar-watermark__name">Sai Sanjay</span>
                            </>
                        ) : (
                            <span className="sidebar-watermark__icon" title="Built by Sai Sanjay">SS</span>
                        )}
                    </div>
                </nav>

                {/* Main content with top header */}
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                    {/* Top Header Bar */}
                    <header className="top-header">
                        <div className="top-header__left">
                            {pageInfo.platformId ? (
                                <div className="top-header__platform-title">
                                    <PlatformIcon platform={pageInfo.platformId} size={28} />
                                    <div>
                                        <div>{pageInfo.title}</div>
                                        <div className="top-header__subtitle">{pageInfo.subtitle}</div>
                                    </div>
                                </div>
                            ) : (
                                <div className="top-header__platform-title">
                                    <span style={{ fontSize: 24 }}>{pageInfo.icon}</span>
                                    <div>
                                        <div>{pageInfo.title}</div>
                                        <div className="top-header__subtitle">{pageInfo.subtitle}</div>
                                    </div>
                                </div>
                            )}
                        </div>
                        <div className="top-header__right">
                            <div
                                className="top-header__bell"
                                title={`${activeJobCount} active jobs — Click to view Dashboard`}
                                onClick={() => setPage(PAGES.dashboard)}
                                style={{ position: 'relative' }}
                            >
                                🔔
                                {activeJobCount > 0 && (
                                    <span style={{
                                        position: 'absolute', top: -4, right: -4,
                                        background: 'var(--accent-crimson)',
                                        color: '#fff', fontSize: 9, fontWeight: 700,
                                        width: 16, height: 16, borderRadius: '50%',
                                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                                        animation: 'pulse-glow 2s infinite',
                                    }}>
                                        {activeJobCount}
                                    </span>
                                )}
                            </div>
                            <HeaderClientDropdown />
                        </div>
                    </header>

                    {/* Main scrollable content */}
                    <main className="app-main">
                        {visitedPages.has(PAGES.dashboard) && (
                            <div style={{ display: page === PAGES.dashboard ? 'block' : 'none', height: '100%' }}>
                                <Dashboard />
                            </div>
                        )}

                        {PLATFORMS.map((p) => visitedPages.has(`${PAGES.platform}_${p.id}`) && (
                            <div key={p.id} style={{ display: page === PAGES.platform && activePlatform === p.id ? 'block' : 'none', height: '100%' }}>
                                <PlatformView platformId={p.id} />
                            </div>
                        ))}

                        {visitedPages.has(PAGES.sessions) && (
                            <div style={{ display: page === PAGES.sessions ? 'block' : 'none', height: '100%' }}>
                                <SessionLogin />
                            </div>
                        )}
                        
                        {visitedPages.has(PAGES.cron) && (
                            <div style={{ display: page === PAGES.cron ? 'block' : 'none', height: '100%' }}>
                                <CronManager />
                            </div>
                        )}
                    </main>
                </div>
            </div>
        </ToastProvider>
        </QueryClientProvider>
    );
}
