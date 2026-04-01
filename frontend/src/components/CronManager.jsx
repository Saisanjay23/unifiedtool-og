/**
 * CronManager — scheduled job management UI.
 * Displays active cron jobs configured via the backend.
 */

import { useQuery } from '@tanstack/react-query';
import { getHealth } from '../api/client';

export default function CronManager() {
    // cron info comes with health endpoint (could be separate)
    const { data } = useQuery({
        queryKey: ['health'],
        queryFn: async () => {
            const res = await getHealth();
            return res.data;
        },
        refetchInterval: 30000,
    });

    return (
        <div className="card">
            <div className="card-header">
                <div className="card-title">Scheduled Jobs</div>
            </div>

            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 'var(--space-md)' }}>
                Cron jobs are defined in <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>cron_jobs.json</span>.
                The scheduler auto-reloads on file changes.
            </div>

            <div style={{ marginBottom: 'var(--space-md)' }}>
                <div className="form-label">Example cron_jobs.json:</div>
                <pre style={{
                    background: 'var(--bg-primary)',
                    padding: 'var(--space-md)',
                    borderRadius: 'var(--radius-sm)',
                    fontSize: 11,
                    fontFamily: 'var(--font-mono)',
                    color: 'var(--text-secondary)',
                    overflow: 'auto',
                }}>
                    {`[
  {
    "job_id": "daily_instagram_scan",
    "client": "client_alpha",
    "platforms": ["instagram"],
    "keywords": ["cybersecurity", "infosec"],
    "schedule": "0 8 * * *",
    "max_results": 50
  }
]`}
                </pre>
            </div>

            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                <table className="data-table">
                    <thead>
                        <tr>
                            <th>Field</th>
                            <th>Description</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr><td>job_id</td><td>Unique identifier for the job</td></tr>
                        <tr><td>client</td><td>Client name to associate results with</td></tr>
                        <tr><td>platforms</td><td>Array of platforms to search</td></tr>
                        <tr><td>keywords</td><td>Search keywords</td></tr>
                        <tr><td>schedule</td><td>Cron expression (minute hour day month weekday)</td></tr>
                        <tr><td>max_results</td><td>Max results per keyword per platform</td></tr>
                    </tbody>
                </table>
            </div>

            <div style={{ marginTop: 'var(--space-md)', color: 'var(--text-muted)', fontSize: 11 }}>
                To start the cron scheduler: <code style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent-cyan)' }}>python home.py --cron</code>
            </div>
        </div>
    );
}
