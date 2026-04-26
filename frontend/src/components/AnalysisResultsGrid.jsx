/**
 * AnalysisResultsGrid — Implements the exact 3-column editable profile card UI
 * from the old Social Media Tool for the Analysis tab.
 */

import { useState, useCallback, useEffect, useRef } from 'react';
import { createJob, exportMemoryResults } from '../api/client';
import useStore from '../store';

const PAGE_SIZE = 50;

function calculateRiskScore(result) {
    const has_name = Boolean(result.has_name_match);
    const has_logo = Boolean(result.has_logo);
    const location = String(result.location || '').trim();
    const has_location = Boolean(location && location.toLowerCase() !== 'nan' && location.toLowerCase() !== 'none');

    let followers = 0;
    try {
        followers = parseInt(String(result.followers || 0).replace(/,/g, ''), 10);
        if (isNaN(followers)) followers = 0;
    } catch (e) { }

    const now = new Date();

    function getMonthsAgo(dateStr) {
        if (!dateStr || String(dateStr).toLowerCase() === 'nan' || String(dateStr).toLowerCase() === 'no') return 999;
        try {
            const parts = String(dateStr).split('-');
            let dt;
            if (parts.length === 2) {
                dt = new Date(parseInt(parts[1], 10), parseInt(parts[0], 10) - 1, 1);
            } else if (parts.length === 3) {
                dt = new Date(parseInt(parts[2], 10), parseInt(parts[1], 10) - 1, parseInt(parts[0], 10));
            } else {
                return 999;
            }
            return (now.getFullYear() - dt.getFullYear()) * 12 + (now.getMonth() - dt.getMonth());
        } catch (e) {
            return 999;
        }
    }

    const created_months = getMonthsAgo(result.created_at);
    const posted_months = getMonthsAgo(result.last_post_date);

    const is_new = created_months <= 6;
    const is_very_new = created_months <= 1;
    const is_active_post = posted_months <= 6;

    const is_active = is_active_post || is_new;
    const priority = has_logo ? "High" : "Low";

    let score = 0;
    if (has_name && has_logo && is_new && is_active && has_location && followers > 100) score = 9;
    else if (has_name && has_logo && is_active && has_location && is_very_new) score = 8;
    else if (has_name && has_logo && is_active && has_location) score = 7;
    else if (has_name && has_logo && (is_active || is_new)) score = 7;
    else if (has_name && has_logo) score = 6;
    else if (has_name && is_new) score = 4;
    else if (has_name) score = 3;

    return { risk_score: score, priority, is_active };
}

function EditableField({ label, value, type = 'text', options = [], onChange }) {
    const [localVal, setLocalVal] = useState(value || '');

    // Sync with external value changes
    useEffect(() => {
        setLocalVal(typeof value === 'boolean' ? (value ? 'Yes' : 'No') : (value || ''));
    }, [value]);

    const handleBlur = () => {
        if (type !== 'radio') {
            if (localVal !== (value || '')) {
                onChange(localVal);
            }
        }
    };

    const handleKeyDown = (e) => {
        if (e.key === 'Enter') {
            e.target.blur();
        }
    };

    return (
        <div style={{ display: 'flex', flexDirection: 'column', marginBottom: 14 }}>
            <span style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 6, fontWeight: 600 }}>{label}</span>
            {type === 'text' && (
                <input
                    type="text"
                    value={localVal}
                    onChange={(e) => setLocalVal(e.target.value)}
                    onBlur={handleBlur}
                    onKeyDown={handleKeyDown}
                    style={{
                        background: 'var(--bg-secondary)',
                        border: '1px solid #475569',
                        color: 'var(--text-primary)',
                        padding: '8px 12px',
                        borderRadius: 8,
                        fontSize: 14,
                        outline: 'none',
                        boxShadow: 'inset 0 1px 2px rgba(0,0,0,0.2)',
                        transition: 'border-color 0.2s'
                    }}
                    onFocus={(e) => e.target.style.borderColor = 'var(--accent-cyan)'}
                    onBlurCapture={(e) => {
                        e.target.style.borderColor = '#475569';
                    }}
                />
            )}
            {type === 'radio' && (
                <div style={{ display: 'flex', gap: 12, marginTop: 4 }}>
                    {options.map((opt) => (
                        <label key={opt} style={{
                            fontSize: 13, display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer',
                            background: localVal === opt ? 'rgba(0, 212, 255, 0.1)' : 'var(--bg-secondary)',
                            border: `1px solid ${localVal === opt ? 'var(--accent-cyan)' : '#475569'}`,
                            padding: '6px 16px',
                            borderRadius: 20,
                            color: localVal === opt ? 'var(--accent-cyan)' : 'var(--text-secondary)',
                            fontWeight: localVal === opt ? 600 : 400,
                            transition: 'all 0.2s',
                        }}>
                            <input
                                type="radio"
                                name={`${label}_${Math.random()}`}
                                checked={localVal === opt}
                                onChange={() => {
                                    setLocalVal(opt);
                                    let boolVal = undefined;
                                    if (opt === 'Yes') boolVal = true;
                                    if (opt === 'No') boolVal = false;
                                    onChange(boolVal !== undefined ? boolVal : opt);
                                }}
                                style={{ display: 'none' }} // hide the default radio circle
                            />
                            {opt}
                        </label>
                    ))}
                </div>
            )}
        </div>
    );
}

