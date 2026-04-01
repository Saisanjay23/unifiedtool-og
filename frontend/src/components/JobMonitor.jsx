/**
 * JobMonitor — live job progress panel connected via WebSocket.
 * Shows real-time progress bar, ETA, speed metrics, elapsed time,
 * event log, found count, and cancel button.
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { connectJobWebSocket, cancelJob } from '../api/client';
import useStore from '../store';

function formatDuration(seconds) {
    if (!seconds || seconds < 0) return '--:--';
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    if (hrs > 0) return `${hrs}h ${mins}m ${secs}s`;
    if (mins > 0) return `${mins}m ${secs}s`;
    return `${secs}s`;
}

export default function JobMonitor({ jobId, onClose, onResultFound }) {
    const [events, setEvents] = useState([]);
    const [status, setStatus] = useState('running');
    const [progress, setProgress] = useState(0);
    const [foundCount, setFoundCount] = useState(0);
    const [totalTarget, setTotalTarget] = useState(0);
    const [elapsed, setElapsed] = useState(0);
    const [eta, setEta] = useState(null);
    const [speed, setSpeed] = useState(0);
    const logRef = useRef(null);
    const wsRef = useRef(null);
    const startTimeRef = useRef(Date.now());
    const timerRef = useRef(null);
    const updateJob = useStore((s) => s.updateJob);

    // Reset all state when jobId changes (fixes stuck progress on second run)
    useEffect(() => {
        setEvents([]);
        setStatus('running');
        setProgress(0);
        setFoundCount(0);
        setTotalTarget(0);
        setElapsed(0);
        setEta(null);
        setSpeed(0);
        startTimeRef.current = Date.now();

        // Restart the elapsed timer
        if (timerRef.current) clearInterval(timerRef.current);
        timerRef.current = setInterval(() => {
            const elapsedSec = (Date.now() - startTimeRef.current) / 1000;
            setElapsed(elapsedSec);
        }, 1000);

        return () => clearInterval(timerRef.current);
    }, [jobId]);

    // Recalculate speed + ETA whenever foundCount or elapsed changes
    useEffect(() => {
        if (elapsed > 2 && foundCount > 0) {
            const profilesPerSec = foundCount / elapsed;
            setSpeed(profilesPerSec * 60); // profiles per minute

            if (totalTarget > 0 && foundCount < totalTarget) {
                const remaining = totalTarget - foundCount;
                setEta(remaining / profilesPerSec);
            } else {
                setEta(null);
            }
        }
    }, [foundCount, elapsed, totalTarget]);

    // Stop timer when job finishes
    useEffect(() => {
        if (['completed', 'failed', 'cancelled'].includes(status)) {
            clearInterval(timerRef.current);
        }
    }, [status]);

    useEffect(() => {
        if (!jobId) return;

        const conn = connectJobWebSocket(jobId, (event) => {
            setEvents((prev) => [...prev.slice(-100), event]);

            if (event.count_found !== undefined) {
                setFoundCount(event.count_found);
            }

            if (event.count_total !== undefined && event.count_total > 0) {
                setTotalTarget(event.count_total);
            }

            if (event.event_type === 'result_found' && event.result && onResultFound) {
                onResultFound(event.result);
            }

            if (event.event_type === 'progress' && event.count_total) {
                setProgress(event.count_found / event.count_total);
            }

            if (event.event_type === 'completed' || event.event_type === 'failed') {
                setStatus(event.event_type);
                setProgress(event.event_type === 'completed' ? 1 : progress);
                updateJob(jobId, { status: event.event_type });
            }
        });

        wsRef.current = conn;

        return () => {
            conn.close();
        };
    }, [jobId]);

    // auto-scroll event log
    useEffect(() => {
        if (logRef.current) {
            logRef.current.scrollTop = logRef.current.scrollHeight;
        }
    }, [events]);

    const handleCancel = async () => {
        try {
            await cancelJob(jobId);
            setStatus('cancelled');
            updateJob(jobId, { status: 'cancelled' });
        } catch (err) {
            console.error('Failed to cancel job:', err);
        }
    };

    const isFinished = ['completed', 'failed', 'cancelled'].includes(status);
    const progressPct = Math.round(progress * 100);

    const statusColor = {
        running: 'var(--accent-cyan)',
        completed: 'var(--accent-emerald)',
        failed: 'var(--accent-crimson)',
        cancelled: 'var(--accent-amber)',
    };

    return (
        <div className="card slide-up" style={{ marginBottom: 'var(--space-md)' }}>
            {/* Header */}
            <div className="card-header">
                <div className="card-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{
                        display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
                        background: statusColor[status] || 'var(--accent-cyan)',
                        animation: status === 'running' ? 'pulse-glow 1.5s ease-in-out infinite' : 'none',
                    }} />
                    {status === 'running' ? 'Scraping in Progress...' : status === 'completed' ? 'Scrape Complete' : status.charAt(0).toUpperCase() + status.slice(1)}
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                    {!isFinished && (
                        <button className="btn btn-danger btn-sm" onClick={handleCancel}>
                            ⏹ Cancel
                        </button>
                    )}
                    {isFinished && onClose && (
                        <button className="btn btn-secondary btn-sm" onClick={onClose}>
                            Dismiss
                        </button>
                    )}
                </div>
            </div>

            {/* Enhanced Progress Bar */}
            <div style={{ marginBottom: 'var(--space-sm)' }}>
                <div style={{
                    width: '100%', height: 10, background: 'var(--bg-primary)',
                    borderRadius: 'var(--radius-full)', overflow: 'hidden',
                    border: '1px solid var(--border-subtle)',
                }}>
                    <div style={{
                        height: '100%',
                        width: `${progressPct}%`,
                        background: isFinished
                            ? (status === 'completed' ? 'var(--accent-emerald)' : 'var(--accent-crimson)')
                            : 'linear-gradient(90deg, var(--accent-cyan), var(--accent-emerald))',
                        borderRadius: 'var(--radius-full)',
                        transition: 'width 0.4s cubic-bezier(0.4, 0, 0.2, 1)',
                        boxShadow: status === 'running' ? '0 0 8px rgba(0, 212, 255, 0.4)' : 'none',
                    }} />
                </div>
            </div>

            {/* Stats Row */}
            <div style={{
                display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--space-sm)',
                marginBottom: 'var(--space-md)',
                fontFamily: 'var(--font-mono)', fontSize: 12,
            }}>
                {/* Progress % */}
                <div style={{
                    background: 'var(--bg-primary)', borderRadius: 'var(--radius-sm)',
                    padding: '8px 12px', textAlign: 'center',
                    border: '1px solid var(--border-subtle)',
                }}>
                    <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: 2 }}>
                        Progress
                    </div>
                    <div style={{ color: 'var(--accent-cyan)', fontWeight: 700, fontSize: 16 }}>
                        {progressPct}%
                    </div>
                </div>

                {/* Found Count */}
                <div style={{
                    background: 'var(--bg-primary)', borderRadius: 'var(--radius-sm)',
                    padding: '8px 12px', textAlign: 'center',
                    border: '1px solid var(--border-subtle)',
                }}>
                    <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: 2 }}>
                        Found
                    </div>
                    <div style={{ color: 'var(--accent-emerald)', fontWeight: 700, fontSize: 16 }}>
                        {foundCount}{totalTarget > 0 ? `/${totalTarget}` : ''}
                    </div>
                </div>

                {/* Elapsed */}
                <div style={{
                    background: 'var(--bg-primary)', borderRadius: 'var(--radius-sm)',
                    padding: '8px 12px', textAlign: 'center',
                    border: '1px solid var(--border-subtle)',
                }}>
                    <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: 2 }}>
                        Elapsed
                    </div>
                    <div style={{ color: 'var(--text-secondary)', fontWeight: 700, fontSize: 16 }}>
                        {formatDuration(elapsed)}
                    </div>
                </div>

                {/* ETA */}
                <div style={{
                    background: 'var(--bg-primary)', borderRadius: 'var(--radius-sm)',
                    padding: '8px 12px', textAlign: 'center',
                    border: '1px solid var(--border-subtle)',
                }}>
                    <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: 2 }}>
                        {isFinished ? 'Total Time' : 'ETA'}
                    </div>
                    <div style={{ color: 'var(--accent-amber)', fontWeight: 700, fontSize: 16 }}>
                        {isFinished ? formatDuration(elapsed) : (eta ? formatDuration(eta) : '--:--')}
                    </div>
                </div>
            </div>

            {/* Speed indicator */}
            {speed > 0 && (
                <div style={{
                    fontSize: 11, color: 'var(--text-muted)', marginBottom: 'var(--space-sm)',
                    fontFamily: 'var(--font-mono)', display: 'flex', gap: 'var(--space-md)',
                }}>
                    <span>⚡ {speed.toFixed(1)} profiles/min</span>
                    {foundCount > 0 && elapsed > 0 && (
                        <span>📊 Avg: {(elapsed / foundCount).toFixed(1)}s per profile</span>
                    )}
                </div>
            )}

            {/* Event log */}
            <div
                ref={logRef}
                style={{
                    maxHeight: 160,
                    overflowY: 'auto',
                    background: 'var(--bg-primary)',
                    borderRadius: 'var(--radius-sm)',
                    padding: 'var(--space-sm)',
                    fontFamily: 'var(--font-mono)',
                    fontSize: 11,
                    border: '1px solid var(--border-subtle)',
                }}
            >
                {events.length === 0 && (
                    <div style={{ color: 'var(--text-muted)' }}>⏳ Initializing browser... please wait</div>
                )}
                {events.map((event, i) => (
                    <div
                        key={i}
                        style={{
                            padding: '2px 0',
                            color: event.event_type === 'result_found'
                                ? 'var(--accent-emerald)'
                                : event.event_type === 'failed'
                                    ? 'var(--accent-crimson)'
                                    : event.event_type === 'rate_limited'
                                        ? 'var(--accent-amber)'
                                        : 'var(--text-secondary)',
                        }}
                    >
                        {event.event_type === 'result_found' ? '✅ ' : event.event_type === 'failed' ? '❌ ' : ''}
                        {event.message}
                    </div>
                ))}
            </div>
        </div>
    );
}
