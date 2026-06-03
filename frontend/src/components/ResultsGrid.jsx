/**
 * Virtual DOM Reconciliation & Memoization Boundary.
 * Orchestrates the lifecycle of heavily hydrated card components containing volatile multi-media payloads (B64 images).
 * Employs optimistic UI mutations to eagerly apply status transitions before server acknowledgment, 
 * ensuring perceived sub-millisecond latency for high-speed OSINT tagging.
 */

import React, { useState, useCallback, useRef, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
    getResults,
    updateResultStatus,
    updateResultFields,
    exportResults,
    getKeywords,
    getKnownUrls,
    getValidatedUrls,
} from '../api/client';
import useStore from '../store';

const PAGE_SIZE = 20;
const DEFAULT_AVATAR = 'data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAxMDAgMTAwIj48cmVjdCB3aWR0aD0iMTAwIiBoZWlnaHQ9IjEwMCIgZmlsbD0iIzFhMWExYSIvPjxjaXJjbGUgY3g9IjUwIiBjeT0iMzUiIHI9IjE4IiBmaWxsPSIjNDQ0Ii8+PHBhdGggZD0iTTIwIDg1YTMwIDMwIDAgMCAxIDYwIDAiIGZpbGw9IiM0NDQiLz48dGV4dCB4PSI1MCIgeT0iNjUiIGZvbnQtc2l6ZT0iOCIgZmlsbD0iIzY2NiIgdGV4dC1hbmNob3I9Im1pZGRsZSI+Tm8gSW1hZ2U8L3RleHQ+PC9zdmc+';

// ─── Pure Functional Component: Deterministic renderer mapping immutable prop streams ────────────

