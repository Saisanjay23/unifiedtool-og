/**
 * ClientManager — Full client management panel (sidebar component).
 * Inspired by old tool: dropdown selector, create/select toggle, delete with confirmation.
 */

import { useState, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { getClients, createClient, deleteClient } from '../api/client';
import useStore from '../store';

export default function ClientManager() {
    const queryClient = useQueryClient();
    const { selectedClient, setSelectedClient } = useStore();
    const [mode, setMode] = useState('select'); // 'select' | 'create'
    const [newName, setNewName] = useState('');
    const [deleteConfirm, setDeleteConfirm] = useState('');
    const [showDelete, setShowDelete] = useState(false);

    const { data, isLoading } = useQuery({
        queryKey: ['clients'],
        queryFn: async () => {
            const res = await getClients();
            return res.data.clients || [];
        },
        refetchInterval: 30000,
    });

    const clients = data || [];

    // (Removed auto-select useEffect as it races with deletions)

    const addMutation = useMutation({
        mutationFn: async (name) => {
            const res = await createClient(name);
            return res.data;
        },
        onSuccess: (_, name) => {
            queryClient.invalidateQueries({ queryKey: ['clients'] });
            setSelectedClient(name);
            setNewName('');
            setMode('select');
        },
    });

    const removeMutation = useMutation({
        mutationFn: async (name) => {
            await deleteClient(name);
            return name;
        },
        onSuccess: async (name) => {
            // Wait for DB to sync
            await queryClient.invalidateQueries({ queryKey: ['clients'] });

            // Get the updated list fresh from cache
            const currentClients = queryClient.getQueryData(['clients']) || [];

            if (selectedClient === name) {
                // Select the next available client, or null if list is empty
                setSelectedClient(currentClients.length > 0 ? currentClients[0] : null);
            }
            setDeleteConfirm('');
            setShowDelete(false);
        },
    });

    return (
        <div className="client-manager">
            <h3 className="client-manager__title">📂 Clients</h3>

            {/* Mode Toggle */}
            <div className="client-manager__toggle">
                <button
                    className={`toggle-btn ${mode === 'select' ? 'active' : ''}`}
                    onClick={() => setMode('select')}
                >
                    📂 Existing
                </button>
                <button
                    className={`toggle-btn ${mode === 'create' ? 'active' : ''}`}
                    onClick={() => setMode('create')}
                >
                    ➕ New
                </button>
            </div>

            {/* Select Existing Mode */}
            {mode === 'select' && (
                <div className="client-manager__select">
                    {isLoading ? (
                        <div className="loading-text">Loading clients...</div>
                    ) : clients.length === 0 ? (
                        <div className="empty-text">No clients yet. Create one.</div>
                    ) : (
                        <>
                            <select
                                className="client-dropdown"
                                value={selectedClient || ''}
                                onChange={(e) => setSelectedClient(e.target.value)}
                            >
                                <option value="" disabled>Select Client...</option>
                                {clients.map((c) => (
                                    <option key={c} value={c}>{c}</option>
                                ))}
                            </select>

                            {/* Delete Option */}
                            {selectedClient && (
                                <div className="client-delete-section">
                                    <button
                                        className="btn-delete-toggle"
                                        onClick={() => setShowDelete(!showDelete)}
                                    >
                                        🗑️ Delete
                                    </button>

                                    {showDelete && (
                                        <div className="delete-confirm-box">
                                            <span className="delete-warning">Type "{selectedClient}" to confirm:</span>
                                            <input
                                                type="text"
                                                className="delete-confirm-input"
                                                value={deleteConfirm}
                                                onChange={(e) => setDeleteConfirm(e.target.value)}
                                                placeholder={selectedClient}
                                            />
                                            <button
                                                className="btn-confirm-delete"
                                                disabled={deleteConfirm !== selectedClient || removeMutation.isPending}
                                                onClick={() => removeMutation.mutate(selectedClient)}
                                            >
                                                {removeMutation.isPending ? '...' : 'Confirm Delete'}
                                            </button>
                                        </div>
                                    )}
                                </div>
                            )}
                        </>
                    )}
                </div>
            )}

            {/* Create New Mode */}
            {mode === 'create' && (
                <div className="client-manager__create">
                    <input
                        type="text"
                        className="client-input"
                        placeholder="e.g. AcmeCorp"
                        value={newName}
                        onChange={(e) => setNewName(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter' && newName.trim()) {
                                addMutation.mutate(newName.trim());
                            }
                        }}
                    />
                    <button
                        className="btn-add-client"
                        disabled={!newName.trim() || addMutation.isPending}
                        onClick={() => addMutation.mutate(newName.trim())}
                    >
                        {addMutation.isPending ? '...' : '+ Add'}
                    </button>
                </div>
            )}

            {/* Active Client Badge */}
            {selectedClient && (
                <div className="active-client-badge">
                    📂 Active: <strong>{selectedClient}</strong>
                </div>
            )}
        </div>
    );
}