function AnalysisProfileCard({ result, onFieldChange, onValidate, activePlatform, selectedClient, onRescrape }) {
    const imgSrc = result.profile_image_b64
        ? `data:image/jpeg;base64,${result.profile_image_b64}`
        : result.profile_image_url || null;

    const screenshotSrc = result.screenshot_b64
        ? `data:image/jpeg;base64,${result.screenshot_b64}`
        : null;

    const handleFieldUpdate = (field, val) => {
        onFieldChange(result, { [field]: val });
    };

    // Screenshot popup state
    const [fullscreenImg, setFullscreenImg] = useState(null);
    const [hoverPreview, setHoverPreview] = useState(false);
    const thumbRef = useRef(null);

    return (
        <div className="card" style={{ marginBottom: 'var(--space-md)', padding: 'var(--space-lg)' }}>
            <h3 style={{ margin: '0 0 var(--space-md) 0', fontSize: 18 }}>{result.display_name || result.username || 'Unknown Profile'}</h3>

            <div style={{ display: 'grid', gridTemplateColumns: 'minmax(250px, 1.2fr) 1fr 1fr', gap: 'var(--space-xl)' }}>
                {/* Column 1: Profile Image + Screenshot Thumbnail */}
                <div>
                    {/* Profile Image */}
                    <div style={{
                        borderRadius: 8,
                        border: '1px solid var(--border-subtle)',
                        overflow: 'hidden',
                        marginBottom: 10,
                        background: 'var(--bg-primary)',
                    }}>
                        {imgSrc ? (
                            <img
                                src={imgSrc}
                                alt="Profile"
                                style={{ width: '100%', display: 'block', objectFit: 'cover', objectPosition: 'center top', maxHeight: 300 }}
                            />
                        ) : (
                            <div style={{ width: '100%', height: 120, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
                                🖼️ No profile image
                            </div>
                        )}
                    </div>

                    {/* Screenshot Thumbnail — hover for popup, click for fullscreen */}
                    {screenshotSrc && (
                        <div
                            ref={thumbRef}
                            style={{ position: 'relative', marginBottom: 10 }}
                            onMouseEnter={() => setHoverPreview(true)}
                            onMouseLeave={() => setHoverPreview(false)}
                        >
                            <div
                                onClick={() => setFullscreenImg(screenshotSrc)}
                                style={{
                                    width: '100%',
                                    borderRadius: 8,
                                    border: `1px solid ${hoverPreview ? 'var(--accent-cyan)' : 'var(--border-subtle)'}`,
                                    overflow: 'hidden',
                                    cursor: 'zoom-in',
                                    position: 'relative',
                                    transition: 'border-color 0.2s, box-shadow 0.2s',
                                    boxShadow: hoverPreview ? '0 0 12px rgba(0,210,255,0.25)' : 'none',
                                }}
                            >
                                <img
                                    src={screenshotSrc}
                                    alt="Page screenshot"
                                    style={{
                                        width: '100%',
                                        display: 'block',
                                        maxHeight: 120,
                                        objectFit: 'cover',
                                        objectPosition: 'center top',
                                        filter: hoverPreview ? 'brightness(1.05)' : 'brightness(0.9)',
                                        transition: 'filter 0.2s',
                                    }}
                                />
                                {/* Overlay label */}
                                <div style={{
                                    position: 'absolute',
                                    bottom: 0,
                                    left: 0,
                                    right: 0,
                                    padding: '6px 10px',
                                    background: 'linear-gradient(transparent, rgba(0,0,0,0.75))',
                                    color: '#fff',
                                    fontSize: 11,
                                    fontWeight: 600,
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: 4,
                                }}>
                                    📷 {hoverPreview ? 'Click for full view' : 'Screenshot'}
                                </div>
                            </div>

                            {/* Hover popup preview — positioned to the right */}
                            {hoverPreview && (
                                <div
                                    style={{
                                        position: 'absolute',
                                        top: 0,
                                        left: 'calc(100% + 12px)',
                                        width: 420,
                                        maxHeight: 500,
                                        zIndex: 1000,
                                        background: 'var(--bg-elevated, #1e293b)',
                                        border: '1px solid var(--accent-cyan)',
                                        borderRadius: 10,
                                        overflow: 'hidden',
                                        boxShadow: '0 8px 32px rgba(0,0,0,0.5), 0 0 16px rgba(0,210,255,0.15)',
                                        animation: 'screenshotPopupFadeIn 0.15s ease-out',
                                    }}
                                >
                                    <div style={{
                                        padding: '8px 12px',
                                        background: 'rgba(0,210,255,0.08)',
                                        borderBottom: '1px solid var(--border-subtle)',
                                        fontSize: 12,
                                        fontWeight: 600,
                                        color: 'var(--accent-cyan)',
                                        display: 'flex',
                                        alignItems: 'center',
                                        gap: 6,
                                    }}>
                                        🔍 Screenshot Preview — click to enlarge
                                    </div>
                                    <img
                                        src={screenshotSrc}
                                        alt="Screenshot preview"
                                        style={{
                                            width: '100%',
                                            display: 'block',
                                            maxHeight: 460,
                                            objectFit: 'contain',
                                        }}
                                    />
                                </div>
                            )}
                        </div>
                    )}

                    <a href={result.url} target={result.url && result.url.startsWith('tg://') ? '_self' : '_blank'} rel="noopener noreferrer" style={{ display: 'flex', alignItems: 'center', gap: 4, color: 'var(--accent-cyan)', textDecoration: 'none', fontWeight: 600, fontSize: 13 }}>
                        👉 Open Profile
                    </a>
                </div>

                {/* Fullscreen Screenshot Overlay */}
                {fullscreenImg && (
                    <div
                        onClick={() => setFullscreenImg(null)}
                        style={{
                            position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
                            background: 'rgba(0,0,0,0.92)', zIndex: 9999,
                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                            cursor: 'zoom-out', padding: 20,
                        }}
                    >
                        <img
                            src={fullscreenImg}
                            alt="Screenshot Full View"
                            style={{ maxWidth: '95vw', maxHeight: '95vh', objectFit: 'contain', borderRadius: 8, boxShadow: '0 0 60px rgba(0,0,0,0.5)' }}
                        />
                        <div style={{ position: 'absolute', top: 20, right: 30, color: '#fff', fontSize: 28, cursor: 'pointer', fontWeight: 700 }}>✕</div>
                    </div>
                )}

                {/* Column 2 */}
                <div>
                    <EditableField label="Original Name" value={result.original_name} onChange={(v) => handleFieldUpdate('original_name', v)} />
                    <EditableField label="Profile name" value={result.display_name || result.username} onChange={(v) => handleFieldUpdate('display_name', v)} />
                    <EditableField label="Logo (Yes / No)" type="radio" options={['Yes', 'No']} value={result.has_logo} onChange={(v) => handleFieldUpdate('has_logo', v)} />
                    <EditableField label="Active (Yes / No)" type="radio" options={['Yes', 'No']} value={result.is_active} onChange={(v) => handleFieldUpdate('is_active', v)} />
                    <EditableField label="Risk Score" value={result.risk_score} onChange={(v) => handleFieldUpdate('risk_score', v)} />
                    <EditableField label="Created Date" value={result.created_at} onChange={(v) => handleFieldUpdate('created_at', v)} />
                    <EditableField label="Original feed" value={result.original_feed} onChange={(v) => handleFieldUpdate('original_feed', v)} />
                </div>

                {/* Column 3 */}
                <div>
                    <EditableField label="Followers" value={result.followers} onChange={(v) => handleFieldUpdate('followers', v)} />
                    <EditableField label="Name (Yes / No)" type="radio" options={['Yes', 'No']} value={result.has_name_match} onChange={(v) => handleFieldUpdate('has_name_match', v)} />
                    <EditableField label="Location" value={result.location} onChange={(v) => handleFieldUpdate('location', v)} />
                    <EditableField label="Last Post (DD-MM-YYYY)" value={result.last_post_date || result.last_active} onChange={(v) => handleFieldUpdate('last_post_date', v)} />
                    <EditableField label="priority" type="radio" options={['Low', 'High']} value={result.priority} onChange={(v) => handleFieldUpdate('priority', v)} />
                    <EditableField label="Comments" value={result.comments} onChange={(v) => handleFieldUpdate('comments', v)} />

                    <div style={{ marginTop: 'var(--space-md)', display: 'flex', flexDirection: 'column', gap: 12 }}>
                        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600, cursor: 'pointer' }}>
                            <input
                                type="checkbox"
                                checked={result.status === 'approved'}
                                onChange={(e) => onValidate(result, e.target.checked)}
                                style={{ width: 18, height: 18, cursor: 'pointer' }}
                            />
                            ✅ Validate
                        </label>

                        <button
                            className="btn btn-secondary"
                            style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6 }}
                            onClick={() => onRescrape(result.url)}
                        >
                            🔄 Re-scrape
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}