function ProfileCard({
    result, onApprove, onReject, onFieldChange, isValidatedTab,
    selected, onSelect, onProfileClick
}) {

    const isNew = result.first_seen &&
        (Date.now() - new Date(result.first_seen).getTime()) < 86400000;

    // Build HD image URL using Facebook Graph API (no CORS on <img> tags)
    const extractUsername = (url) => {
        if (!url) return null;
        // Handle profile.php?id=12345
        const idMatch = url.match(/profile\.php\?id=(\d+)/);
        if (idMatch) return idMatch[1];
        // Handle facebook.com/username
        const parts = url.replace(/\/$/, '').split('/');
        const last = parts[parts.length - 1];
        if (last && !['www.facebook.com', 'facebook.com', 'm.facebook.com'].includes(last)) return last;
        return null;
    };

    const fbUsername = extractUsername(result.url);
    const graphApiUrl = fbUsername && result.platform === 'facebook'
        ? `https://graph.facebook.com/${fbUsername}/picture?type=large&width=2048&height=2048`
        : null;

    // Priority: base64 (HD from backend) → Graph API → original URL → default avatar
    const imgSrc = (result.profile_image_b64 ? `data:image/jpeg;base64,${result.profile_image_b64}` : null)
        || graphApiUrl
        || result.profile_image_url
        || DEFAULT_AVATAR;

    const [currentImgSrc, setCurrentImgSrc] = useState(imgSrc);
    const imgFallbacks = useRef([
        graphApiUrl,
        result.profile_image_url,
        DEFAULT_AVATAR,
    ].filter(Boolean));
    const fallbackIndex = useRef(0);

    const handleImgError = () => {
        console.warn(`Image load failed for ${result.username || result.display_name}. Trying fallback...`);
        if (fallbackIndex.current < imgFallbacks.current.length) {
            const nextSrc = imgFallbacks.current[fallbackIndex.current];
            console.log(`Falling back to: ${typeof nextSrc === 'string' ? nextSrc.substring(0, 50) : 'binary data'}...`);
            setCurrentImgSrc(nextSrc);
            fallbackIndex.current += 1;
        } else {
            console.error(`All fallbacks failed for ${result.username}. Using default avatar.`);
            setCurrentImgSrc(DEFAULT_AVATAR);
        }
    };

    const confidenceColor = {
        HIGH: '#22c55e',
        MEDIUM: '#f59e0b',
        LOW: '#ef4444',
    };


    return (
        <div
            className={`profile-card ${selected ? 'profile-card--selected' : ''}`}
            style={{ cursor: onProfileClick ? 'pointer' : 'default' }}
        >
            {/* Image + badges */}
            <div className="profile-card__image-wrap" onClick={() => onProfileClick && onProfileClick(result)}>
                <img
                    src={currentImgSrc}
                    alt={result.display_name || 'Profile'}
                    className="profile-card__image"
                    loading="lazy"
                    referrerPolicy="no-referrer-when-downgrade"
                    onError={handleImgError}
                />
                {result.confidence && (
                    <span
                        className="profile-card__confidence"
                        style={{ background: confidenceColor[result.confidence] || '#666' }}
                    >
                        {result.confidence}
                    </span>
                )}
                <span className={`profile-card__age-badge ${isNew ? 'badge--new' : 'badge--old'}`}>
                    {isNew ? 'NEW' : 'OLD'}
                </span>
            </div>

            {/* Content area — flexible middle section */}
            <div className="profile-card__content">
                {/* Profile Name */}
                <h4 className="profile-card__name" title={result.display_name || result.username}>
                    <span className="name-text">
                        {result.display_name || result.username || 'Unknown'}
                    </span>
                    {result.is_verified && <span className="verified-badge" title="Verified">✓</span>}
                </h4>

                {/* Meta row: entity type + keyword */}
                <div className="profile-card__meta">
                    <span>{result.entity_type || result.platform}</span>
                    <span title={Array.isArray(result.keywords) ? result.keywords.join(', ') : (result.keyword || '')}>
                        {(() => {
                            const kws = Array.isArray(result.keywords) ? result.keywords : (result.keyword ? [result.keyword] : []);
                            if (kws.length === 0) return '';
                            if (kws.length <= 2) return kws.join(', ');
                            return `${kws.slice(0, 2).join(', ')} +${kws.length - 2} more`;
                        })()}
                    </span>
                </div>

                {/* Bio (skip for Telegram) */}
                {result.bio && result.platform !== 'telegram' && (
                    <p className="profile-card__bio" title={result.bio}>
                        {result.bio.substring(0, 80)}{result.bio.length > 80 ? '...' : ''}
                    </p>
                )}

                {/* Stats */}
                <div className="profile-card__stats-grid">
                    {(result.followers !== null && result.followers !== undefined) && (
                        <div className="stat-item" title="Followers">
                            <span className="stat-icon">👥</span>
                            <span>{result.followers?.toLocaleString()}</span>
                        </div>
                    )}
                    {result.location && (
                        <div className="stat-item" title="Location">
                            <span className="stat-icon">📍</span>
                            <span>{result.location.substring(0, 15)}{result.location.length > 15 ? '...' : ''}</span>
                        </div>
                    )}
                    {result.last_post_date && (
                        <div className="stat-item" title="Last Post">
                            <span className="stat-icon">🕒</span>
                            <span>{result.last_post_date}</span>
                        </div>
                    )}
                    {result.created_at && (
                        <div className="stat-item" title="Created/Joined">
                            <span className="stat-icon">📅</span>
                            <span>{String(result.created_at).substring(0, 12)}</span>
                        </div>
                    )}
                </div>
            </div>

            {/* Footer — pinned to bottom of card */}
            <div className="profile-card__footer">
                {/* Action Buttons */}
                <div className="profile-card__actions-row">
                    {result.status !== 'approved' && (
                        <button className="btn-validate" onClick={(e) => { e.stopPropagation(); onApprove(result); }} title="Validate">
                            ✓ Validate
                        </button>
                    )}
                    {result.status !== 'rejected' && (
                        <button className="btn-reject-styled" onClick={(e) => { e.stopPropagation(); onReject(result); }} title="Reject">
                            ✕ Reject
                        </button>
                    )}
                </div>

                <a href={result.url} target={result.url && result.url.startsWith('tg://') ? '_self' : '_blank'} rel="noopener noreferrer" className="btn-view-profile"
                    onClick={(e) => e.stopPropagation()}
                >
                    View Profile →
                </a>

                {/* Selection checkbox for validated tab */}
                {isValidatedTab && (
                    <button
                        className={`btn-select ${selected ? 'btn-select--active' : ''}`}
                        onClick={(e) => { e.stopPropagation(); onSelect(result._id); }}
                    >
                        {selected ? '✅ Selected' : '⬜ Select'}
                    </button>
                )}
            </div>
        </div>
    );
}

// ─── Results Grid ────────────────────────────────────────────────────────────

