/**
 * KeywordPresets — Saved keyword sets management.
 * Features: Load preset into search, create new preset, delete preset.
 * Inspired by old tool's saved_searches with select + load flow.
 */

import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { getPresets, savePreset, deletePreset } from '../api/client';
import useStore from '../store';

export default function KeywordPresets({ onLoadPreset, onAppendPreset }) {
    const { selectedClient, activePlatform } = useStore();
    const queryClient = useQueryClient();
    const [showManager, setShowManager] = useState(false);
    const [newName, setNewName] = useState('');
    const [newKeywords, setNewKeywords] = useState('');

    const { data: presets } = useQuery({
        queryKey: ['presets', selectedClient, activePlatform],
        queryFn: async () => {
            if (!selectedClient) return [];
            const res = await getPresets(selectedClient, activePlatform);
            return res.data.presets || [];
        },
        enabled: !!selectedClient,
    });

    const saveMutation = useMutation({
        mutationFn: async () => {
            const keywords = newKeywords.split(/[,\n]/).map(k => k.trim()).filter(Boolean);
            await savePreset(selectedClient, activePlatform, newName.trim(), keywords);
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['presets'] });
            setNewName('');
            setNewKeywords('');
        },
    });

    const deleteMutation = useMutation({
        mutationFn: async (presetName) => {
            await deletePreset(selectedClient, activePlatform, presetName);
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['presets'] });
        },
    });

    if (!selectedClient) return null;

    return (
        <div className="keyword-presets">
            {/* Quick Load Bar */}
            {presets && presets.length > 0 && (
                <div className="preset-load-bar">
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                        {presets.map((p) => (
                            <div key={p.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 0, borderRadius: 'var(--radius-full)', overflow: 'hidden', border: '1px solid var(--border-subtle)' }}>
                                <button
                                    className="btn-preset-chip"
                                    onClick={() => onLoadPreset && onLoadPreset(p.keywords.join('\n'))}
                                    title={`Load "${p.preset_name}" (replaces current)`}
                                    style={{ padding: '4px 10px', fontSize: 11, fontWeight: 500, background: 'var(--accent-cyan-dim)', color: 'var(--text-secondary)', border: 'none', cursor: 'pointer', fontFamily: 'var(--font-ui)' }}
                                >
                                    📂 {p.preset_name}
                                </button>
                                {onAppendPreset && (
                                    <button
                                        className="btn-preset-append"
                                        onClick={() => onAppendPreset(p.keywords.join('\n'))}
                                        title={`Append "${p.preset_name}" to current keywords`}
                                        style={{ padding: '4px 8px', fontSize: 11, fontWeight: 700, background: 'rgba(0,212,255,0.08)', color: 'var(--accent-cyan)', border: 'none', borderLeft: '1px solid var(--border-subtle)', cursor: 'pointer', fontFamily: 'var(--font-ui)' }}
                                    >
                                        +
                                    </button>
                                )}
                            </div>
                        ))}
                    </div>
                </div>
            )}

            {/* Manager Toggle */}
            <button
                className="btn-manage-presets"
                onClick={() => setShowManager(!showManager)}
            >
                🛠️ {showManager ? 'Close' : 'Manage'} Presets
            </button>

            {/* Manager Panel */}
            {showManager && (
                <div className="preset-manager">
                    <h4>💾 Create New Preset</h4>
                    <div className="preset-create-row">
                        <input
                            type="text"
                            className="preset-name-input"
                            placeholder="Preset name..."
                            value={newName}
                            onChange={(e) => setNewName(e.target.value)}
                        />
                        <textarea
                            className="preset-keywords-input"
                            placeholder="keyword1, keyword2, keyword3..."
                            value={newKeywords}
                            onChange={(e) => setNewKeywords(e.target.value)}
                            rows={2}
                        />
                        <button
                            className="btn-save-preset"
                            disabled={!newName.trim() || !newKeywords.trim() || saveMutation.isPending}
                            onClick={() => saveMutation.mutate()}
                        >
                            ➕ Save
                        </button>
                    </div>

                    {/* Delete Presets */}
                    {presets && presets.length > 0 && (
                        <div className="preset-delete-section">
                            <h4>🗑️ Delete Presets</h4>
                            {presets.map((p) => (
                                <div key={p.id} className="preset-delete-row">
                                    <span className="preset-label">{p.preset_name}</span>
                                    <span className="preset-count">{p.keywords.length} keywords</span>
                                    <button
                                        className="btn-delete-preset"
                                        onClick={() => deleteMutation.mutate(p.preset_name)}
                                        disabled={deleteMutation.isPending}
                                    >
                                        🗑️
                                    </button>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
