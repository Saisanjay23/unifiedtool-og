/**
 * SessionLogin — Per-platform session management.
 * Features: Interactive login launch, manual cookie import, logout, status display.
 * YouTube: API Key input. Telegram: API ID/Hash/Phone + interactive OTP login.
 */

import { useState, useEffect, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
    launchSession,
    importCookies,
    clearSession,
    getAllSessionStatuses,
    getCredentials,
    saveCredentials,
    sendTelegramCode,
    verifyTelegramCode,
    verifyTelegramPassword,
    validateSelectors,
} from '../api/client';

const PLATFORMS = [
    { id: 'facebook', name: 'Facebook', icon: '📘', color: '#1877F2' },
    { id: 'instagram', name: 'Instagram', icon: '📸', color: '#E1306C' },
    { id: 'twitter', name: 'Twitter', icon: '🐦', color: '#1DA1F2' },
    { id: 'youtube', name: 'YouTube', icon: '📺', color: '#FF0000' },
    { id: 'telegram', name: 'Telegram', icon: '✈️', color: '#0088CC' },
    { id: 'tiktok', name: 'TikTok', icon: '🎵', color: '#FE2C55' },
];

export default function SessionLogin() {
    const queryClient = useQueryClient();
    const [expandedPlatform, setExpandedPlatform] = useState(null);
    const [credentialsExpanded, setCredentialsExpanded] = useState(null);
    const [cookieInput, setCookieInput] = useState('');
    const [message, setMessage] = useState({ text: '', type: '' });

    const [isValidating, setIsValidating] = useState({});
    const [validationResults, setValidationResults] = useState({});

    const handleValidateSelectors = async (platformId) => {
        setIsValidating((prev) => ({ ...prev, [platformId]: true }));
        showMsg(`Running selector validation check for ${platformId.toUpperCase()}...`, 'warning');
        try {
            const res = await validateSelectors(platformId);
            const data = res.data.results[platformId];
            setValidationResults((prev) => ({ ...prev, [platformId]: data }));
            if (data.success) {
                showMsg(`${platformId.toUpperCase()} selectors are fully functional!`);
            } else {
                showMsg(`${platformId.toUpperCase()} has broken/degraded selectors. See report below.`, 'warning');
            }
        } catch (e) {
            showMsg(`Validation failed: ${e.message}`, 'error');
        } finally {
            setIsValidating((prev) => ({ ...prev, [platformId]: false }));
        }
    };

    // --- YouTube form state ---
    const [ytApiKey, setYtApiKey] = useState('');

    // --- Telegram form state ---
    const [tgApiId, setTgApiId] = useState('');
    const [tgApiHash, setTgApiHash] = useState('');
    const [tgPhone, setTgPhone] = useState('');
    const [tgAuthStep, setTgAuthStep] = useState('keys'); // 'keys' | 'code' | 'password'
    const [tgCode, setTgCode] = useState('');
    const [tgPassword, setTgPassword] = useState('');

    // Fetch all session statuses
    const { data: statuses } = useQuery({
        queryKey: ['sessions'],
        queryFn: getAllSessionStatuses,
        refetchInterval: 2000,
    });

    // Auto-clear messages
    useEffect(() => {
        if (message.text) {
            const timer = setTimeout(() => setMessage({ text: '', type: '' }), 6000);
            return () => clearTimeout(timer);
        }
    }, [message]);

    const showMsg = useCallback((text, type = 'success') => setMessage({ text, type }), []);

    // --- Mutations ---
    const loginMutation = useMutation({
        mutationFn: async (platform) => (await launchSession(platform)).data,
        onSuccess: (d) => { showMsg(d.message); queryClient.invalidateQueries({ queryKey: ['sessions'] }); },
        onError: (e) => showMsg(e.message, 'error'),
    });

    const cookieMutation = useMutation({
        mutationFn: async ({ platform, cookies }) => (await importCookies(platform, cookies)).data,
        onSuccess: (d) => { showMsg(d.message); setCookieInput(''); queryClient.invalidateQueries({ queryKey: ['sessions'] }); },
        onError: (e) => showMsg(e.message, 'error'),
    });

    const logoutMutation = useMutation({
        mutationFn: async (platform) => { await clearSession(platform); return platform; },
        onSuccess: (p) => { showMsg(`Logged out of ${p}`); queryClient.invalidateQueries({ queryKey: ['sessions'] }); },
        onError: (e) => showMsg(e.message, 'error'),
    });

    const saveCredentialsMutation = useMutation({
        mutationFn: async ({ platform, payload }) => (await saveCredentials(platform, payload)).data,
        onSuccess: (d, vars) => {
            showMsg(d.message);
            if (vars.platform !== 'telegram') setCredentialsExpanded(null);
            queryClient.invalidateQueries({ queryKey: ['sessions'] });
        },
        onError: (e) => showMsg(e.message, 'error'),
    });

    const sendCodeMutation = useMutation({
        mutationFn: async (payload) => (await sendTelegramCode(payload)).data,
        onSuccess: (d) => { showMsg(d.message); setTgAuthStep('code'); },
        onError: (e) => showMsg(e.message, 'error'),
    });

    const verifyCodeMutation = useMutation({
        mutationFn: async (payload) => (await verifyTelegramCode(payload)).data,
        onSuccess: (d) => {
            if (d.status === 'needs_password') {
                showMsg(d.message, 'warning');
                setTgAuthStep('password');
            } else {
                showMsg(d.message);
                setCredentialsExpanded(null);
                setTgAuthStep('keys');
                queryClient.invalidateQueries({ queryKey: ['sessions'] });
            }
        },
        onError: (e) => showMsg(e.message, 'error'),
    });

    const verifyPasswordMutation = useMutation({
        mutationFn: async (payload) => (await verifyTelegramPassword(payload)).data,
        onSuccess: (d) => {
            showMsg(d.message);
            setCredentialsExpanded(null);
            setTgAuthStep('keys');
            setTgPassword('');
            queryClient.invalidateQueries({ queryKey: ['sessions'] });
        },
        onError: (e) => showMsg(e.message, 'error'),
    });

    // --- Handlers ---
    const handleCookieImport = (platformId) => {
        try {
            const parsed = JSON.parse(cookieInput);
            if (!Array.isArray(parsed)) { showMsg('Cookies must be a JSON array', 'error'); return; }
            cookieMutation.mutate({ platform: platformId, cookies: parsed });
        } catch { showMsg('Invalid JSON format', 'error'); }
    };

    const handleOpenCredentials = async (platformId) => {
        if (credentialsExpanded === platformId) {
            setCredentialsExpanded(null);
            return;
        }
        try {
            const res = await getCredentials(platformId);
            const keys = res.data.keys;
            if (platformId === 'youtube') {
                setYtApiKey(keys.api_key || '');
            } else if (platformId === 'telegram') {
                setTgApiId(keys.api_id || '');
                setTgApiHash(keys.api_hash || '');
                setTgPhone(keys.phone || '');
            }
            setCredentialsExpanded(platformId);
        } catch {
            showMsg('Failed to load credentials', 'error');
        }
    };

    const handleSaveYouTube = () => {
        saveCredentialsMutation.mutate({ platform: 'youtube', payload: { api_key: ytApiKey } });
    };

    const handleSaveTelegram = () => {
        saveCredentialsMutation.mutate({
            platform: 'telegram',
            payload: { api_id: tgApiId, api_hash: tgApiHash, phone: tgPhone },
        });
    };

    const handleSaveAndSendCode = async () => {
        // First save the credentials, then send the code
        try {
            await saveCredentials('telegram', { api_id: tgApiId, api_hash: tgApiHash, phone: tgPhone });
            showMsg('Keys saved! Sending code...');
            sendCodeMutation.mutate({ phone: tgPhone });
        } catch (e) {
            showMsg(e.message, 'error');
        }
    };

    const formatAge = (hours) => {
        if (!hours) return '';
        if (hours < 1) return `${Math.round(hours * 60)}m ago`;
        if (hours < 24) return `${Math.round(hours)}h ago`;
        return `${Math.round(hours / 24)}d ago`;
    };

    return (
        <div className="session-manager">
            <h2 className="page-title">🔐 Session Manager</h2>
            <p className="page-subtitle">Manage browser sessions for each platform</p>

            {message.text && (
                <div className={`session-message session-message--${message.type}`}>
                    {message.type === 'success' ? '✅' : message.type === 'warning' ? '⚠️' : '❌'} {message.text}
                </div>
            )}

            <div className="session-grid">
                {PLATFORMS.map((platform) => {
                    const status = statuses?.[platform.id] || {};
                    const isLoggedIn = status.logged_in;
                    const isLoggingIn = status.login_in_progress;
                    const isExpanded = expandedPlatform === platform.id;

                    return (
                        <div
                            key={platform.id}
                            className={`session-card ${isLoggedIn ? 'session-card--active' : ''}`}
                            style={{ '--platform-color': platform.color }}
                        >
                            {/* Card Header */}
                            <div className="session-card__header">
                                <span className="session-card__icon">{platform.icon}</span>
                                <span className="session-card__name">{platform.name}</span>
                                <span className={`session-card__badge ${isLoggedIn ? 'badge--active' : 'badge--inactive'}`}>
                                    {isLoggingIn ? '⏳ Logging in...' : isLoggedIn ? '🟢 Active' : '🔴 Inactive'}
                                </span>
                            </div>

                            {isLoggedIn && (
                                <div className="session-card__details">
                                    <span className="detail-text">🕐 {formatAge(status.age_hours)}</span>
                                </div>
                            )}

                            {/* Action Buttons — Context-Aware */}
                            <div className="session-card__actions">
                                {isLoggedIn ? (
                                    <>
                                        {['youtube', 'telegram'].includes(platform.id) && (
                                            <button className="btn-session btn-session--login" onClick={() => handleOpenCredentials(platform.id)}>
                                                🔑 {credentialsExpanded === platform.id ? 'Close' : 'Configure API Keys'}
                                            </button>
                                        )}
                                        <button className="btn-session btn-session--logout" onClick={() => logoutMutation.mutate(platform.id)} disabled={logoutMutation.isPending}>
                                            🚪 Logout
                                        </button>
                                    </>
                                ) : (
                                    <>
                                        {['youtube', 'telegram'].includes(platform.id) && (
                                            <button className="btn-session btn-session--login" onClick={() => handleOpenCredentials(platform.id)}>
                                                🔑 {credentialsExpanded === platform.id ? 'Close' : 'Configure API Keys'}
                                            </button>
                                        )}
                                        {!['telegram', 'youtube'].includes(platform.id) && (
                                            <button className="btn-session btn-session--login" onClick={() => loginMutation.mutate(platform.id)} disabled={loginMutation.isPending || isLoggingIn}>
                                                {isLoggingIn ? '⏳ Opening...' : '🔓 Launch Login Browser'}
                                            </button>
                                        )}
                                        {!['telegram', 'youtube'].includes(platform.id) && (
                                            <button className="btn-session btn-session--cookie" onClick={() => setExpandedPlatform(isExpanded ? null : platform.id)}>
                                                🍪 {isExpanded ? 'Close' : 'Import Cookies'}
                                            </button>
                                        )}
                                    </>
                                )}
                                {['facebook', 'instagram', 'twitter'].includes(platform.id) && (
                                    <button className="btn-session" style={{ background: 'rgba(255, 255, 255, 0.05)', border: '1px solid rgba(255, 255, 255, 0.1)', color: 'var(--text-secondary)' }} onClick={() => handleValidateSelectors(platform.id)} disabled={isValidating[platform.id]}>
                                        {isValidating[platform.id] ? '⏳ Testing...' : '🔍 Test Selectors'}
                                    </button>
                                )}
                            </div>

                            {/* Selector Validation Results */}
                            {validationResults[platform.id] && (
                                <div className="session-card__cookie-panel" style={{ marginTop: '12px', borderTop: '1px solid rgba(255, 255, 255, 0.1)', paddingTop: '12px', width: '100%', boxSizing: 'border-box' }}>
                                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                                        <span style={{ fontSize: '12px', fontWeight: 'bold' }}>Selector Integrity Report</span>
                                        <span style={{ fontSize: '11px', fontWeight: '600', color: validationResults[platform.id].success ? '#2ecc71' : '#f1c40f' }}>
                                            {validationResults[platform.id].success ? '● PASS' : '⚠ DEGRADED'}
                                        </span>
                                    </div>
                                    <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '11px' }}>
                                        {Object.entries(validationResults[platform.id].metrics || {}).map(([field, data]) => (
                                            <div key={field} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', background: 'rgba(255, 255, 255, 0.03)', padding: '6px 8px', borderRadius: '4px', border: '1px solid rgba(255, 255, 255, 0.05)' }}>
                                                <span style={{ textTransform: 'capitalize', fontWeight: '500', color: 'var(--text-secondary)' }}>{field.replace('_', ' ')}</span>
                                                <span style={{ color: data.status === 'pass' ? '#2ecc71' : data.status === 'fail' ? '#e74c3c' : '#f1c40f', fontWeight: '600' }} title={data.details}>
                                                    {data.status === 'pass' ? '✓' : data.status === 'fail' ? '✗' : '⚠'} {data.status.toUpperCase()}
                                                </span>
                                            </div>
                                        ))}
                                    </div>
                                    <button className="btn-session" style={{ marginTop: '8px', background: 'transparent', border: 'none', color: 'var(--text-muted)', fontSize: '10px', width: '100%', textAlign: 'center', cursor: 'pointer', padding: '4px 0' }} onClick={() => {
                                        const newRes = { ...validationResults };
                                        delete newRes[platform.id];
                                        setValidationResults(newRes);
                                    }}>
                                        Clear Report
                                    </button>
                                </div>
                            )}

                            {/* Cookie Import Panel */}
                            {isExpanded && (
                                <div className="session-card__cookie-panel">
                                    <textarea className="cookie-textarea" placeholder='Paste cookies JSON array...' value={cookieInput} onChange={(e) => setCookieInput(e.target.value)} rows={4} />
                                    <button className="btn-session btn-session--save-cookies" onClick={() => handleCookieImport(platform.id)} disabled={!cookieInput.trim() || cookieMutation.isPending}>
                                        {cookieMutation.isPending ? '...' : '💾 Save Cookies'}
                                    </button>
                                </div>
                            )}

                            {/* YouTube Credentials Panel */}
                            {credentialsExpanded === platform.id && platform.id === 'youtube' && (
                                <div className="session-card__cookie-panel">
                                    <input type="text" className="cookie-textarea" placeholder="Paste your YouTube Data API Key here..." value={ytApiKey} onChange={(e) => setYtApiKey(e.target.value)} style={{ height: '40px', padding: '10px' }} />
                                    <button className="btn-session btn-session--save-cookies" onClick={handleSaveYouTube} disabled={saveCredentialsMutation.isPending || !ytApiKey} style={{ marginTop: '10px' }}>
                                        {saveCredentialsMutation.isPending ? '...' : '💾 Save API Key'}
                                    </button>
                                </div>
                            )}

                            {/* Telegram Credentials + Auth Panel */}
                            {credentialsExpanded === platform.id && platform.id === 'telegram' && (
                                <div className="session-card__cookie-panel">
                                    {tgAuthStep === 'keys' && (
                                        <>
                                            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                                                <input type="text" className="cookie-textarea" placeholder="API ID (number from my.telegram.org)" value={tgApiId} onChange={(e) => setTgApiId(e.target.value)} style={{ height: '40px', padding: '10px' }} />
                                                <input type="text" className="cookie-textarea" placeholder="API Hash (from my.telegram.org)" value={tgApiHash} onChange={(e) => setTgApiHash(e.target.value)} style={{ height: '40px', padding: '10px' }} />
                                                <input type="text" className="cookie-textarea" placeholder="Phone with country code (e.g. +918555915889)" value={tgPhone} onChange={(e) => setTgPhone(e.target.value)} style={{ height: '40px', padding: '10px' }} />
                                            </div>
                                            <div style={{ display: 'flex', gap: '10px', marginTop: '10px' }}>
                                                <button className="btn-session btn-session--save-cookies" onClick={handleSaveTelegram} disabled={saveCredentialsMutation.isPending || !tgApiId || !tgApiHash}>
                                                    {saveCredentialsMutation.isPending ? '...' : '💾 Save Keys'}
                                                </button>
                                                <button className="btn-session btn-session--login" onClick={handleSaveAndSendCode} disabled={sendCodeMutation.isPending || !tgApiId || !tgApiHash || !tgPhone} style={{ flexGrow: 1 }}>
                                                    {sendCodeMutation.isPending ? '⏳ Sending...' : '📱 Save & Request Code'}
                                                </button>
                                            </div>
                                        </>
                                    )}
                                    {tgAuthStep === 'code' && (
                                        <>
                                            <p style={{ color: '#27ae60', margin: '0 0 10px', fontSize: '14px' }}>✅ Code sent to your Telegram app! Enter it below:</p>
                                            <input type="text" className="cookie-textarea" placeholder="Enter the 5-digit code from Telegram" value={tgCode} onChange={(e) => setTgCode(e.target.value)} style={{ height: '40px', padding: '10px' }} />
                                            <div style={{ display: 'flex', gap: '10px', marginTop: '10px' }}>
                                                <button className="btn-session btn-session--save-cookies" onClick={() => setTgAuthStep('keys')} style={{ background: '#7f8c8d' }}>
                                                    🔙 Back
                                                </button>
                                                <button className="btn-session btn-session--save-cookies" onClick={() => verifyCodeMutation.mutate({ phone: tgPhone, code: tgCode })} disabled={verifyCodeMutation.isPending || !tgCode} style={{ flexGrow: 1 }}>
                                                    {verifyCodeMutation.isPending ? '⏳ Verifying...' : '✅ Verify Code'}
                                                </button>
                                            </div>
                                        </>
                                    )}
                                    {tgAuthStep === 'password' && (
                                        <>
                                            <p style={{ color: '#f39c12', margin: '0 0 10px', fontSize: '14px' }}>⚠️ 2FA enabled. Enter your cloud password:</p>
                                            <input type="password" className="cookie-textarea" placeholder="Enter your Telegram 2FA password" value={tgPassword} onChange={(e) => setTgPassword(e.target.value)} style={{ height: '40px', padding: '10px' }} />
                                            <div style={{ display: 'flex', gap: '10px', marginTop: '10px' }}>
                                                <button className="btn-session btn-session--save-cookies" onClick={() => setTgAuthStep('keys')} style={{ background: '#7f8c8d' }}>
                                                    🔙 Back
                                                </button>
                                                <button className="btn-session btn-session--save-cookies" onClick={() => verifyPasswordMutation.mutate({ phone: tgPhone, password: tgPassword })} disabled={verifyPasswordMutation.isPending || !tgPassword} style={{ flexGrow: 1 }}>
                                                    {verifyPasswordMutation.isPending ? '⏳ Verifying...' : '🔐 Submit Password'}
                                                </button>
                                            </div>
                                        </>
                                    )}
                                </div>
                            )}
                        </div>
                    );
                })}
            </div>
        </div>
    );
}