export default function ResultsGrid({ activePlatform, onProfileClick, liveResults = [], hasActiveJob = false, onAnalyzeUrls, activeJobKeywords = [] }) {
    const { selectedClient } = useStore();
    const queryClient = useQueryClient();

    const [activeTab, setActiveTab] = useState('pending');
    const [keyword, setKeyword] = useState(null);
    const [confidence, setConfidence] = useState(null);
    const [page, setPage] = useState(0);
    const [selectedIds, setSelectedIds] = useState(new Set());
    const [hiddenIds, setHiddenIds] = useState(new Set());
    const [loadingAllUrls, setLoadingAllUrls] = useState(false);
    const [copySelectedFeedback, setCopySelectedFeedback] = useState(false);
    const [copyAllFeedback, setCopyAllFeedback] = useState(false);

    // Clear hidden/selected state when filters or tabs change
    useEffect(() => {
        setHiddenIds(new Set());
        setSelectedIds(new Set());
        setPage(0);
    }, [activeTab, activePlatform, selectedClient, keyword, confidence]);

    // Fetch results — poll faster during active job
    const { data, isLoading, refetch } = useQuery({
        queryKey: ['results', selectedClient, activePlatform, activeTab, keyword, confidence, page],
        queryFn: async () => {
            const params = {
                platform: activePlatform,
                status: activeTab,
                limit: PAGE_SIZE,
                offset: page * PAGE_SIZE,
            };
            if (keyword) params.keyword = keyword;
            if (confidence) params.confidence = confidence;
            const res = await getResults(selectedClient, params);
            return res.data;
        },
        enabled: !!selectedClient && !!activePlatform,
        refetchInterval: hasActiveJob ? 3000 : 10000,
    });

    // Fetch ALL known URLs for this client+platform (any status) during active jobs
    // This prevents validated/rejected profiles from reappearing in the pending live feed
    const { data: knownUrlsData } = useQuery({
        queryKey: ['known-urls', selectedClient, activePlatform],
        queryFn: async () => {
            const res = await getKnownUrls(selectedClient, activePlatform);
            return res.data.urls;
        },
        enabled: !!selectedClient && !!activePlatform && hasActiveJob,
        refetchInterval: hasActiveJob ? 5000 : false,
    });
    const knownUrlsSet = new Set(knownUrlsData || []);

    // Merge DB results with live WebSocket results (dedupe by URL)
    const dbResults = data?.results || [];
    const dbUrls = new Set(dbResults.map(r => r.url));
    const newLiveResults = activeTab === 'pending'
        ? liveResults.filter(r => {
              if (dbUrls.has(r.url) || knownUrlsSet.has(r.url)) return false;
              if (keyword && r.keyword !== keyword) return false;
              if (confidence && r.confidence !== confidence) return false;
              return true;
          })
        : [];
    const results = [...dbResults, ...newLiveResults].filter(r => !hiddenIds.has(r._id) && !hiddenIds.has(r.url));
    const totalCount = (data?.count || 0) + newLiveResults.length;
    const totalPages = Math.ceil(totalCount / PAGE_SIZE) || 1;

    // Fetch keywords for the filter dropdown
    const { data: keywordsData } = useQuery({
        queryKey: ['keywords', selectedClient, activePlatform],
        queryFn: async () => {
            const res = await getKeywords(selectedClient, activePlatform);
            return res.data.keywords;
        },
        enabled: !!selectedClient && !!activePlatform,
        refetchInterval: hasActiveJob ? 5000 : 15000,
    });

    // Combine DB keywords with any new keywords actively streaming over WebSockets
    const availableKeywords = Array.from(new Set([
        ...(keywordsData || []),
        ...activeJobKeywords,
        ...(liveResults || []).map(r => r.keyword).filter(Boolean) // Instantly appear in dropdown
    ])).sort();

    // Fetch TOTAL validated count (not paginated) — used by ALL buttons in validated tab
    const { data: validatedUrlsData } = useQuery({
        queryKey: ['validated-urls-count', selectedClient, activePlatform],
        queryFn: async () => {
            const res = await getValidatedUrls(selectedClient, activePlatform);
            return res.data;
        },
        enabled: !!selectedClient && !!activePlatform && activeTab === 'approved',
        refetchInterval: 15000,
    });
    const totalValidatedCount = validatedUrlsData?.count || 0;

    // Status mutation with OPTIMISTIC update — card vanishes immediately
    const statusMutation = useMutation({
        mutationFn: async ({ docId, platform, status }) => {
            await updateResultStatus(docId, platform, status);
        },
        onMutate: async ({ docId }) => {
            // Cancel any outgoing refetches
            await queryClient.cancelQueries({ queryKey: ['results'] });
            // Snapshot previous data
            const previousData = queryClient.getQueryData(
                ['results', selectedClient, activePlatform, activeTab, keyword, confidence, page]
            );
            // Optimistically remove this card from current results
            if (previousData) {
                queryClient.setQueryData(
                    ['results', selectedClient, activePlatform, activeTab, keyword, confidence, page],
                    (old) => {
                        if (!old) return previousData;
                        return {
                            ...old,
                            results: old.results.filter(r => r._id !== docId),
                            count: Math.max(0, (old.count || 0) - 1),
                        };
                    }
                );
            }
            return { previousData };
        },
        onError: (_err, _vars, context) => {
            // Rollback on error
            if (context?.previousData) {
                queryClient.setQueryData(
                    ['results', selectedClient, activePlatform, activeTab, keyword, confidence, page],
                    context.previousData
                );
            }
        },
        onSettled: () => {
            // Always refetch to sync with server
            queryClient.invalidateQueries({ queryKey: ['results'] });
        },
    });

    // Field editing mutation
    const fieldMutation = useMutation({
        mutationFn: async ({ docId, platform, fields }) => {
            const res = await updateResultFields(docId, platform, fields);
            return res.data;
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['results'] });
        },
    });

    const handleApprove = (result) => {
        if (!result._id) {
            alert('Cannot approve right now. The database has not finished saving this profile. Please wait a moment and try again.');
            return;
        }
        const idToHide = result._id || result.url;
        if (idToHide) {
            setHiddenIds((prev) => new Set(prev).add(idToHide));
        }

        statusMutation.mutate({ docId: result._id, platform: result.platform, status: 'approved' });
    };

    const handleReject = (result) => {
        if (!result._id) {
            alert('Cannot reject right now. The database has not finished saving this profile. Please wait a moment and try again.');
            return;
        }
        const idToHide = result._id || result.url;
        if (idToHide) {
            setHiddenIds((prev) => new Set(prev).add(idToHide));
        }

        statusMutation.mutate({ docId: result._id, platform: result.platform, status: 'rejected' });
    };

    const handleFieldChange = useCallback((result, fields) => {
        if (!result._id) return;
        fieldMutation.mutate({ docId: result._id, platform: result.platform, fields });
    }, [fieldMutation]);

    const toggleSelect = (id) => {
        setSelectedIds((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    const handleExport = async () => {
        try {
            const res = await exportResults(selectedClient, {
                platform: activePlatform,
                status: activeTab,
            });
            const url = window.URL.createObjectURL(new Blob([res.data], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }));
            const a = document.createElement('a');
            a.href = url;
            a.download = `${selectedClient}_${activePlatform}_results.xlsx`;
            a.click();
            window.URL.revokeObjectURL(url);
        } catch (err) {
            console.error('Export failed:', err);
            alert(`Export failed: ${err.message}`);
        }
    };

    const handleCopySelected = () => {
        const urls = results.filter(r => selectedIds.has(r._id)).map(r => r.url);
        if (urls.length > 0) {
            navigator.clipboard.writeText(urls.join('\n'));
            setCopySelectedFeedback(true);
            setTimeout(() => setCopySelectedFeedback(false), 2000);
        }
    };

    const handleCopyAllValidated = async () => {
        // Fetch ALL validated URLs from backend, not just current page
        setLoadingAllUrls(true);
        try {
            const res = await getValidatedUrls(selectedClient, activePlatform);
            const urls = res.data.urls || [];
            if (urls.length > 0) {
                navigator.clipboard.writeText(urls.join('\n'));
                setCopyAllFeedback(true);
                setTimeout(() => setCopyAllFeedback(false), 2000);
            }
        } catch (err) {
            console.error('Failed to fetch validated URLs:', err);
            // Fallback to current page results
            const urls = results.map(r => r.url);
            if (urls.length > 0) {
                navigator.clipboard.writeText(urls.join('\n'));
                setCopyAllFeedback(true);
                setTimeout(() => setCopyAllFeedback(false), 2000);
            }
        } finally {
            setLoadingAllUrls(false);
        }
    };

    const handleValidateAll = () => {
        results.forEach((result) => {
            if (result._id && result.status !== 'approved') {
                const idToHide = result._id || result.url;
                if (idToHide) {
                    setHiddenIds((prev) => new Set(prev).add(idToHide));
                }
                statusMutation.mutate({ docId: result._id, platform: result.platform, status: 'approved' });
            }
        });
    };

    if (!selectedClient) {
        return (
            <div className="results-empty">
                <div className="results-empty__icon">📂</div>
                <h3>Select a Client</h3>
                <p>Choose or create a client from the sidebar to view results.</p>
            </div>
        );
    }

    const tabs = [
        { id: 'pending', label: '🟡 Pending Review', color: '#f59e0b' },
        { id: 'approved', label: '🟢 Validated', color: '#22c55e' },
        { id: 'rejected', label: '🔴 Rejected', color: '#ef4444' },
    ];

    return (
        <div className="results-container">
            {/* Filter Bar */}
            <div className="results-filter-bar">
                <div className="results-filters">
                    <select
                        className="filter-select"
                        value={keyword || ''}
                        onChange={(e) => { setKeyword(e.target.value || null); setPage(0); }}
                    >
                        <option value="">All Keywords</option>
                        {availableKeywords.map((kw) => (
                            <option key={kw} value={kw}>{kw}</option>
                        ))}
                    </select>

                    <select
                        className="filter-select"
                        value={confidence || ''}
                        onChange={(e) => { setConfidence(e.target.value || null); setPage(0); }}
                    >
                        <option value="">All Confidence</option>
                        <option value="HIGH">HIGH</option>
                        <option value="MEDIUM">MEDIUM</option>
                        <option value="LOW">LOW</option>
                    </select>
                </div>

                <div className="results-actions-bar">
                    <button className="btn-export" onClick={handleExport}>📥 Export Excel</button>
                    <button className="btn-refresh" onClick={() => refetch()}>🔄 Refresh</button>
                </div>
            </div>

            {/* 3-Tab Layout */}
            <div className="results-tabs">
                {tabs.map((tab) => (
                    <button
                        key={tab.id}
                        className={`tab-btn ${activeTab === tab.id ? 'tab-btn--active' : ''}`}
                        style={activeTab === tab.id ? { borderBottomColor: tab.color } : {}}
                        onClick={() => setActiveTab(tab.id)}
                    >
                        {tab.label}
                    </button>
                ))}
            </div>

            {/* Batch actions for pending tab */}
            {activeTab === 'pending' && results.length > 0 && (
                <div style={{
                    display: 'flex', gap: 8, marginBottom: 'var(--space-sm)', padding: '8px 0',
                }}>
                    <button
                        className="btn btn-primary"
                        style={{ fontSize: 12, padding: '6px 14px' }}
                        onClick={handleValidateAll}
                    >
                        ✅ Validate All ({results.length})
                    </button>
                </div>
            )}

            {/* Action buttons for validated tab */}
            {activeTab === 'approved' && results.length > 0 && (
                <>
                    {/* Info banner: total validated count */}
                    {totalValidatedCount > results.length && (
                        <div style={{
                            display: 'flex', alignItems: 'center', gap: 8,
                            padding: '10px 16px', marginBottom: 8,
                            background: 'linear-gradient(135deg, rgba(34,197,94,0.08), rgba(16,185,129,0.08))',
                            border: '1px solid rgba(34,197,94,0.2)',
                            borderRadius: 10, fontSize: 13, color: 'var(--text-secondary)',
                        }}>
                            <span style={{ fontSize: 16 }}>📊</span>
                            <span>
                                Showing <strong style={{ color: 'var(--text-primary)' }}>{results.length}</strong> of{' '}
                                <strong style={{ color: '#22c55e' }}>{totalValidatedCount}</strong> total validated profiles.
                                Use the <em>"ALL"</em> buttons below to copy or analyze all {totalValidatedCount} profiles.
                            </span>
                        </div>
                    )}
                    <div style={{
                        display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8,
                        marginBottom: 'var(--space-sm)', padding: '8px 0',
                    }}>
                        <button
                            style={{
                                fontSize: 13, padding: '10px 16px', borderRadius: 8,
                                background: 'linear-gradient(135deg, #0891b2, #06b6d4)',
                                color: '#fff', border: 'none', cursor: selectedIds.size === 0 ? 'not-allowed' : 'pointer',
                                fontWeight: 600, opacity: selectedIds.size === 0 ? 0.5 : 1,
                            }}
                            onClick={() => {
                                const urls = results.filter(r => selectedIds.has(r._id)).map(r => r.url);
                                if (urls.length > 0 && onAnalyzeUrls) onAnalyzeUrls(urls);
                            }}
                            disabled={selectedIds.size === 0}
                        >
                            🔬 Analyze SELECTED Profiles ({selectedIds.size})
                        </button>
                        <button
                            style={{
                                fontSize: 13, padding: '10px 16px', borderRadius: 8,
                                background: loadingAllUrls
                                    ? 'linear-gradient(135deg, #374151, #4b5563)'
                                    : 'linear-gradient(135deg, #059669, #10b981)',
                                color: '#fff', border: 'none', cursor: loadingAllUrls ? 'wait' : 'pointer',
                                fontWeight: 600, transition: 'all 0.2s ease',
                            }}
                            disabled={loadingAllUrls}
                            onClick={async () => {
                                setLoadingAllUrls(true);
                                try {
                                    const res = await getValidatedUrls(selectedClient, activePlatform);
                                    const urls = res.data.urls || [];
                                    if (urls.length > 0 && onAnalyzeUrls) onAnalyzeUrls(urls);
                                    else alert('No validated URLs found');
                                } catch (err) {
                                    console.error('Failed to fetch validated URLs:', err);
                                    const urls = results.map(r => r.url);
                                    if (urls.length > 0 && onAnalyzeUrls) onAnalyzeUrls(urls);
                                } finally {
                                    setLoadingAllUrls(false);
                                }
                            }}
                        >
                            {loadingAllUrls ? '⏳ Fetching...' : `🔬 Analyze ALL Validated (${totalValidatedCount})`}
                        </button>
                        <button
                            style={{
                                fontSize: 13, padding: '10px 16px', borderRadius: 8,
                                background: copySelectedFeedback
                                    ? 'linear-gradient(135deg, #059669, #10b981)'
                                    : 'linear-gradient(135deg, #1e3a5f, #2563eb)',
                                color: '#fff', border: 'none', cursor: selectedIds.size === 0 ? 'not-allowed' : 'pointer',
                                fontWeight: 600, opacity: selectedIds.size === 0 ? 0.5 : 1,
                                transition: 'background 0.3s ease',
                            }}
                            onClick={handleCopySelected}
                            disabled={selectedIds.size === 0}
                        >
                            {copySelectedFeedback ? '✅ Copied!' : `📋 Copy Selected Profile URLs (${selectedIds.size})`}
                        </button>
                        <button
                            style={{
                                fontSize: 13, padding: '10px 16px', borderRadius: 8,
                                background: copyAllFeedback
                                    ? 'linear-gradient(135deg, #059669, #10b981)'
                                    : loadingAllUrls
                                        ? 'linear-gradient(135deg, #374151, #4b5563)'
                                        : 'linear-gradient(135deg, #6b21a8, #7c3aed)',
                                color: '#fff', border: 'none', cursor: loadingAllUrls ? 'wait' : 'pointer',
                                fontWeight: 600, transition: 'all 0.2s ease',
                            }}
                            disabled={loadingAllUrls}
                            onClick={handleCopyAllValidated}
                        >
                            {copyAllFeedback ? '✅ Copied!' : loadingAllUrls ? '⏳ Fetching...' : `📋 Copy ALL Validated URLs (${totalValidatedCount})`}
                        </button>
                    </div>
                </>
            )}

            {/* Results Content */}
            {isLoading ? (
                <div className="results-loading">
                    <div className="spinner" />
                    <span>Loading results...</span>
                </div>
            ) : results.length === 0 ? (
                <div className="results-empty-tab">
                    <p>No {activeTab} profiles found{keyword ? ` for keyword "${keyword}"` : ''}.</p>
                </div>
            ) : (
                <>
                    {/* Profile Cards Grid */}
                    <div className="profile-cards-grid">
                        {results.map((result) => (
                            <ProfileCard
                                key={result._id}
                                result={result}
                                onApprove={handleApprove}
                                onReject={handleReject}
                                onFieldChange={handleFieldChange}
                                isValidatedTab={activeTab === 'approved'}
                                selected={selectedIds.has(result._id)}
                                onSelect={toggleSelect}
                                onProfileClick={onProfileClick}
                            />
                        ))}
                    </div>

                    {/* Pagination */}
                    <div className="results-pagination">
                        <button
                            className="btn-page"
                            disabled={page === 0}
                            onClick={() => setPage((p) => p - 1)}
                        >
                            ← Prev
                        </button>
                        <span className="page-info">
                            Page {page + 1} of {totalPages || 1} ({totalCount} total)
                        </span>
                        <button
                            className="btn-page"
                            disabled={page >= totalPages - 1}
                            onClick={() => setPage((p) => p + 1)}
                        >
                            Next →
                        </button>
                    </div>
                </>
            )}
        </div>
    );
}
