/**
 * PlatformView — unified per-platform workspace with INDEPENDENT tabs.
 * Discovery mode: keyword search → launch job → monitor → results grid.
 * Analysis mode: URL input → launch job → monitor → results grid + detail overlay.
 * Each mode maintains its own independent state.
 */

import React, { useState, useCallback } from 'react';
import { createJob } from '../api/client';
import useStore from '../store';
import JobMonitor from './JobMonitor';
import ResultsGrid from './ResultsGrid';
import AnalysisPanel from './AnalysisPanel';
import KeywordPresets from './KeywordPresets';
import AnalysisResultsGrid from './AnalysisResultsGrid';
import PlatformIcon from './PlatformIcon';

const PLATFORM_NAMES = {
    facebook: 'Facebook',
    instagram: 'Instagram',
    twitter: 'Twitter / X',
    youtube: 'YouTube',
    telegram: 'Telegram',
    tiktok: 'TikTok',
};

const PLATFORM_ICONS = {
    facebook: '📘',
    instagram: '📸',
    twitter: '🐦',
    youtube: '📺',
    telegram: '✈️',
    tiktok: '🎵',
};

const ANALYSIS_PLACEHOLDERS = {
    facebook: 'Enter Facebook profile or page URLs...',
    instagram: 'Enter Instagram profile URLs...',
    twitter: 'Enter Twitter/X profile URLs...',
    youtube: 'Enter YouTube channel URLs...',
    telegram: 'Enter Telegram channel or group URLs...',
    tiktok: 'Enter TikTok profile URLs...',
};

// ─── Discovery Tab (fully independent state) ────────────────────────────────

