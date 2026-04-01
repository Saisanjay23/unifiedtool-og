/**
 * Dashboard — Real-time intelligence overview with live session detection,
 * gradient stat cards, glowing health rings, and visual timeline Quick Start.
 */

import { useQuery } from '@tanstack/react-query';
import { getHealth, getJobs, getAllSessionStatuses } from '../api/client';
import useStore from '../store';
import HealthRing from './HealthRing';
import PlatformIcon, { PLATFORM_COLORS } from './PlatformIcon';

const PLATFORMS = ['facebook', 'instagram', 'twitter', 'youtube', 'telegram'];

export default function Dashboard() {
    const setActivePlatform = useStore((s) => s.setActivePlatform);

    const { data: healthData } = useQuery({
        queryKey: ['health'],
        queryFn: async () => {
            const res = await getHealth();
            return res.data;
        },
        refetchInterval: 15000,
    });

    const { data: jobsData } = useQuery({
        queryKey: ['jobs'],
        queryFn: async () => {
            const res = await getJobs();
            return res.data;
        },
        refetchInterval: 5000,
    });

    // Real-time session status polling every 10s
    const { data: sessionData, isLoading: sessionsLoading } = useQuery({
        queryKey: ['all-session-statuses'],
        queryFn: getAllSessionStatuses,
        refetchInterval: 10000,
    });

    const platforms = healthData?.platforms || {};
    const jobs = jobsData?.jobs || [];
    const sessions = sessionData || {};
    const activeJobs = jobs.filter((j) => j.status === 'running');
    const completedJobs = jobs.filter((j) => j.status === 'completed');
    const failedJobs = jobs.filter((j) => j.status === 'failed' || j.status === 'error');
    const loggedInCount = Object.values(sessions).filter((s) => s.logged_in && !s.session_expired).length;
    const expiredCount = Object.values(sessions).filter((s) => s.session_expired).length;

    return (
        <div>
            <div style={{ marginBottom: 'var(--space-xl)' }}>
                <h1 style={{ fontSize: 26, fontWeight: 700, marginBottom: 4, letterSpacing: '-0.5px' }}>Dashboard</h1>
                <div style={{ color: 'var(--text-muted)', fontSize: 13 }}>
                    Unified Social Media Tool — Intelligence Overview
                </div>
            </div>

            {/* Gradient Stat Cards */}
            <div className="stat-cards-row">
                <div className="gradient-stat-card gradient-stat-card--cyan" style={{ animationDelay: '0s' }}>
                    <div className="gradient-stat-card__value">{activeJobs.length}</div>
                    <div className="gradient-stat-card__label">Active Jobs</div>
                    {activeJobs.length > 0 && (
                        <div className="stat-card__pulse-dot" />
                    )}
                </div>
                <div className="gradient-stat-card gradient-stat-card--emerald" style={{ animationDelay: '0.08s' }}>
                    <div className="gradient-stat-card__value">{completedJobs.length}</div>
                    <div className="gradient-stat-card__label">Completed</div>
                </div>
                <div className="gradient-stat-card gradient-stat-card--amber" style={{ animationDelay: '0.16s' }}>
                    <div className="gradient-stat-card__value">
                        {loggedInCount}<span style={{ fontSize: 16, opacity: 0.7 }}>/{PLATFORMS.length}</span>
                    </div>
                    <div className="gradient-stat-card__label">Sessions Active</div>
                    {expiredCount > 0 && (
                        <div className="stat-card__sub-badge stat-card__sub-badge--warning">
                            {expiredCount} expired
                        </div>
                    )}
                </div>
                <div className="gradient-stat-card gradient-stat-card--violet" style={{ animationDelay: '0.24s' }}>
                    <div className="gradient-stat-card__value">{jobs.length}</div>
                    <div className="gradient-stat-card__label">Total Jobs</div>
                    {failedJobs.length > 0 && (
                        <div className="stat-card__sub-badge stat-card__sub-badge--error">
                            {failedJobs.length} failed
                        </div>
                    )}
                </div>
            </div>

            {/* Platform Health — Glowing Rings with Session Status */}
            <div className="card" style={{ marginBottom: 'var(--space-lg)' }}>
                <div className="card-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div className="card-title" style={{ fontSize: 16, textTransform: 'uppercase', letterSpacing: '1px' }}>Platform Health</div>
                    {!sessionsLoading && (
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 11, color: 'var(--text-muted)' }}>
                            <span className="live-indicator" />
                            Live · {loggedInCount} active session{loggedInCount !== 1 ? 's' : ''}
                        </div>
                    )}
                </div>
                <div className="platform-health-grid">
                    {PLATFORMS.map((platform, i) => {
                        const health = platforms[platform] || {};
                        const session = sessions[platform] || {};
                        const platformColor = PLATFORM_COLORS[platform] || '#00d4ff';
                        return (
                            <div
                                key={platform}
                                className={`health-ring-container ${session.logged_in ? 'health-ring-container--active' : 'health-ring-container--inactive'}`}
                                onClick={() => setActivePlatform(platform)}
                                style={{
                                    '--ring-color': platformColor,
                                    animation: `fadeSlideUp 0.5s cubic-bezier(0.22, 1, 0.36, 1) ${i * 0.1}s both`,
                                }}
                            >
                                {/* Glow overlay */}
                                <div className="health-ring-glow" style={{
                                    background: `radial-gradient(circle at 50% 50%, ${platformColor}15, transparent 70%)`,
                                }} />

                                {/* Session status indicator dot */}
                                <div className={`session-status-dot ${session.session_expired ? 'session-status-dot--expired' : session.logged_in ? 'session-status-dot--online' : 'session-status-dot--offline'} ${session.login_in_progress ? 'session-status-dot--connecting' : ''}`}
                                    title={session.session_expired ? `Session Expired: ${session.expired_reason || 'server-side logout'}` : session.logged_in ? 'Session Active' : session.login_in_progress ? 'Login in progress...' : 'No active session'}
                                />

                                <div style={{ marginBottom: 8 }}>
                                    <PlatformIcon platform={platform} size={32} />
                                </div>
                                <HealthRing
                                    score={health.score ?? 1.0}
                                    status={health.status || 'healthy'}
                                    label={platform.charAt(0).toUpperCase() + platform.slice(1)}
                                    requestCount={health.request_count || 0}
                                    platformColor={platformColor}
                                    sessionStatus={session}
                                />

                                {/* Session detail row */}
                                <div className="session-detail-row">
                                    {session.login_in_progress ? (
                                        <span className="session-badge session-badge--progress">
                                            <span className="spinner-inline" /> Logging in...
                                        </span>
                                    ) : session.session_expired ? (
                                        <span className="session-badge session-badge--expired">
                                            ⚠ Expired
                                        </span>
                                    ) : session.logged_in ? (
                                        <span className="session-badge session-badge--active">
                                            ● Active
                                            {session.age_hours != null && (
                                                <span className="session-age">
                                                    {session.age_hours < 1 ? '< 1h' : session.age_hours < 24 ? `${Math.round(session.age_hours)}h` : `${Math.round(session.age_hours / 24)}d`}
                                                </span>
                                            )}
                                        </span>
                                    ) : (
                                        <span className="session-badge session-badge--inactive">
                                            ○ No Session
                                        </span>
                                    )}
                                </div>
                            </div>
                        );
                    })}
                </div>
            </div>

            {/* Recent Activity Feed — unified view of all jobs */}
            <div className="card" style={{ marginBottom: 'var(--space-lg)' }}>
                <div className="card-header">
                    <div className="card-title" style={{ fontSize: 16, textTransform: 'uppercase', letterSpacing: '1px' }}>Recent Activity</div>
                    <div style={{ display: 'flex', gap: 8 }}>
                        {activeJobs.length > 0 && <span className="badge badge-pending pulse">{activeJobs.length} running</span>}
                        {completedJobs.length > 0 && <span className="badge badge-approved">{completedJobs.length} done</span>}
                        {failedJobs.length > 0 && <span className="badge badge-rejected">{failedJobs.length} failed</span>}
                    </div>
                </div>

                {jobs.length > 0 ? (
                    <div className="activity-feed">
                        {[...jobs]
                            .sort((a, b) => {
                                // Running first, then by most recent
                                if (a.status === 'running' && b.status !== 'running') return -1;
                                if (b.status === 'running' && a.status !== 'running') return 1;
                                return (b.created_at || 0) - (a.created_at || 0);
                            })
                            .slice(0, 8)
                            .map((job) => {
                                const isRunning = job.status === 'running';
                                const isFailed = job.status === 'failed' || job.status === 'error';
                                const isCompleted = job.status === 'completed';
                                return (
                                    <div
                                        key={job.job_id}
                                        className={`activity-item ${isRunning ? 'activity-item--running' : isFailed ? 'activity-item--failed' : 'activity-item--done'}`}
                                    >
                                        <div className="activity-item__icon">
                                            <PlatformIcon platform={job.platform} size={20} />
                                        </div>
                                        <div className="activity-item__body">
                                            <div className="activity-item__title">
                                                <span style={{ textTransform: 'capitalize', fontWeight: 600 }}>{job.platform}</span>
                                                <span style={{ color: 'var(--text-muted)', margin: '0 6px' }}>·</span>
                                                <span style={{ textTransform: 'capitalize', fontSize: 12, color: 'var(--text-secondary)' }}>{job.mode}</span>
                                            </div>
                                            <div className="activity-item__meta">
                                                {job.client && <span>Client: {job.client}</span>}
                                                {job.count_found > 0 && <span>• {job.count_found} found</span>}
                                            </div>
                                        </div>
                                        <div className="activity-item__status">
                                            {isRunning ? (
                                                <span className="badge badge-pending pulse">⟳ Running</span>
                                            ) : isFailed ? (
                                                <span className="badge badge-rejected">✗ Failed</span>
                                            ) : isCompleted ? (
                                                <span className="badge badge-approved">✓ Done</span>
                                            ) : (
                                                <span className="badge">{job.status}</span>
                                            )}
                                        </div>
                                    </div>
                                );
                            })}
                    </div>
                ) : (
                    <div className="activity-empty">
                        <div style={{ fontSize: 32, marginBottom: 12, opacity: 0.7 }}>📋</div>
                        <div style={{ fontSize: 14, fontWeight: 500, marginBottom: 4 }}>No activity yet</div>
                        <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>Jobs will appear here as you run discovery and analysis tasks</div>
                    </div>
                )}
            </div>

            {/* Quick Start — Only shows when no jobs exist (new users) */}
            {jobs.length === 0 && (
                <div className="card">
                    <div className="card-header">
                        <div className="card-title" style={{ fontSize: 16, textTransform: 'uppercase', letterSpacing: '1px' }}>Quick Start</div>
                    </div>
                    <p style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 'var(--space-md)' }}>
                        Get started with your first intelligence gathering operation.
                    </p>
                    <div className="timeline-container">
                        <div className="timeline-step">
                            <div className="timeline-step__icon timeline-step__icon--cyan">👤</div>
                            <div className="timeline-step__title">1. Create a Client</div>
                            <div className="timeline-step__desc">Use the Clients panel in the sidebar to create a client for organizing results.</div>
                        </div>
                        <div className="timeline-step">
                            <div className="timeline-step__icon timeline-step__icon--violet">🔑</div>
                            <div className="timeline-step__title">2. Login to Platforms</div>
                            <div className="timeline-step__desc">Go to Sessions to log in to each platform using a real browser session.</div>
                        </div>
                        <div className="timeline-step">
                            <div className="timeline-step__icon timeline-step__icon--emerald">🔍</div>
                            <div className="timeline-step__title">3. Run Discovery</div>
                            <div className="timeline-step__desc">Select a platform, enter search keywords, and launch a discovery job.</div>
                        </div>
                        <div className="timeline-step">
                            <div className="timeline-step__icon timeline-step__icon--amber">📊</div>
                            <div className="timeline-step__title">4. Analyze & Export</div>
                            <div className="timeline-step__desc">Review results, approve/reject profiles, and export to Excel.</div>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
