'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { useToast } from '@/context/ToastContext';

interface Lawyer {
    id: number;
    full_name: string;
    email: string;
    specialization: string;
}

export default function NewCasePage() {
    const router = useRouter();
    const [lawyers, setLawyers] = useState<Lawyer[]>([]);
    const [loading, setLoading] = useState(false);
    const { showToast } = useToast();
    const [formData, setFormData] = useState({
        title: '',
        description: '',
        defendant_name: '',
        case_type: 'Fraud',
        status: 'Open',
        lead_attorney_id: ''
    });

    useEffect(() => {
        fetch('/api/lawyers')
            .then(res => res.json())
            .then(data => {
                setLawyers(data);
                if (data.length > 0) {
                    setFormData(prev => ({ ...prev, lead_attorney_id: data[0].id.toString() }));
                }
            })
            .catch(err => console.error('Failed to fetch lawyers', err));
    }, []);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setLoading(true);

        try {
            const res = await fetch('/api/cases', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    ...formData,
                    lead_attorney_id: parseInt(formData.lead_attorney_id)
                })
            });

            if (res.ok) {
                const newCase = await res.json();
                router.push(`/cases/${newCase.id}`);
            } else {
                const error = await res.json();
                showToast(`Error: ${error.detail || 'Failed to create case'}`, 'error');
            }
        } catch (err) {
            showToast('Error creating case', 'error');
        } finally {
            setLoading(false);
        }
    };

    return (
        <main className="min-h-screen bg-slate-950 text-slate-100 p-8">
            <Link href="/" className="text-amber-500 hover:text-amber-400 mb-8 inline-block">&larr; Back to Dashboard</Link>

            <div className="max-w-3xl mx-auto">
                <header className="mb-8 border-b border-slate-800 pb-4">
                    <h1 className="text-3xl font-bold text-white">Create New Case</h1>
                    <p className="text-slate-400">Enter the details for the new prosecution case</p>
                </header>

                <form onSubmit={handleSubmit} className="bg-slate-900/50 border border-slate-800 rounded-xl p-8 space-y-6">
                    <div>
                        <label className="block text-sm font-medium text-slate-300 mb-2">Case Title *</label>
                        <input
                            type="text"
                            value={formData.title}
                            onChange={(e) => setFormData({ ...formData, title: e.target.value })}
                            placeholder="e.g., The King v. Smith - Fraud"
                            className="w-full bg-slate-950 border border-slate-700 rounded-lg px-4 py-2 text-white focus:outline-none focus:border-amber-500 transition"
                            required
                        />
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className="block text-sm font-medium text-slate-300 mb-2">Case Type *</label>
                            <select
                                value={formData.case_type}
                                onChange={(e) => setFormData({ ...formData, case_type: e.target.value })}
                                className="w-full bg-slate-950 border border-slate-700 rounded-lg px-4 py-2 text-white focus:outline-none focus:border-amber-500 transition"
                                required
                            >
                                <option value="Fraud">Fraud</option>
                                <option value="Homicide">Homicide</option>
                                <option value="Theft">Theft</option>
                                <option value="Assault">Assault</option>
                                <option value="Cybercrime">Cybercrime</option>
                                <option value="Narcotics">Narcotics</option>
                            </select>
                        </div>

                        <div>
                            <label className="block text-sm font-medium text-slate-300 mb-2">Status *</label>
                            <select
                                value={formData.status}
                                onChange={(e) => setFormData({ ...formData, status: e.target.value })}
                                className="w-full bg-slate-950 border border-slate-700 rounded-lg px-4 py-2 text-white focus:outline-none focus:border-amber-500 transition"
                                required
                            >
                                <option value="Open">Open</option>
                                <option value="Under Investigation">Under Investigation</option>
                                <option value="Pending Trial">Pending Trial</option>
                                <option value="Closed">Closed</option>
                                <option value="Dismissed">Dismissed</option>
                            </select>
                        </div>
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-slate-300 mb-2">Defendant Name *</label>
                        <input
                            type="text"
                            value={formData.defendant_name}
                            onChange={(e) => setFormData({ ...formData, defendant_name: e.target.value })}
                            placeholder="Full name of the defendant"
                            className="w-full bg-slate-950 border border-slate-700 rounded-lg px-4 py-2 text-white focus:outline-none focus:border-amber-500 transition"
                            required
                        />
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-slate-300 mb-2">Lead Attorney *</label>
                        <select
                            value={formData.lead_attorney_id}
                            onChange={(e) => setFormData({ ...formData, lead_attorney_id: e.target.value })}
                            className="w-full bg-slate-950 border border-slate-700 rounded-lg px-4 py-2 text-white focus:outline-none focus:border-amber-500 transition"
                            required
                        >
                            {lawyers.map(lawyer => (
                                <option key={lawyer.id} value={lawyer.id}>
                                    {lawyer.full_name} - {lawyer.specialization}
                                </option>
                            ))}
                        </select>
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-slate-300 mb-2">Case Description *</label>
                        <textarea
                            value={formData.description}
                            onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                            placeholder="Detailed description of the case..."
                            rows={6}
                            className="w-full bg-slate-950 border border-slate-700 rounded-lg px-4 py-2 text-white focus:outline-none focus:border-amber-500 transition resize-none"
                            required
                        />
                    </div>

                    <div className="flex gap-4 pt-4">
                        <button
                            type="submit"
                            disabled={loading}
                            className="flex-1 bg-amber-600 text-slate-900 font-bold py-3 rounded-lg hover:bg-amber-500 transition disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            {loading ? 'Creating Case...' : 'Create Case'}
                        </button>
                        <Link
                            href="/"
                            className="flex-1 bg-slate-800 text-slate-300 font-bold py-3 rounded-lg hover:bg-slate-700 transition text-center"
                        >
                            Cancel
                        </Link>
                    </div>
                </form>
            </div>
        </main>
    );
}