export default function AnalysisResultsGrid({ activePlatform, inMemoryResults, setInMemoryResults }) {
    const { selectedClient } = useStore();
    const [page, setPage] = useState(0);
    const [isRescraping, setIsRescraping] = useState(false);

    const results = inMemoryResults || [];
    const totalCount = results.length;
    const totalPages = Math.ceil(totalCount / PAGE_SIZE);

    const paginatedResults = results.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

    const handleFieldChange = useCallback((resultToUpdate, fields) => {
        setInMemoryResults(prev => prev.map(r => {
            if (r.url === resultToUpdate.url) {
                const updated = { ...r, ...fields };

                // Recalculate Risk Score dynamically if relevant fields changed
                if ('has_logo' in fields || 'is_active' in fields || 'has_name_match' in fields || 'followers' in fields || 'location' in fields || 'created_at' in fields || 'last_post_date' in fields) {
                    const { risk_score, priority, is_active } = calculateRiskScore(updated);
                    updated.risk_score = risk_score;
                    updated.priority = priority;
                    if (!('is_active' in fields)) {
                        // auto update is_active unless manual override
                        updated.is_active = is_active;
                    }
                }
                return updated;
            }
            return r;
        }));
    }, [setInMemoryResults]);

    const handleValidate = (resultToUpdate, checked) => {
        setInMemoryResults(prev => prev.map(r => {
            if (r.url === resultToUpdate.url) {
                return { ...r, status: checked ? 'approved' : 'pending' };
            }
            return r;
        }));
    };

    const handleValidateAll = () => {
        setInMemoryResults(prev => prev.map(r => ({ ...r, status: 'approved' })));
    };

    const handleRescrape = async (url) => {
        setIsRescraping(true);
        try {
            await createJob({
                platform: activePlatform,
                mode: 'analysis',
                client: selectedClient,
                keywords: [url],
                headless: true,
            });
            // Job runs in background, JobMonitor picks it up and replaces array items later
        } catch (err) {
            console.error('Re-scrape failed:', err);
        } finally {
            setIsRescraping(false);
        }
    };

    const handleExportExcel = async () => {
        try {
            const res = await exportMemoryResults(selectedClient, {
                platform: activePlatform,
                results: results,
            });
            const url = window.URL.createObjectURL(new Blob([res.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }));
            const a = document.createElement('a');
            a.href = url;
            a.download = `${selectedClient}_${activePlatform}_profiles_with_screenshots.xlsx`;
            a.click();
            window.URL.revokeObjectURL(url);
        } catch (err) {
            console.error('Export failed:', err);
            alert(`Export failed: ${err.message}`);
        }
    };

    // Build TSV content with the exact 14-column format from the old tool
    const buildTsvContent = useCallback((data) => {
        const header = [
            'Original Name', 'Original feed', 'IMPERSONATED', 'Profile name',
            'Created Date', 'Logo (Yes / No)', 'Followers', 'Active (Yes / No)',
            'Name (Yes / No)', 'Location', 'Last Post (DD-MM-YYYY) (Optional)',
            'Risk Score', 'priority', 'Date', 'Comments',
        ].join('\t');
        const rows = data.map(r => {
            const cols = [
                '',                                                 // Original Name (always empty)
                '',                                                 // Original feed (always empty)
                r.url || '',                                        // IMPERSONATED (profile URL)
                r.display_name || r.username || '',                 // Profile name
                r.created_at || '',                                 // Created Date
                r.has_logo ? 'Yes' : 'No',                         // Logo (Yes / No)
                r.followers != null ? String(r.followers) : '0',   // Followers
                r.is_active ? 'Yes' : 'No',                        // Active (Yes / No)
                r.has_name_match ? 'Yes' : 'No',                   // Name (Yes / No)
                r.location || '',                                   // Location
                r.last_post_date || '',                             // Last Post
                r.risk_score != null ? String(r.risk_score) : '0', // Risk Score
                r.priority || 'Low',                                // priority
                r.first_seen ? new Date(r.first_seen).toLocaleDateString() : new Date().toLocaleDateString(), // Date
                r.comments || '',                                   // Comments
            ];
            return cols.join('\t');
        });
        return [header, ...rows].join('\n');
    }, []);

    if (!selectedClient) {
        return (
            <div className="results-empty">
                <div className="results-empty__icon">📂</div>
                <h3>Select a Client</h3>
                <p>Choose or create a client from the sidebar to view results.</p>
            </div>
        );
    }

    return (
        <div style={{ marginTop: 'var(--space-xl)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--space-md)' }}>
                <h2 style={{ fontSize: 20 }}>📋 Review & Edit Each Profile</h2>
                <div style={{ display: 'flex', gap: 8 }}>
                    <button className="btn btn-primary" onClick={handleValidateAll}>
                        ✅ Validate All Profiles
                    </button>
                    <button className="btn btn-secondary" onClick={() => refetch()}>
                        🔄 Refresh
                    </button>
                </div>
            </div>

            <hr style={{ borderColor: 'var(--border-subtle)', marginBottom: 'var(--space-lg)' }} />

            {results.length === 0 ? (
                <div style={{ textAlign: 'center', padding: 40, color: 'var(--text-muted)' }}>No profiles found for {selectedClient}.</div>
            ) : (
                <>
                    {paginatedResults.map((result) => (
                        <AnalysisProfileCard
                            key={result.url}
                            result={result}
                            onFieldChange={handleFieldChange}
                            onValidate={handleValidate}
                            activePlatform={activePlatform}
                            selectedClient={selectedClient}
                            onRescrape={handleRescrape}
                        />
                    ))}

                    <div className="results-pagination" style={{ justifyContent: 'center', padding: 'var(--space-lg) 0' }}>
                        <button className="btn-page" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>← Prev</button>
                        <span className="page-info">Page {page + 1} of {totalPages || 1} ({totalCount} total)</span>
                        <button className="btn-page" disabled={page >= totalPages - 1} onClick={() => setPage((p) => p + 1)}>Next →</button>
                    </div>

                    {/* Final Review & Downloads Section — matches old tool layout */}
                    <div style={{ marginTop: 'var(--space-2xl)', borderTop: '2px dashed var(--border-subtle)', paddingTop: 'var(--space-lg)' }}>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-lg)' }}>

                            {/* LEFT: Copy TSV Results */}
                            <div className="card">
                                <h3 style={{ fontSize: 16, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                                    📋 Copy TSV Results
                                </h3>
                                <textarea
                                    readOnly
                                    style={{
                                        width: '100%', minHeight: 180, padding: 12,
                                        background: 'var(--bg-primary)', color: 'var(--text-primary)',
                                        border: '1px solid var(--border-subtle)', borderRadius: 8,
                                        fontFamily: 'monospace', fontSize: 11, lineHeight: 1.5,
                                        resize: 'vertical', whiteSpace: 'pre', overflowX: 'auto',
                                    }}
                                    value={buildTsvContent(results)}
                                    onClick={(e) => e.target.select()}
                                />
                                <button
                                    style={{
                                        marginTop: 10, width: '100%', padding: '10px 16px',
                                        borderRadius: 8, border: 'none', cursor: 'pointer',
                                        background: 'linear-gradient(135deg, #0891b2, #06b6d4)',
                                        color: '#fff', fontWeight: 600, fontSize: 13,
                                    }}
                                    onClick={() => {
                                        navigator.clipboard.writeText(buildTsvContent(results));
                                        alert('TSV results copied to clipboard!');
                                    }}
                                >
                                    📋 Copy to Clipboard
                                </button>
                            </div>

                            {/* RIGHT: Download Scraped Results */}
                            <div className="card">
                                <h3 style={{ fontSize: 16, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                                    📁 Download Scraped Results
                                </h3>
                                <p style={{ fontSize: 13, color: 'var(--text-muted)', marginBottom: 20, lineHeight: 1.6 }}>
                                    Download all analyzed profiles in Excel format. The export includes all editable fields (Risk Score, Logo, Active, Name Match, Comments) with their current values.
                                </p>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                                    <button
                                        style={{
                                            padding: '12px 20px', borderRadius: 8, border: '1px solid var(--border-subtle)',
                                            background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                                            cursor: 'pointer', fontWeight: 600, fontSize: 14,
                                            display: 'flex', alignItems: 'center', gap: 8,
                                            transition: 'background 0.2s',
                                        }}
                                        onClick={handleExportExcel}
                                        onMouseEnter={(e) => e.target.style.background = 'var(--bg-hover)'}
                                        onMouseLeave={(e) => e.target.style.background = 'var(--bg-elevated)'}
                                    >
                                        ⬇ Download Excel with Screenshots
                                    </button>
                                    <button
                                        style={{
                                            padding: '12px 20px', borderRadius: 8, border: '1px solid var(--border-subtle)',
                                            background: 'var(--bg-elevated)', color: 'var(--text-primary)',
                                            cursor: 'pointer', fontWeight: 600, fontSize: 14,
                                            display: 'flex', alignItems: 'center', gap: 8,
                                            transition: 'background 0.2s',
                                        }}
                                        onClick={async () => {
                                            try {
                                                const exportData = results.map(r => {
                                                    const { screenshot_b64, profile_image_b64, ...rest } = r;
                                                    return rest;
                                                });
                                                const res = await exportMemoryResults(selectedClient, {
                                                    platform: activePlatform,
                                                    results: exportData,
                                                });
                                                const url = window.URL.createObjectURL(new Blob([res.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }));
                                                const a = document.createElement('a');
                                                a.href = url;
                                                a.download = `${selectedClient}_${activePlatform}_profiles_no_screenshots.xlsx`;
                                                a.click();
                                                window.URL.revokeObjectURL(url);
                                            } catch (err) {
                                                console.error('Export failed:', err);
                                                alert(`Export failed: ${err.message}`);
                                            }
                                        }}
                                        onMouseEnter={(e) => e.target.style.background = 'var(--bg-hover)'}
                                        onMouseLeave={(e) => e.target.style.background = 'var(--bg-elevated)'}
                                    >
                                        ⬇ Download Excel without Screenshots
                                    </button>
                                </div>
                            </div>

                        </div>
                    </div>
                </>
            )}
        </div>
    );
}