function DiscoveryTab({ activePlatform, selectedClient, onAnalyzeUrls }) {
    const addJob = useStore((s) => s.addJob);
    const [keywords, setKeywords] = useState('');
    const [maxResults, setMaxResults] = useState(50);
    const [headless, setHeadless] = useState(true);
    const [activeJobId, setActiveJobId] = useState(null);
    const [launching, setLaunching] = useState(false);
    const [error, setError] = useState('');
    const [searchType, setSearchType] = useState('people'); // people | pages | both
    const [scrapeAll, setScrapeAll] = useState(false);
    const [liveResults, setLiveResults] = useState([]);

    // Clear live results when switching clients to prevent data leaks
    React.useEffect(() => {
        setLiveResults([]);
        setActiveJobId(null);
        setError('');
    }, [selectedClient, activePlatform]);

    const handleLaunch = useCallback(async () => {
        if (!selectedClient) { setError('Select a client first'); return; }
        const kwList = keywords.split('\n').map((k) => k.trim()).filter(Boolean);
        if (kwList.length === 0) { setError('Enter at least one keyword'); return; }

        setError('');
        setLaunching(true);
        try {
            const res = await createJob({
                platform: activePlatform,
                mode: 'discovery',
                client: selectedClient,
                keywords: kwList,
                headless,
                max_results: scrapeAll ? 99999 : maxResults,
                search_type: activePlatform === 'facebook' ? searchType : 'people',
            });
            setActiveJobId(res.data.job_id);
            setLiveResults([]);  // Clear live results for new job
            addJob(res.data.job_id, { platform: activePlatform, mode: 'discovery', status: 'queued', client: selectedClient });
        } catch (err) {
            setError(err.message);
        } finally {
            setLaunching(false);
        }
    }, [selectedClient, keywords, maxResults, headless, activePlatform, addJob, searchType, scrapeAll]);

    const searchTypeOptions = [
        { id: 'people', label: '👤 People' },
        { id: 'pages', label: '📄 Pages' },
        { id: 'both', label: '🔀 Both' },
    ];

    return (
        <>
            <div className="card" style={{ marginBottom: 'var(--space-md)' }}>
                <div className="card-header">
                    <div className="card-title">Search Keywords</div>
                </div>

                <div className="form-group">
                    <label className="form-label">Enter keywords (one per line):</label>
                    <KeywordPresets
                        onLoadPreset={(kws) => setKeywords(kws)}
                        onAppendPreset={(kws) => setKeywords(prev => prev ? prev.trim() + '\n' + kws : kws)}
                    />
                    <textarea
                        className="form-textarea"
                        placeholder={'Enter keywords (one per line)...'}
                        value={keywords}
                        onChange={(e) => setKeywords(e.target.value)}
                        rows={4}
                    />
                </div>

                {/* People / Pages / Both  (Facebook only) */}
                {activePlatform === 'facebook' && (
                    <div className="form-group">
                        <label className="form-label">Search for:</label>
                        <div className="mode-tabs" style={{ maxWidth: 340, marginBottom: 0 }}>
                            {searchTypeOptions.map((opt) => (
                                <button
                                    key={opt.id}
                                    className={`mode-tab ${searchType === opt.id ? 'active' : ''}`}
                                    onClick={() => setSearchType(opt.id)}
                                >
                                    {opt.label}
                                </button>
                            ))}
                        </div>
                    </div>
                )}

                <div style={{ display: 'flex', gap: 'var(--space-lg)', alignItems: 'flex-end', flexWrap: 'wrap' }}>
                    {/* Count input — hidden when Scrape All is on */}
                    {!scrapeAll && (
                        <div className="form-group" style={{ maxWidth: 200 }}>
                            <label className="form-label">Max results per keyword:</label>
                            <input
                                className="form-input"
                                type="number"
                                min={1}
                                max={500}
                                value={maxResults}
                                onChange={(e) => setMaxResults(Number(e.target.value))}
                            />
                        </div>
                    )}

                    {/* Scrape All toggle */}
                    <div className="form-group" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <input
                            type="checkbox"
                            id="scrape-all-toggle"
                            checked={scrapeAll}
                            onChange={(e) => setScrapeAll(e.target.checked)}
                            style={{ width: 16, height: 16, cursor: 'pointer' }}
                        />
                        <label htmlFor="scrape-all-toggle" className="form-label" style={{ margin: 0, cursor: 'pointer' }}
                            title="Scrape all available results until the feed ends. No count limit."
                        >
                            Scrape All
                        </label>
                    </div>

                    <div className="form-group" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                        <input
                            type="checkbox"
                            id="headless-toggle-disc"
                            checked={headless}
                            onChange={(e) => setHeadless(e.target.checked)}
                            style={{ width: 16, height: 16, cursor: 'pointer' }}
                        />
                        <label htmlFor="headless-toggle-disc" className="form-label" style={{ margin: 0, cursor: 'pointer' }}
                            title="When checked, the browser runs invisibly. Uncheck to see the browser during scraping."
                        >
                            Headless Mode
                        </label>
                    </div>
                </div>

                {error && (
                    <div style={{ color: 'var(--accent-crimson)', fontSize: 12, marginBottom: 'var(--space-sm)' }}>
                        ⚠ {error}
                    </div>
                )}

                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <button className="btn btn-primary" onClick={handleLaunch} disabled={launching}>
                        {launching ? 'Launching...' : '🚀 Start Discovery'}
                    </button>
                    {selectedClient && (
                        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                            Client: <strong style={{ color: 'var(--text-secondary)' }}>{selectedClient}</strong>
                        </span>
                    )}
                </div>
            </div>

            {activeJobId && (
                <JobMonitor
                    key={activeJobId}
                    jobId={activeJobId}
                    onClose={() => { setActiveJobId(null); setLiveResults([]); }}
                    onResultFound={(res) => setLiveResults(prev => [...prev, res])}
                />
            )}

            <ResultsGrid 
                activePlatform={activePlatform} 
                liveResults={liveResults} 
                hasActiveJob={!!activeJobId} 
                onAnalyzeUrls={onAnalyzeUrls} 
                activeJobKeywords={activeJobId ? keywords.split('\n').map((k) => k.trim()).filter(Boolean) : []}
            />
        </>
    );
}

// ─── Analysis Tab (fully independent state) ─────────────────────────────────

