/**
 * Toast — Global notification system.
 * Provides a useToast hook and ToastContainer component for
 * surfacing success/error/info messages to the user.
 */

import { useState, useCallback, useEffect, createContext, useContext } from 'react';

// Toast context
const ToastContext = createContext(null);

let toastIdCounter = 0;

export function ToastProvider({ children }) {
    const [toasts, setToasts] = useState([]);

    const addToast = useCallback((message, type = 'info', duration = 4000) => {
        const id = ++toastIdCounter;
        setToasts((prev) => [...prev, { id, message, type, duration }]);
        return id;
    }, []);

    const removeToast = useCallback((id) => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
    }, []);

    const toast = {
        success: (msg, dur) => addToast(msg, 'success', dur),
        error: (msg, dur) => addToast(msg, 'error', dur ?? 6000),
        info: (msg, dur) => addToast(msg, 'info', dur),
        warning: (msg, dur) => addToast(msg, 'warning', dur ?? 5000),
    };

    return (
        <ToastContext.Provider value={toast}>
            {children}
            <ToastContainer toasts={toasts} removeToast={removeToast} />
        </ToastContext.Provider>
    );
}

export function useToast() {
    const ctx = useContext(ToastContext);
    if (!ctx) throw new Error('useToast must be used within <ToastProvider>');
    return ctx;
}

/* ─── Toast Container ─── */
function ToastContainer({ toasts, removeToast }) {
    return (
        <div style={{
            position: 'fixed',
            top: 16,
            right: 16,
            zIndex: 9999,
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
            pointerEvents: 'none',
            maxWidth: 380,
        }}>
            {toasts.map((t) => (
                <ToastItem key={t.id} toast={t} onDismiss={() => removeToast(t.id)} />
            ))}
        </div>
    );
}

/* ─── Single Toast ─── */
function ToastItem({ toast, onDismiss }) {
    const [exiting, setExiting] = useState(false);

    useEffect(() => {
        const timer = setTimeout(() => {
            setExiting(true);
            setTimeout(onDismiss, 300);
        }, toast.duration);
        return () => clearTimeout(timer);
    }, [toast.duration, onDismiss]);

    const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
    const colors = {
        success: 'var(--accent-emerald)',
        error: 'var(--accent-crimson)',
        warning: 'var(--accent-amber)',
        info: 'var(--accent-cyan)',
    };

    return (
        <div
            style={{
                pointerEvents: 'auto',
                background: 'var(--bg-secondary)',
                border: `1px solid ${colors[toast.type]}`,
                borderLeft: `4px solid ${colors[toast.type]}`,
                borderRadius: 'var(--radius-md, 8px)',
                padding: '12px 16px',
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                boxShadow: '0 8px 32px rgba(0,0,0,0.3)',
                backdropFilter: 'blur(12px)',
                animation: exiting ? 'toast-exit 0.3s ease forwards' : 'toast-enter 0.3s ease forwards',
                fontSize: 13,
                color: 'var(--text-primary)',
                cursor: 'pointer',
            }}
            onClick={() => { setExiting(true); setTimeout(onDismiss, 300); }}
        >
            <span style={{ fontSize: 16, flexShrink: 0 }}>{icons[toast.type]}</span>
            <span style={{ flex: 1, lineHeight: 1.4 }}>{toast.message}</span>
            <span style={{ color: 'var(--text-muted)', fontSize: 16, flexShrink: 0, marginLeft: 4 }}>×</span>
        </div>
    );
}
