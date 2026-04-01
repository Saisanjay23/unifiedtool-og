/**
 * HealthRing — animated SVG gauge with platform-specific colored glow
 * and real-time session status awareness.
 */

import { useMemo } from 'react';

const RADIUS = 40;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

function getStatusColor(score, platformColor) {
    if (platformColor) return platformColor;
    if (score >= 0.7) return 'var(--status-healthy)';
    if (score >= 0.4) return 'var(--status-degraded)';
    return 'var(--status-critical)';
}

function getGlowColor(score, platformColor) {
    if (platformColor) return platformColor + '40'; // 25% opacity hex
    if (score >= 0.7) return 'rgba(0, 255, 136, 0.25)';
    if (score >= 0.4) return 'rgba(255, 184, 0, 0.25)';
    return 'rgba(255, 59, 107, 0.25)';
}

function getStatusLabel(status, sessionStatus) {
    // If session data is available, show session-aware labels
    if (sessionStatus) {
        if (sessionStatus.login_in_progress) return 'Connecting...';
        if (sessionStatus.session_expired) return 'Expired';
        if (!sessionStatus.logged_in) return 'No Session';
    }
    const labels = {
        healthy: 'Healthy',
        degraded: 'Degraded',
        critical: 'Critical',
        suspended: 'Suspended',
        expired: 'Expired',
    };
    return labels[status] || status;
}

function getEffectiveStatus(status, sessionStatus) {
    if (sessionStatus && sessionStatus.session_expired) {
        return 'expired';
    }
    if (sessionStatus && !sessionStatus.logged_in && !sessionStatus.login_in_progress) {
        return 'suspended';
    }
    return status;
}

export default function HealthRing({ score = 0, status = 'healthy', label = '', requestCount = 0, platformColor, sessionStatus }) {
    // Adjust the displayed score based on session status
    const effectiveScore = (sessionStatus && (!sessionStatus.logged_in || sessionStatus.session_expired) && !sessionStatus.login_in_progress) ? 0 : score;
    const effectiveStatus = getEffectiveStatus(status, sessionStatus);

    const percentage = Math.round(effectiveScore * 100);
    const dashOffset = CIRCUMFERENCE * (1 - effectiveScore);
    const color = (effectiveStatus === 'suspended' || effectiveStatus === 'expired') ? 'var(--status-suspended, #666)' : getStatusColor(effectiveScore, platformColor);
    const glowColor = getGlowColor(effectiveScore, platformColor);

    const ringStyle = useMemo(() => ({
        strokeDasharray: CIRCUMFERENCE,
        strokeDashoffset: dashOffset,
        transition: 'stroke-dashoffset 1.2s cubic-bezier(0.4, 0, 0.2, 1)',
        transform: 'rotate(-90deg)',
        transformOrigin: '50% 50%',
        filter: `drop-shadow(0 0 8px ${glowColor})`,
    }), [dashOffset, glowColor]);

    // Build tooltip text
    const tooltipParts = [label || 'Platform'];
    if (sessionStatus) {
        if (sessionStatus.logged_in) tooltipParts.push('✓ Session active');
        else tooltipParts.push('✗ No active session');
        if (sessionStatus.session_expired) tooltipParts.push('⚠ Session expired');
        if (sessionStatus.age_hours) {
            const h = sessionStatus.age_hours;
            const ageStr = h < 1 ? `${Math.round(h * 60)}m` : h < 24 ? `${Math.round(h)}h` : `${Math.round(h / 24)}d`;
            tooltipParts.push(`Session age: ${ageStr}`);
        }
    }
    tooltipParts.push(`Health: ${percentage}%`);
    const tooltipText = tooltipParts.join('\n');

    return (
        <div style={{ textAlign: 'center', cursor: 'default' }} title={tooltipText}>
            <svg width="100" height="100" viewBox="0 0 100 100">
                {/* Outer glow ring */}
                <circle
                    cx="50" cy="50" r={RADIUS + 4}
                    fill="none"
                    stroke={color}
                    strokeWidth="1"
                    opacity="0.15"
                />
                {/* background ring */}
                <circle
                    cx="50" cy="50" r={RADIUS}
                    fill="none"
                    stroke="var(--bg-primary)"
                    strokeWidth="6"
                    opacity="0.6"
                />
                {/* score ring */}
                <circle
                    cx="50" cy="50" r={RADIUS}
                    fill="none"
                    stroke={color}
                    strokeWidth="6"
                    strokeLinecap="round"
                    style={ringStyle}
                />
                {/* percentage text */}
                <text
                    x="50" y="46"
                    textAnchor="middle"
                    fontFamily="var(--font-mono)"
                    fontSize="22"
                    fontWeight="800"
                    fill="var(--text-primary)"
                >
                    {percentage}%
                </text>
            </svg>
            {label && (
                <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-secondary)', marginTop: 2 }}>
                    {label}
                </div>
            )}
            <div style={{ marginTop: 4 }}>
                <span className={`badge badge-${effectiveStatus}`} style={{ fontSize: 10 }}>
                    {getStatusLabel(effectiveStatus, sessionStatus)}
                </span>
            </div>
        </div>
    );
}
