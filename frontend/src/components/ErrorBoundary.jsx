import React from 'react';

/**
 * Global Error Boundary
 * Catches JavaScript errors anywhere in their child component tree,
 * logs those errors, and displays a fallback UI instead of crashing the whole app.
 */
class ErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false, error: null, errorInfo: null };
    }

    static getDerivedStateFromError(error) {
        return { hasError: true, error };
    }

    componentDidCatch(error, errorInfo) {
        this.setState({ errorInfo });
        console.error("ErrorBoundary caught an error:", error, errorInfo);
    }

    render() {
        if (this.state.hasError) {
            return (
                <div style={{
                    padding: 'var(--space-2xl)',
                    textAlign: 'center',
                    color: 'var(--text-primary)',
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    justifyContent: 'center',
                    minHeight: '100vh',
                    background: 'var(--bg-primary)',
                }}>
                    <div style={{
                        fontSize: 64,
                        marginBottom: 'var(--space-lg)',
                        animation: 'pulse-glow 2s ease-in-out infinite',
                    }}>
                        ⚠️
                    </div>
                    <h2 style={{
                        color: 'var(--accent-crimson)',
                        marginBottom: 'var(--space-md)',
                        fontSize: 24,
                        fontWeight: 700,
                    }}>
                        Something went wrong
                    </h2>
                    <p style={{
                        marginBottom: 'var(--space-lg)',
                        color: 'var(--text-secondary)',
                        maxWidth: 480,
                    }}>
                        The application encountered an unexpected frontend error.
                    </p>
                    <details style={{
                        whiteSpace: 'pre-wrap',
                        textAlign: 'left',
                        background: 'var(--bg-card)',
                        padding: 'var(--space-md)',
                        borderRadius: 'var(--radius-md)',
                        fontSize: 12,
                        overflowX: 'auto',
                        maxWidth: 600,
                        width: '100%',
                        border: '1px solid var(--border-subtle)',
                        color: 'var(--text-secondary)',
                    }}>
                        <summary style={{ cursor: 'pointer', marginBottom: 'var(--space-sm)', color: 'var(--text-primary)', fontWeight: 600 }}>View Error Details</summary>
                        {this.state.error && this.state.error.toString()}
                        <br />
                        {this.state.errorInfo && this.state.errorInfo.componentStack}
                    </details>
                    <button
                        className="btn btn-primary"
                        style={{ marginTop: 'var(--space-xl)' }}
                        onClick={() => window.location.reload()}
                    >
                        🔄 Reload Application
                    </button>
                </div>
            );
        }

        return this.props.children;
    }
}

export default ErrorBoundary;
