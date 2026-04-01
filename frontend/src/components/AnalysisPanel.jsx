/**
 * AnalysisPanel — profile detail viewer for analyzed results.
 * Shows profile card, stats, bio, and screenshot.
 */

export default function AnalysisPanel({ profile, onClose }) {
    if (!profile) {
        return (
            <div className="empty-state">
                <div className="icon">🔍</div>
                <div>Select a profile from the results to view analysis details</div>
            </div>
        );
    }

    return (
        <div className="card slide-up">
            <div className="card-header">
                <div className="card-title">Profile Analysis</div>
                {onClose && (
                    <button className="btn btn-secondary btn-sm" onClick={onClose}>✕</button>
                )}
            </div>

            {/* Profile header */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-lg)', marginBottom: 'var(--space-lg)' }}>
                {profile.profile_image_url ? (
                    <img
                        src={profile.profile_image_url}
                        alt={profile.display_name}
                        style={{ width: 80, height: 80, borderRadius: '50%', objectFit: 'cover', border: '3px solid var(--border-active)' }}
                    />
                ) : (
                    <div style={{ width: 80, height: 80, borderRadius: '50%', background: 'var(--bg-elevated)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 32, border: '3px solid var(--border-subtle)' }}>
                        👤
                    </div>
                )}
                <div>
                    <h2 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4 }}>
                        {profile.display_name || 'Unknown'}
                        {profile.is_verified && <span style={{ color: 'var(--accent-cyan)', marginLeft: 8 }}>✓</span>}
                    </h2>
                    <div style={{ color: 'var(--text-secondary)', fontSize: 14 }}>
                        @{profile.username}
                    </div>
                    <span className={`platform-${profile.platform}`} style={{ fontWeight: 600, fontSize: 12, textTransform: 'capitalize' }}>
                        {profile.platform}
                    </span>
                </div>
            </div>

            {/* Stats row */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--space-md)', marginBottom: 'var(--space-lg)' }}>
                <StatItem label="Followers" value={profile.followers} />
                <StatItem label="Following" value={profile.following} />
                <StatItem label="Posts" value={profile.post_count} />
                <StatItem label="Status" value={profile.status} isText />
            </div>

            {/* Bio */}
            {profile.bio && (
                <div style={{ marginBottom: 'var(--space-lg)' }}>
                    <div className="form-label">Bio</div>
                    <div style={{ padding: 'var(--space-md)', background: 'var(--bg-primary)', borderRadius: 'var(--radius-sm)', fontSize: 13, lineHeight: 1.6, color: 'var(--text-secondary)' }}>
                        {profile.bio}
                    </div>
                </div>
            )}

            {/* Details */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-md)', marginBottom: 'var(--space-lg)' }}>
                {profile.location && <DetailItem label="Location" value={profile.location} />}
                {profile.created_at && <DetailItem label="Joined" value={profile.created_at} />}
                {profile.last_active && <DetailItem label="Last Active" value={profile.last_active} />}
                {profile.url && (
                    <DetailItem
                        label="URL"
                        value={
                            <a href={profile.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent-cyan)', textDecoration: 'none', fontSize: 12 }}>
                                {profile.url.substring(0, 50)}...
                            </a>
                        }
                    />
                )}
            </div>

            {/* Screenshot */}
            {profile.screenshot_b64 && (
                <div>
                    <div className="form-label">Screenshot</div>
                    <img
                        src={`data:image/png;base64,${profile.screenshot_b64}`}
                        alt="Profile screenshot"
                        style={{ width: '100%', borderRadius: 'var(--radius-sm)', border: '1px solid var(--border-subtle)' }}
                    />
                </div>
            )}
        </div>
    );
}

function StatItem({ label, value, isText = false }) {
    const display = value != null
        ? isText ? value : value.toLocaleString()
        : '-';

    return (
        <div style={{ textAlign: 'center', padding: 'var(--space-sm)', background: 'var(--bg-primary)', borderRadius: 'var(--radius-sm)' }}>
            <div className="stat-value" style={{ fontSize: 18 }}>{display}</div>
            <div className="stat-label">{label}</div>
        </div>
    );
}

function DetailItem({ label, value }) {
    return (
        <div>
            <div className="form-label">{label}</div>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{value}</div>
        </div>
    );
}