function AnalysisTab({ activePlatform, selectedClient, initialUrls }) {
    const addJob = useStore((s) => s.addJob);
    const [urls, setUrls] = useState(initialUrls || '');
    const [headless, setHeadless] = useState(true);
    const [activeJobId, setActiveJobId] = useState(null);
    const [analysisResults, setAnalysisResults] = useState([]); // IN-MEMORY RESULTS
    const [launching, setLaunching] = useState(false);
    const [error, setError] = useState('');

    // Update URLs when initialUrls changes (from Discovery → Analyze flow)
    React.useEffect(() => {
        if (initialUrls) setUrls(initialUrls);
    }, [initialUrls]);

    const handleLaunch = useCallback(async () => {
        if (!selectedClient) { setError('Select a client first'); return; }
        const urlList = urls.split('\n').map((u) => u.trim()).filter(Boolean);
        if (urlList.length === 0) { setError('Enter at least one URL'); return; }

        setError('');
        setLaunching(true);
        setAnalysisResults([]); // Clear previous in-memory results on new run

        try {
            const res = await createJob({
                platform: activePlatform,
                mode: 'analysis',
                client: selectedClient,
                keywords: urlList,  // backend uses "keywords" field for URLs in analysis mode
                headless,
            });
            setActiveJobId(res.data.job_id);
            addJob(res.data.job_id, { platform: activePlatform, mode: 'analysis', status: 'queued', client: selectedClient });
        } catch (err) {
            setError(err.message);
        } finally {
            setLaunching(false);
        }
    }, [selectedClient, urls, headless, activePlatform, addJob]);

    return (
        <>
            <div className="card" style={{ marginBottom: 'var(--space-md)' }}>
                <div className="card-header">
                    <div className="card-title">Profile URLs</div>
                </div>

                <div className="form-group">
                    <label className="form-label">Enter profile URLs to analyze (one per line):</label>
                    <textarea
                        className="form-textarea"
                        placeholder={ANALYSIS_PLACEHOLDERS[activePlatform] || 'Enter profile URLs (one per line)'}
                        value={urls}
                        onChange={(e) => setUrls(e.target.value)}
                        rows={4}
                    />
                </div>

                <div className="form-group" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <input
                        type="checkbox"
                        id="headless-toggle-anal"
                        checked={headless}
                        onChange={(e) => setHeadless(e.target.checked)}
                        style={{ width: 16, height: 16, cursor: 'pointer' }}
                    />
                    <label htmlFor="headless-toggle-anal" className="form-label" style={{ margin: 0, cursor: 'pointer' }}
                        title="When checked, the browser runs invisibly. Uncheck to see the browser during scraping."
                    >
                        Headless Mode
                    </label>
                </div>

                {error && (
                    <div style={{ color: 'var(--accent-crimson)', fontSize: 12, marginBottom: 'var(--space-sm)' }}>
                        ⚠ {error}
                    </div>
                )}

                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <button className="btn btn-primary" onClick={handleLaunch} disabled={launching}>
                        {launching ? 'Launching...' : '🔬 Start Analysis'}
                    </button>
                    {selectedClient && (
                        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                            Client: <strong style={{ color: 'var(--text-secondary)' }}>{selectedClient}</strong>
                        </span>
                    )}
                </div>
            </div>

            {activeJobId && (
                <JobMonitor
                    key={activeJobId}
                    jobId={activeJobId}
                    onClose={() => setActiveJobId(null)}
                    onResultFound={(res) => setAnalysisResults(prev => [...prev, res])}
                />
            )}

            {/* Results grid with 3-column editable old-tool UI - Pure In-Memory Now */}
            <AnalysisResultsGrid
                activePlatform={activePlatform}
                inMemoryResults={analysisResults}
                setInMemoryResults={setAnalysisResults}
            />
        </>
    );
}

// ─── Platform View (parent with mode switcher) ──────────────────────────────

export default function PlatformView({ platformId }) {
    const [activeMode, setActiveMode] = useState('discovery');
    const [pendingAnalysisUrls, setPendingAnalysisUrls] = useState('');
    const selectedClient = useStore((s) => s.selectedClient);

    // Called from Discovery's Validated tab → switches to Analysis and pre-fills URLs
    const handleAnalyzeUrls = React.useCallback((urls) => {
        setPendingAnalysisUrls(urls.join('\n'));
        setActiveMode('analysis');
    }, []);

    return (
        <div>
            {/* Platform header */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-md)', marginBottom: 'var(--space-lg)' }}>
                <span style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    <PlatformIcon platform={platformId} size={28} />
                </span>
                <div>
                    <h1 style={{ fontSize: 22, fontWeight: 700 }}>{PLATFORM_NAMES[platformId]}</h1>
                    <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                        {activeMode === 'discovery' ? 'Keyword-based profile discovery' : 'Deep profile analysis from URLs'}
                    </div>
                </div>
            </div>

            {/* Mode switcher */}
            <div className="mode-tabs">
                <button className={`mode-tab ${activeMode === 'discovery' ? 'active' : ''}`} onClick={() => setActiveMode('discovery')}>
                    🔍 Discovery
                </button>
                <button className={`mode-tab ${activeMode === 'analysis' ? 'active' : ''}`} onClick={() => setActiveMode('analysis')}>
                    📊 Analysis
                </button>
            </div>

            {/* Tab content — Discovery passes analyze callback, Analysis receives pre-filled URLs */}
            <div style={{ display: activeMode === 'discovery' ? 'block' : 'none' }}>
                <DiscoveryTab activePlatform={platformId} selectedClient={selectedClient} onAnalyzeUrls={handleAnalyzeUrls} />
            </div>
            <div style={{ display: activeMode === 'analysis' ? 'block' : 'none' }}>
                <AnalysisTab activePlatform={platformId} selectedClient={selectedClient} initialUrls={pendingAnalysisUrls} />
            </div>
        </div>
    );
}
