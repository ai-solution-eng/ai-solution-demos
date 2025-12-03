'use client';

import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import Link from 'next/link';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useToast } from '@/context/ToastContext';

interface Evidence {
    id: number;
    description: string;
    evidence_type: string;
    location_found: string;
    collected_date: string;
}

interface Document {
    id: number;
    title: string;
    content: string;
    created_date: string;
}

interface CaseVideo {
    id: number;
    filename: string;
    file_path: string;
    processed: number;
    created_date: string;
}

interface Lawyer {
    id: number;
    full_name: string;
    email: string;
    specialization: string;
}

interface Personae {
    name: string;
    role: string;
    category: string;
    description: string;
}

interface CaseDetail {
    id: number;
    title: string;
    description: string;
    status: string;
    case_type: string;
    defendant_name: string;
    date_opened: string;
    lead_attorney: Lawyer;
    evidence: Evidence[];
    documents: Document[];
    videos: CaseVideo[];
}

export default function CasePage() {
    const params = useParams();
    const [caseData, setCaseData] = useState<CaseDetail | null>(null);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(false);
    const { showToast } = useToast();

    const [selectedDocument, setSelectedDocument] = useState<Document | null>(null);

    const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
        if (!e.target.files || e.target.files.length === 0) return;

        const file = e.target.files[0];
        const formData = new FormData();
        formData.append('file', file);

        setUploading(true);
        try {
            const res = await fetch(`/api/cases/${params.id}/documents`, {
                method: 'POST',
                body: formData,
            });

            if (res.ok) {
                const newDoc = await res.json();
                setCaseData(prev => prev ? {
                    ...prev,
                    documents: [...prev.documents, newDoc]
                } : null);
            } else {
                console.error('Upload failed');
            }
        } catch (err) {
            console.error('Error uploading file', err);
        } finally {
            setUploading(false);
            // Reset input
            e.target.value = '';
        }
    };

    const handleDeleteDocument = async (docId: number, e: React.MouseEvent) => {
        e.stopPropagation(); // Prevent opening the document viewer

        if (!confirm('Are you sure you want to delete this document?')) {
            return;
        }

        try {
            const res = await fetch(`/api/cases/${params.id}/documents/${docId}`, {
                method: 'DELETE',
            });

            if (res.ok) {
                setCaseData(prev => prev ? {
                    ...prev,
                    documents: prev.documents.filter(d => d.id !== docId)
                } : null);
            } else {
                console.error('Delete failed');
                showToast('Failed to delete document', 'error');
            }
        } catch (err) {
            console.error('Error deleting document', err);
            showToast('Error deleting document', 'error');
        }
    };

    // Evidence form state
    const [showEvidenceForm, setShowEvidenceForm] = useState(false);
    const [evidenceForm, setEvidenceForm] = useState({
        description: '',
        evidence_type: 'Physical',
        location_found: ''
    });
    const [addingEvidence, setAddingEvidence] = useState(false);

    const handleAddEvidence = async (e: React.FormEvent) => {
        e.preventDefault();
        setAddingEvidence(true);

        try {
            const res = await fetch(`/api/cases/${params.id}/evidence`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(evidenceForm)
            });

            if (res.ok) {
                const newEvidence = await res.json();
                setCaseData(prev => prev ? {
                    ...prev,
                    evidence: [...prev.evidence, newEvidence]
                } : null);
                // Reset form
                setEvidenceForm({
                    description: '',
                    evidence_type: 'Physical',
                    location_found: ''
                });
                setShowEvidenceForm(false);
            } else {
                console.error('Failed to add evidence');
                showToast('Failed to add evidence', 'error');
            }
        } catch (err) {
            console.error('Error adding evidence', err);
            showToast('Error adding evidence', 'error');
        } finally {
            setAddingEvidence(false);
        }
    };

    // Document viewer handlers
    const handleDownloadDocument = () => {
        if (!selectedDocument) return;

        // Check if content is a data URI (e.g. PDF)
        if (selectedDocument.content && selectedDocument.content.startsWith('data:')) {
            const a = document.createElement('a');
            a.href = selectedDocument.content;
            a.download = selectedDocument.title || 'document';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            return;
        }

        // Create a blob from the content
        const blob = new Blob([selectedDocument.content || ''], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = selectedDocument.title || 'document.txt';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    };

    const handlePrintDocument = () => {
        if (!selectedDocument) return;

        // Create a new window with the document content
        const printWindow = window.open('', '_blank');
        if (printWindow) {
            printWindow.document.write(`
                <html>
                    <head>
                        <title>${selectedDocument.title}</title>
                        <style>
                            body {
                                font-family: monospace;
                                padding: 20px;
                                white-space: pre-wrap;
                            }
                        </style>
                    </head>
                    <body>
                        <h1>${selectedDocument.title}</h1>
                        <hr>
                        <pre>${selectedDocument.content || ''}</pre>
                    </body>
                </html>
            `);
            printWindow.document.close();
            printWindow.focus();
            setTimeout(() => {
                printWindow.print();
            }, 250);
        }
    };

    // Chat state
    const [chatMessages, setChatMessages] = useState<Array<{ role: string, content: string }>>([]);
    const [chatInput, setChatInput] = useState('');
    const [chatLoading, setChatLoading] = useState(false);
    const [llmConfigured, setLlmConfigured] = useState<boolean | null>(null);
    const [showChat, setShowChat] = useState(false);
    const [availableModels, setAvailableModels] = useState<string[]>([]);
    const [selectedModel, setSelectedModel] = useState<string>('');
    const [showDebug, setShowDebug] = useState(false);
    const [debugInfo, setDebugInfo] = useState<any>(null);

    // AI Actions State
    const [showAiMenu, setShowAiMenu] = useState(false);
    const [showDramatisModal, setShowDramatisModal] = useState(false);
    const [dramatisFormat, setDramatisFormat] = useState<'Table' | 'Tree' | 'Spider'>('Table');
    const [dramatisLoading, setDramatisLoading] = useState(false);
    const [dramatisResult, setDramatisResult] = useState<Personae[] | null>(null);
    const [dramatisDebugSteps, setDramatisDebugSteps] = useState<any[]>([]);
    const [showDramatisDebug, setShowDramatisDebug] = useState(false);
    const [savingPdf, setSavingPdf] = useState(false);

    // Video State
    const [showVideoModal, setShowVideoModal] = useState(false);
    const [videoUploading, setVideoUploading] = useState(false);
    const [selectedVideo, setSelectedVideo] = useState<CaseVideo | null>(null);
    const [videoChatInput, setVideoChatInput] = useState('');
    const [videoChatHistory, setVideoChatHistory] = useState<Array<{ role: string, content: string }>>([]);
    const [videoChatLoading, setVideoChatLoading] = useState(false);
    const [videoNumFrames, setVideoNumFrames] = useState(16);
    const [videoFps, setVideoFps] = useState(1);
    const [videoMaxDuration, setVideoMaxDuration] = useState(30);

    // Check LLM configuration and fetch models
    useEffect(() => {
        fetch('/api/settings/')
            .then(res => res.json())
            .then(data => {
                const hasEndpoint = data.some((s: any) => s.key === 'llm_endpoint' && s.value);
                const hasKey = data.some((s: any) => s.key === 'llm_api_key' && s.value);
                const configured = hasEndpoint && hasKey;
                setLlmConfigured(configured);

                // Fetch available models if configured
                if (configured) {
                    fetch('/api/chat/models')
                        .then(res => res.json())
                        .then(modelData => {
                            setAvailableModels(modelData.models || []);
                            setSelectedModel(modelData.default || modelData.models[0] || '');
                        })
                        .catch(err => console.error('Failed to fetch models', err));
                }
            })
            .catch(() => setLlmConfigured(false));
    }, []);



    const handleGenerateDramatis = async () => {
        setDramatisLoading(true);
        setDramatisResult(null);
        try {
            const res = await fetch(`/api/ai/cases/${params.id}/dramatis-personae`, {
                method: 'POST',
            });
            if (res.ok) {
                const data = await res.json();
                setDramatisResult(data.personae);
                setDramatisDebugSteps(data.debug_steps || []);
            } else {
                showToast('Failed to generate Dramatis Personae', 'error');
            }
        } catch (err) {
            console.error(err);
            showToast('Error generating Dramatis Personae', 'error');
        } finally {
            setDramatisLoading(false);
        }
    };

    const handleSaveDramatisPdf = async () => {
        if (!dramatisResult) return;
        setSavingPdf(true);
        try {
            const res = await fetch(`/api/ai/cases/${params.id}/dramatis-personae/save-pdf`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ personae: dramatisResult })
            });
            if (res.ok) {
                const data = await res.json();
                showToast('PDF saved to case documents successfully!', 'success');
                // Refresh case data to show new document
                fetch(`/api/cases/${params.id}`)
                    .then(res => res.json())
                    .then(data => setCaseData(data));
            } else {
                showToast('Failed to save PDF', 'error');
            }
        } catch (err) {
            console.error(err);
            showToast('Error saving PDF', 'error');
        } finally {
            setSavingPdf(false);
        }
    };

    const handleDownloadDramatisPdf = async () => {
        // Since we don't have a direct download endpoint for the generated content without saving,
        // we can either save it first or use a client-side generator.
        // Given the backend has the PDF generation logic, let's rely on the "Save to Case" functionality primarily,
        // or we can implement a download-only endpoint.
        // For now, I'll guide the user to "Save to Case" then download from documents, 
        // OR I can implement a client-side PDF generation if needed, but backend is better.
        // Let's just use the save function for now as the primary action, or I can add a download button that triggers the save and then downloads the result?
        // The user asked: "allow user to download the results as pdf file, or save the content as pdf file".
        // I'll stick to "Save to Case" for now as it's more robust, and maybe add a "Download" that just triggers the save and then opens the doc.
        await handleSaveDramatisPdf();
    };

    // Video Handlers
    const handleUploadVideo = async (e: React.ChangeEvent<HTMLInputElement>) => {
        if (!e.target.files || e.target.files.length === 0) return;

        const file = e.target.files[0];
        const formData = new FormData();
        formData.append('file', file);

        setVideoUploading(true);
        try {
            const res = await fetch(`/api/cases/${params.id}/videos`, {
                method: 'POST',
                body: formData,
            });

            if (res.ok) {
                const newVideo = await res.json();
                setCaseData(prev => prev ? {
                    ...prev,
                    videos: [...(prev.videos || []), newVideo]
                } : null);
            } else {
                console.error('Upload failed');
                showToast('Failed to upload video', 'error');
            }
        } catch (err) {
            console.error('Error uploading video', err);
            showToast('Error uploading video', 'error');
        } finally {
            setVideoUploading(false);
            // Reset input if possible, but we don't have ref here easily, relying on re-render or manual clear if needed
            e.target.value = '';
        }
    };

    const handleDeleteVideo = async (videoId: number, e: React.MouseEvent) => {
        e.stopPropagation();
        if (!confirm('Are you sure you want to delete this video?')) return;

        try {
            const res = await fetch(`/api/cases/${params.id}/videos/${videoId}`, {
                method: 'DELETE',
            });

            if (res.ok) {
                setCaseData(prev => prev ? {
                    ...prev,
                    videos: (prev.videos || []).filter(v => v.id !== videoId)
                } : null);
                if (selectedVideo?.id === videoId) {
                    setSelectedVideo(null);
                    setVideoChatHistory([]);
                }
            } else {
                showToast('Failed to delete video', 'error');
            }
        } catch (err) {
            console.error(err);
            showToast('Error deleting video', 'error');
        }
    };

    const handleVideoChatSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!videoChatInput.trim() || videoChatLoading || !selectedVideo) return;

        const userMessage = videoChatInput.trim();
        setVideoChatInput('');

        const newHistory = [...videoChatHistory, { role: 'user', content: userMessage }];
        setVideoChatHistory(newHistory);
        setVideoChatLoading(true);

        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 300000); // 5 minute timeout

            const res = await fetch(`/api/cases/${params.id}/videos/${selectedVideo.id}/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: userMessage,
                    history: videoChatHistory,
                    num_frames: videoNumFrames,
                    fps: videoFps,
                    max_duration: videoMaxDuration
                }),
                signal: controller.signal
            });

            clearTimeout(timeoutId);

            if (res.ok) {
                const data = await res.json();
                let responseContent = data.response;

                // Add finish/stop reason info if available
                if (data.finish_reason || data.stop_reason) {
                    const reasonInfo = [];
                    if (data.finish_reason && data.finish_reason !== 'stop') {
                        reasonInfo.push(`Finish: ${data.finish_reason}`);
                    }
                    if (data.stop_reason) {
                        reasonInfo.push(`Stop: ${data.stop_reason}`);
                    }
                    if (reasonInfo.length > 0) {
                        responseContent += `\n\n---\n*${reasonInfo.join(' | ')}*`;
                    }
                }

                setVideoChatHistory([...newHistory, { role: 'assistant', content: responseContent }]);
            } else {
                const err = await res.json();
                setVideoChatHistory([...newHistory, { role: 'assistant', content: `Error: ${err.detail || 'Failed to get response'}` }]);
            }
        } catch (err: any) {
            let errorMessage = 'Error: Failed to communicate with server';
            if (err.name === 'AbortError') {
                errorMessage = '⏱️ Request timed out after 5 minutes. The video may be too large or complex. Try reducing the number of frames or duration.';
            } else if (err.message) {
                errorMessage = `Error: ${err.message}`;
            }
            setVideoChatHistory([...newHistory, { role: 'assistant', content: errorMessage }]);
        } finally {
            setVideoChatLoading(false);
        }
    };

    const handleChatSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!chatInput.trim() || chatLoading) return;

        const userMessage = chatInput.trim();
        setChatInput('');

        // Add user message to chat
        const newMessages = [...chatMessages, { role: 'user', content: userMessage }];
        setChatMessages(newMessages);
        setChatLoading(true);

        try {
            const res = await fetch(`/api/chat/cases/${params.id}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: userMessage,
                    history: chatMessages,
                    model: selectedModel || undefined
                })
            });

            if (res.ok) {
                const data = await res.json();
                setChatMessages([...newMessages, { role: 'assistant', content: data.response }]);
                // Store debug info
                if (data.debug_info) {
                    setDebugInfo(data.debug_info);
                }
            } else {
                const error = await res.json();
                setChatMessages([...newMessages, {
                    role: 'assistant',
                    content: `Error: ${error.detail || 'Failed to get response'}`
                }]);
            }
        } catch (err) {
            setChatMessages([...newMessages, {
                role: 'assistant',
                content: 'Error: Failed to communicate with the server.'
            }]);
        } finally {
            setChatLoading(false);
        }
    };

    useEffect(() => {
        if (params.id) {
            fetch(`/api/cases/${params.id}`)
                .then((res) => res.json())
                .then((data) => {
                    setCaseData(data);
                    setLoading(false);
                })
                .catch((err) => {
                    console.error('Failed to fetch case', err);
                    setLoading(false);
                });
        }
    }, [params.id]);

    if (loading) return <div className="min-h-screen bg-slate-950 text-slate-100 p-8 flex items-center justify-center">Loading case file...</div>;
    if (!caseData) return <div className="min-h-screen bg-slate-950 text-slate-100 p-8 flex items-center justify-center">Case not found</div>;



    return (
        <main className="min-h-screen bg-slate-950 text-slate-100 p-8 relative">
            <Link href="/" className="text-amber-500 hover:text-amber-400 mb-8 inline-block">&larr; Back to Dashboard</Link>

            <header className="mb-8 border-b border-slate-800 pb-8">
                <div className="flex justify-between items-start">
                    <div>
                        <div className="flex items-center gap-4 mb-2">
                            <h1 className="text-3xl font-bold text-white">{caseData.title}</h1>
                            <span className={`px-3 py-1 rounded-full text-xs font-medium ${caseData.status === 'Open' ? 'bg-green-900/30 text-green-400 border border-green-800' :
                                caseData.status === 'Closed' ? 'bg-slate-800 text-slate-400 border border-slate-700' :
                                    'bg-amber-900/30 text-amber-400 border border-amber-800'
                                }`}>
                                {caseData.status}
                            </span>
                        </div>
                        <p className="text-slate-400">Case ID: #{caseData.id} • Opened: {new Date(caseData.date_opened).toLocaleDateString()}</p>
                    </div>
                    <div className="text-right">
                        <div className="text-sm text-slate-500">Lead Attorney</div>
                        <div className="text-lg font-medium text-white">{caseData.lead_attorney?.full_name || 'Unassigned'}</div>
                        <div className="text-sm text-slate-400">{caseData.lead_attorney?.email}</div>
                    </div>
                </div>
            </header>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
                <div className="lg:col-span-2 space-y-8">
                    <section className="bg-slate-900/50 border border-slate-800 rounded-xl p-6">
                        <h2 className="text-xl font-semibold mb-4 text-amber-500">Case Details</h2>
                        <div className="prose prose-invert max-w-none">
                            <p>{caseData.description}</p>
                        </div>
                        <div className="mt-6 grid grid-cols-2 gap-4">
                            <div className="bg-slate-950 p-4 rounded-lg border border-slate-800">
                                <div className="text-sm text-slate-500 mb-1">Defendant</div>
                                <div className="font-medium">{caseData.defendant_name}</div>
                            </div>
                            <div className="bg-slate-950 p-4 rounded-lg border border-slate-800">
                                <div className="text-sm text-slate-500 mb-1">Case Type</div>
                                <div className="font-medium">{caseData.case_type}</div>
                            </div>
                        </div>
                    </section>

                    <section>
                        <h2 className="text-xl font-semibold mb-4 text-white">Evidence Log</h2>
                        <div className="space-y-4">
                            {(caseData.evidence || []).map((ev) => (
                                <div key={ev.id} className="bg-slate-900/30 border border-slate-800 rounded-lg p-4 flex justify-between items-center">
                                    <div>
                                        <div className="font-medium text-slate-200">{ev.description}</div>
                                        <div className="text-sm text-slate-500 mt-1">Found at: {ev.location_found}</div>
                                    </div>
                                    <div className="text-right">
                                        <div className="px-2 py-1 bg-slate-800 rounded text-xs text-slate-300 inline-block">{ev.evidence_type}</div>
                                        <div className="text-xs text-slate-600 mt-1">{new Date(ev.collected_date).toLocaleDateString()}</div>
                                    </div>
                                </div>
                            ))}
                            {(!caseData.evidence || caseData.evidence.length === 0) && <div className="text-slate-500 italic">No evidence logged.</div>}
                        </div>

                        {/* Add Evidence Form */}
                        {showEvidenceForm ? (
                            <form onSubmit={handleAddEvidence} className="mt-6 bg-slate-900/50 border border-slate-800 rounded-lg p-4">
                                <h3 className="text-sm font-semibold text-amber-500 mb-4">Add New Evidence</h3>
                                <div className="space-y-3">
                                    <div>
                                        <label className="block text-xs text-slate-400 mb-1">Description</label>
                                        <input
                                            type="text"
                                            value={evidenceForm.description}
                                            onChange={(e) => setEvidenceForm({ ...evidenceForm, description: e.target.value })}
                                            className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-amber-500"
                                            required
                                        />
                                    </div>
                                    <div>
                                        <label className="block text-xs text-slate-400 mb-1">Evidence Type</label>
                                        <select
                                            value={evidenceForm.evidence_type}
                                            onChange={(e) => setEvidenceForm({ ...evidenceForm, evidence_type: e.target.value })}
                                            className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-amber-500"
                                        >
                                            <option value="Physical">Physical</option>
                                            <option value="Digital">Digital</option>
                                            <option value="Testimonial">Testimonial</option>
                                            <option value="Documentary">Documentary</option>
                                        </select>
                                    </div>
                                    <div>
                                        <label className="block text-xs text-slate-400 mb-1">Location Found</label>
                                        <input
                                            type="text"
                                            value={evidenceForm.location_found}
                                            onChange={(e) => setEvidenceForm({ ...evidenceForm, location_found: e.target.value })}
                                            className="w-full bg-slate-950 border border-slate-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-amber-500"
                                            required
                                        />
                                    </div>
                                    <div className="flex gap-2 pt-2">
                                        <button
                                            type="submit"
                                            disabled={addingEvidence}
                                            className="px-4 py-2 bg-amber-600 text-slate-900 font-semibold rounded hover:bg-amber-500 transition text-sm disabled:opacity-50"
                                        >
                                            {addingEvidence ? 'Adding...' : 'Add Evidence'}
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => setShowEvidenceForm(false)}
                                            className="px-4 py-2 bg-slate-800 text-slate-300 rounded hover:bg-slate-700 transition text-sm"
                                        >
                                            Cancel
                                        </button>
                                    </div>
                                </div>
                            </form>
                        ) : (
                            <button
                                onClick={() => setShowEvidenceForm(true)}
                                className="mt-6 w-full py-2 border border-dashed border-slate-700 text-slate-500 rounded-lg hover:border-amber-500 hover:text-amber-500 transition text-sm"
                            >
                                + Add Evidence
                            </button>
                        )}
                    </section>
                </div>


                <div className="space-y-8">
                    <section className="bg-slate-900/50 border border-slate-800 rounded-xl p-6">
                        <div className="flex justify-between items-center mb-4">
                            <h2 className="text-xl font-semibold text-white">Documents</h2>

                        </div>
                        <ul className="space-y-3">
                            {(caseData.documents || []).map((doc) => (
                                <li
                                    key={doc.id}
                                    className="flex items-center gap-3 p-3 hover:bg-slate-800 rounded-lg transition group"
                                >
                                    <div
                                        onClick={() => setSelectedDocument(doc)}
                                        className="flex items-center gap-3 flex-1 cursor-pointer"
                                    >
                                        <div className="w-10 h-10 bg-slate-800 rounded flex items-center justify-center text-slate-400 group-hover:text-amber-500 group-hover:bg-slate-700 transition">
                                            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                                        </div>
                                        <div className="overflow-hidden">
                                            <div className="font-medium text-sm truncate text-slate-300 group-hover:text-white">{doc.title}</div>
                                            <div className="text-xs text-slate-500">{new Date(doc.created_date).toLocaleDateString()}</div>
                                        </div>
                                    </div>
                                    <button
                                        onClick={(e) => handleDeleteDocument(doc.id, e)}
                                        className="opacity-0 group-hover:opacity-100 p-2 text-red-400 hover:text-red-300 hover:bg-red-900/20 rounded transition"
                                        title="Delete document"
                                    >
                                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path>
                                        </svg>
                                    </button>
                                </li>
                            ))}
                            {(!caseData.documents || caseData.documents.length === 0) && <div className="text-slate-500 italic">No documents attached.</div>}
                        </ul>
                        <button
                            onClick={() => document.getElementById('file-upload')?.click()}
                            className="w-full mt-6 py-2 border border-dashed border-slate-700 text-slate-500 rounded-lg hover:border-amber-500 hover:text-amber-500 transition text-sm flex items-center justify-center gap-2"
                            disabled={uploading}
                        >
                            {uploading ? (
                                <span className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full"></span>
                            ) : (
                                <span>+ Upload Document</span>
                            )}
                        </button>
                        <input
                            type="file"
                            id="file-upload"
                            className="hidden"
                            onChange={handleUpload}
                        />
                    </section>
                </div>
            </div>

            {/* Dramatis Personae Modal */}
            {showDramatisModal && (
                <div className="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center z-50 p-4">
                    <div className="bg-slate-900 border border-slate-700 rounded-xl w-full max-w-5xl max-h-[90vh] flex flex-col shadow-2xl">
                        {/* Header */}
                        <div className="flex justify-between items-center p-6 border-b border-slate-800">
                            <h3 className="text-xl font-bold text-white flex items-center gap-2">
                                <svg className="w-6 h-6 text-amber-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0z"></path></svg>
                                Dramatis Personae
                            </h3>
                            <button onClick={() => setShowDramatisModal(false)} className="text-slate-400 hover:text-white">
                                <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                            </button>
                        </div>

                        {/* Content */}
                        <div className="flex-1 overflow-y-auto p-6 bg-slate-950/50">
                            {!dramatisResult ? (
                                <div className="text-center py-12">
                                    <div className="w-20 h-20 bg-slate-800 rounded-full flex items-center justify-center mx-auto mb-6 text-amber-500">
                                        <svg className="w-10 h-10" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"></path></svg>
                                    </div>
                                    <h4 className="text-2xl font-bold text-white mb-2">Generate Dramatis Personae</h4>
                                    <p className="text-slate-400 mb-8 max-w-md mx-auto">
                                        AI will analyze all case documents and evidence to identify key parties, witnesses, and their roles.
                                    </p>

                                    <div className="flex justify-center gap-4 mb-8">
                                        {['Table', 'Tree', 'Spider'].map(fmt => (
                                            <button
                                                key={fmt}
                                                onClick={() => setDramatisFormat(fmt as any)}
                                                className={`px-4 py-2 rounded-lg border transition ${dramatisFormat === fmt
                                                    ? 'bg-amber-600 border-amber-500 text-white shadow-lg shadow-amber-900/20'
                                                    : 'bg-slate-800 border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-200'}`}
                                            >
                                                {fmt} View
                                            </button>
                                        ))}
                                    </div>

                                    <button
                                        onClick={handleGenerateDramatis}
                                        disabled={dramatisLoading}
                                        className="px-8 py-4 bg-gradient-to-r from-amber-600 to-amber-500 text-slate-900 font-bold rounded-xl hover:from-amber-500 hover:to-amber-400 transition disabled:opacity-50 disabled:cursor-not-allowed shadow-xl shadow-amber-900/20 flex items-center gap-3 mx-auto"
                                    >
                                        {dramatisLoading ? (
                                            <>
                                                <span className="animate-spin h-5 w-5 border-2 border-slate-900 border-t-transparent rounded-full"></span>
                                                Analyzing Documents...
                                            </>
                                        ) : (
                                            <>
                                                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.384-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"></path></svg>
                                                Generate Analysis
                                            </>
                                        )}
                                    </button>
                                </div>
                            ) : (
                                <div className="h-full flex flex-col">
                                    <div className="flex justify-between items-center mb-6 bg-slate-900 p-4 rounded-lg border border-slate-800">
                                        <div className="flex gap-2">
                                            {['Table', 'Tree', 'Spider'].map(fmt => (
                                                <button
                                                    key={fmt}
                                                    onClick={() => setDramatisFormat(fmt as any)}
                                                    className={`px-3 py-1.5 rounded-lg text-sm font-medium transition ${dramatisFormat === fmt
                                                        ? 'bg-amber-600 text-white shadow-lg shadow-amber-900/20'
                                                        : 'bg-slate-800 text-slate-400 hover:text-white'}`}
                                                >
                                                    {fmt}
                                                </button>
                                            ))}
                                        </div>
                                        <div className="flex gap-2">
                                            <button
                                                onClick={handleGenerateDramatis}
                                                disabled={dramatisLoading}
                                                className="px-4 py-2 bg-slate-800 text-slate-300 rounded-lg hover:bg-slate-700 hover:text-white text-sm flex items-center gap-2 border border-slate-700 transition"
                                                title="Re-run Analysis"
                                            >
                                                <svg className={`w-4 h-4 ${dramatisLoading ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
                                                {dramatisLoading ? 'Running...' : 'Re-run'}
                                            </button>
                                            <button
                                                onClick={handleSaveDramatisPdf}
                                                disabled={savingPdf}
                                                className="px-4 py-2 bg-slate-800 text-slate-300 rounded-lg hover:bg-slate-700 hover:text-white text-sm flex items-center gap-2 border border-slate-700 transition"
                                            >
                                                {savingPdf ? (
                                                    <span className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full"></span>
                                                ) : (
                                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 7H5a2 2 0 00-2 2v9a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-3m-1 4l-3 3m0 0l-3-3m3 3V4"></path></svg>
                                                )}
                                                {savingPdf ? 'Saving...' : 'Save to Case (PDF)'}
                                            </button>
                                        </div>
                                    </div>

                                    <div className="flex-1 overflow-auto border border-slate-800 rounded-xl bg-slate-900 p-6 shadow-inner">
                                        {dramatisFormat === 'Table' && (
                                            <table className="w-full text-left text-sm text-slate-300">
                                                <thead className="bg-slate-800/50 text-slate-400 uppercase font-medium text-xs tracking-wider">
                                                    <tr>
                                                        <th className="px-4 py-3 rounded-l-lg">Name</th>
                                                        <th className="px-4 py-3">Role</th>
                                                        <th className="px-4 py-3">Category</th>
                                                        <th className="px-4 py-3 rounded-r-lg">Description</th>
                                                    </tr>
                                                </thead>
                                                <tbody className="divide-y divide-slate-800">
                                                    {dramatisResult.map((p, i) => (
                                                        <tr key={i} className="hover:bg-slate-800/30 transition">
                                                            <td className="px-4 py-4 font-bold text-white">{p.name}</td>
                                                            <td className="px-4 py-4 text-amber-500">{p.role}</td>
                                                            <td className="px-4 py-4">
                                                                <span className={`px-2 py-1 rounded-full text-xs font-medium border ${p.category.includes('Main') ? 'bg-red-900/20 text-red-400 border-red-900/50' :
                                                                    p.category.includes('Witness') ? 'bg-blue-900/20 text-blue-400 border-blue-900/50' :
                                                                        'bg-slate-800 text-slate-500 border-slate-700'
                                                                    }`}>{p.category}</span>
                                                            </td>
                                                            <td className="px-4 py-4 text-slate-400 leading-relaxed">{p.description}</td>
                                                        </tr>
                                                    ))}
                                                </tbody>
                                            </table>
                                        )}

                                        {dramatisFormat === 'Tree' && (
                                            <div className="space-y-8 max-w-3xl mx-auto">
                                                {/* Group by Category */}
                                                {['Main Party', 'Key Witness', 'Peripheral'].map(cat => {
                                                    const people = dramatisResult.filter(p => {
                                                        if (cat === 'Main Party') return p.category.toLowerCase().includes('main');
                                                        if (cat === 'Key Witness') return p.category.toLowerCase().includes('witness');
                                                        return !p.category.toLowerCase().includes('main') && !p.category.toLowerCase().includes('witness');
                                                    });

                                                    if (people.length === 0) return null;

                                                    return (
                                                        <div key={cat} className="relative">
                                                            <div className="flex items-center gap-3 mb-4">
                                                                <div className={`w-4 h-4 rounded-full border-2 ${cat === 'Main Party' ? 'bg-red-500 border-red-300' :
                                                                    cat === 'Key Witness' ? 'bg-blue-500 border-blue-300' :
                                                                        'bg-slate-500 border-slate-300'
                                                                    }`}></div>
                                                                <h4 className="font-bold text-xl text-white">{cat}</h4>
                                                            </div>

                                                            <div className="ml-2 pl-6 border-l-2 border-slate-800 space-y-4">
                                                                {people.map((p, i) => (
                                                                    <div key={i} className="relative group">
                                                                        <div className="absolute -left-[26px] top-6 w-6 h-0.5 bg-slate-800 group-hover:bg-slate-700 transition"></div>
                                                                        <div className="bg-slate-800/40 p-4 rounded-lg border border-slate-800 hover:border-amber-500/50 hover:bg-slate-800 transition">
                                                                            <div className="flex justify-between items-start mb-1">
                                                                                <div className="font-bold text-white text-lg">{p.name}</div>
                                                                                <div className="text-xs font-mono text-amber-500 bg-amber-900/20 px-2 py-0.5 rounded">{p.role}</div>
                                                                            </div>
                                                                            <div className="text-sm text-slate-400">{p.description}</div>
                                                                        </div>
                                                                    </div>
                                                                ))}
                                                            </div>
                                                        </div>
                                                    );
                                                })}
                                            </div>
                                        )}

                                        {dramatisFormat === 'Spider' && (
                                            <div className="flex items-center justify-center min-h-[600px] relative bg-slate-950 rounded-xl overflow-hidden">
                                                <div className="absolute inset-0 bg-[radial-gradient(circle_at_center,_var(--tw-gradient-stops))] from-slate-900 to-slate-950"></div>
                                                {/* Simple SVG Visualization */}
                                                <svg width="800" height="600" viewBox="-400 -300 800 600" className="max-w-full h-auto">
                                                    <defs>
                                                        <marker id="arrow" markerWidth="10" markerHeight="10" refX="20" refY="3" orient="auto" markerUnits="strokeWidth">
                                                            <path d="M0,0 L0,6 L9,3 z" fill="#334155" />
                                                        </marker>
                                                    </defs>

                                                    {/* Connections */}
                                                    {dramatisResult.map((p, i) => {
                                                        const angle = (i / dramatisResult.length) * 2 * Math.PI;
                                                        const radius = 220;
                                                        const x = Math.cos(angle) * radius;
                                                        const y = Math.sin(angle) * radius;
                                                        return <line key={`line-${i}`} x1="0" y1="0" x2={x} y2={y} stroke="#334155" strokeWidth="1" strokeDasharray="4" />;
                                                    })}

                                                    {/* Center Node (Case) */}
                                                    <circle cx="0" cy="0" r="50" fill="#d97706" stroke="#f59e0b" strokeWidth="4" className="drop-shadow-2xl" />
                                                    <text x="0" y="5" textAnchor="middle" fill="white" fontSize="14" fontWeight="bold">CASE</text>

                                                    {/* Nodes */}
                                                    {dramatisResult.map((p, i) => {
                                                        const angle = (i / dramatisResult.length) * 2 * Math.PI;
                                                        const radius = 220;
                                                        const x = Math.cos(angle) * radius;
                                                        const y = Math.sin(angle) * radius;

                                                        const isMain = p.category.toLowerCase().includes('main');
                                                        const isWitness = p.category.toLowerCase().includes('witness');

                                                        return (
                                                            <g key={i} className="cursor-pointer hover:opacity-80 transition">
                                                                <circle cx={x} cy={y} r={isMain ? 35 : 25} fill={
                                                                    isMain ? '#7f1d1d' :
                                                                        isWitness ? '#1e3a8a' :
                                                                            '#1e293b'
                                                                } stroke={
                                                                    isMain ? '#ef4444' :
                                                                        isWitness ? '#3b82f6' :
                                                                            '#64748b'
                                                                } strokeWidth="2" />

                                                                <text x={x} y={y} dy={isMain ? -45 : -35} textAnchor="middle" fill="white" fontSize="12" fontWeight="bold" className="drop-shadow-md">{p.name}</text>
                                                                <text x={x} y={y} dy="5" textAnchor="middle" fill="white" fontSize="10" opacity="0.8">{p.role.split(' ')[0]}</text>
                                                            </g>
                                                        );
                                                    })}
                                                </svg>
                                            </div>
                                        )}
                                    </div>

                                    {/* Debug Section */}
                                    {dramatisDebugSteps && dramatisDebugSteps.length > 0 && (
                                        <div className="mt-6 border-t border-slate-800 pt-4">
                                            <button
                                                onClick={() => setShowDramatisDebug(!showDramatisDebug)}
                                                className="flex items-center gap-2 text-xs text-slate-500 hover:text-amber-500 transition"
                                            >
                                                <svg className={`w-4 h-4 transition-transform ${showDramatisDebug ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7"></path></svg>
                                                Show Analysis Debug Info
                                            </button>

                                            {showDramatisDebug && (
                                                <div className="mt-4 space-y-4 bg-slate-950 p-4 rounded-lg border border-slate-800 overflow-x-auto">
                                                    {dramatisDebugSteps.map((step, idx) => (
                                                        <div key={idx} className="border-b border-slate-800 pb-4 last:border-0 last:pb-0">
                                                            <div className="flex justify-between items-start mb-2">
                                                                <div className="font-bold text-amber-500 text-sm">{step.step_name}</div>
                                                                <div className="text-xs text-slate-500">{step.used_model}</div>
                                                            </div>

                                                            {step.error ? (
                                                                <div className="text-red-400 text-xs mb-2">Error: {step.error}</div>
                                                            ) : (
                                                                <div className="text-green-400 text-xs mb-2">Success</div>
                                                            )}

                                                            <div className="grid grid-cols-2 gap-4">
                                                                <div>
                                                                    <div className="text-[10px] uppercase text-slate-600 font-bold mb-1">Prompt</div>
                                                                    <div className="bg-slate-900 p-2 rounded text-[10px] font-mono text-slate-400 h-32 overflow-y-auto whitespace-pre-wrap">
                                                                        {step.prompt_sent}
                                                                    </div>
                                                                </div>
                                                                <div>
                                                                    <div className="text-[10px] uppercase text-slate-600 font-bold mb-1">Response</div>
                                                                    <div className="bg-slate-900 p-2 rounded text-[10px] font-mono text-slate-400 h-32 overflow-y-auto whitespace-pre-wrap">
                                                                        {step.raw_response}
                                                                    </div>
                                                                </div>
                                                            </div>
                                                        </div>
                                                    ))}
                                                </div>
                                            )}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* Video Analysis Modal */}
            {showVideoModal && (
                <div className="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center z-50 p-4">
                    <div className="bg-slate-900 border border-slate-700 rounded-xl w-full max-w-6xl max-h-[90vh] flex flex-col shadow-2xl">
                        {/* Header */}
                        <div className="flex justify-between items-center p-6 border-b border-slate-800">
                            <h3 className="text-xl font-bold text-white flex items-center gap-2">
                                <svg className="w-6 h-6 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                                Video Analysis
                            </h3>
                            <button onClick={() => setShowVideoModal(false)} className="text-slate-400 hover:text-white">
                                <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                            </button>
                        </div>

                        {/* Content */}
                        <div className="flex-1 overflow-hidden flex">
                            {/* Sidebar: Video List */}
                            <div className="w-1/3 border-r border-slate-800 flex flex-col bg-slate-950/50">
                                <div className="p-4 border-b border-slate-800">
                                    <button
                                        onClick={() => document.getElementById('video-upload')?.click()}
                                        disabled={videoUploading}
                                        className="w-full py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-500 transition flex items-center justify-center gap-2 text-sm font-medium disabled:opacity-50"
                                    >
                                        {videoUploading ? (
                                            <span className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full"></span>
                                        ) : (
                                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"></path></svg>
                                        )}
                                        Upload Video
                                    </button>
                                    <input
                                        type="file"
                                        id="video-upload"
                                        accept=".mp4,.mkv,.mov,.webm"
                                        className="hidden"
                                        onChange={handleUploadVideo}
                                    />
                                </div>
                                <div className="flex-1 overflow-y-auto p-2 space-y-2">
                                    {caseData?.videos && caseData.videos.length > 0 ? (
                                        caseData.videos.map(video => (
                                            <div
                                                key={video.id}
                                                onClick={() => {
                                                    setSelectedVideo(video);
                                                    setVideoChatHistory([]);
                                                }}
                                                className={`p-3 rounded-lg cursor-pointer border transition flex justify-between items-center group ${selectedVideo?.id === video.id
                                                    ? 'bg-blue-900/20 border-blue-500/50'
                                                    : 'bg-slate-900 border-slate-800 hover:border-slate-600'
                                                    }`}
                                            >
                                                <div className="flex items-center gap-3 overflow-hidden">
                                                    <div className="w-8 h-8 bg-slate-800 rounded flex items-center justify-center text-slate-400 flex-shrink-0">
                                                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"></path><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
                                                    </div>
                                                    <div className="truncate">
                                                        <div className={`text-sm font-medium truncate ${selectedVideo?.id === video.id ? 'text-blue-400' : 'text-slate-300'}`}>{video.filename}</div>
                                                        <div className="text-xs text-slate-500">{new Date(video.created_date).toLocaleDateString()}</div>
                                                    </div>
                                                </div>
                                                <button
                                                    onClick={(e) => handleDeleteVideo(video.id, e)}
                                                    className="opacity-0 group-hover:opacity-100 p-1.5 text-red-400 hover:bg-red-900/20 rounded transition"
                                                    title="Delete Video"
                                                >
                                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path></svg>
                                                </button>
                                            </div>
                                        ))
                                    ) : (
                                        <div className="text-center py-8 text-slate-500 text-sm">
                                            No videos uploaded yet.
                                        </div>
                                    )}
                                </div>
                            </div>

                            {/* Main: Chat Area */}
                            <div className="flex-1 flex flex-col bg-slate-900">
                                {selectedVideo ? (
                                    <>
                                        <div className="p-4 border-b border-slate-800 bg-slate-900/50 flex justify-between items-center">
                                            <div>
                                                <h4 className="font-semibold text-white">{selectedVideo.filename}</h4>
                                                <p className="text-xs text-slate-400">AI Video Analysis</p>
                                            </div>
                                            <div className="text-xs text-slate-500 bg-slate-800 px-2 py-1 rounded">
                                                Vision Model Active
                                            </div>
                                        </div>

                                        <div className="flex-1 overflow-y-auto p-4 space-y-4">
                                            {videoChatHistory.length === 0 ? (
                                                <div className="flex flex-col items-center justify-center h-full text-slate-500">
                                                    <div className="w-16 h-16 bg-slate-800 rounded-full flex items-center justify-center mb-4 text-blue-500">
                                                        <svg className="w-8 h-8" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"></path></svg>
                                                    </div>
                                                    <p>Ask questions about the video content.</p>
                                                    <p className="text-sm mt-2">"What is happening in this video?"</p>
                                                    <p className="text-sm">"Describe the people visible."</p>
                                                </div>
                                            ) : (
                                                videoChatHistory.map((msg, idx) => (
                                                    <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                                                        <div className={`max-w-[80%] rounded-lg p-3 ${msg.role === 'user'
                                                            ? 'bg-blue-600 text-white'
                                                            : 'bg-slate-800 text-slate-200'
                                                            }`}>
                                                            {msg.role === 'assistant' ? (
                                                                <div className="text-sm prose prose-invert prose-sm max-w-none">
                                                                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                                                                        {msg.content}
                                                                    </ReactMarkdown>
                                                                </div>
                                                            ) : (
                                                                <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
                                                            )}
                                                        </div>
                                                    </div>
                                                ))
                                            )}
                                            {videoChatLoading && (
                                                <div className="flex justify-start">
                                                    <div className="bg-slate-800 rounded-lg p-3">
                                                        <div className="flex gap-1">
                                                            <span className="w-2 h-2 bg-slate-500 rounded-full animate-bounce"></span>
                                                            <span className="w-2 h-2 bg-slate-500 rounded-full animate-bounce" style={{ animationDelay: '0.1s' }}></span>
                                                            <span className="w-2 h-2 bg-slate-500 rounded-full animate-bounce" style={{ animationDelay: '0.2s' }}></span>
                                                        </div>
                                                    </div>
                                                </div>
                                            )}
                                        </div>

                                        {/* Video Processing Controls */}
                                        <div className="px-4 py-3 border-t border-slate-800 bg-slate-950/30">
                                            <details className="group">
                                                <summary className="cursor-pointer text-xs text-slate-400 hover:text-blue-400 transition flex items-center gap-2">
                                                    <svg className="w-4 h-4 transition-transform group-open:rotate-90" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 5l7 7-7 7"></path>
                                                    </svg>
                                                    Video Processing Options
                                                </summary>
                                                <div className="mt-3 grid grid-cols-3 gap-3">
                                                    <div>
                                                        <label className="text-xs text-slate-500 block mb-1">Frames</label>
                                                        <input
                                                            type="number"
                                                            value={videoNumFrames}
                                                            onChange={(e) => setVideoNumFrames(parseInt(e.target.value) || 32)}
                                                            min="1"
                                                            max="100"
                                                            className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
                                                        />
                                                    </div>
                                                    <div>
                                                        <label className="text-xs text-slate-500 block mb-1">FPS</label>
                                                        <input
                                                            type="number"
                                                            value={videoFps}
                                                            onChange={(e) => setVideoFps(parseInt(e.target.value) || 1)}
                                                            min="1"
                                                            max="30"
                                                            className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
                                                        />
                                                    </div>
                                                    <div>
                                                        <label className="text-xs text-slate-500 block mb-1">Max Duration (s)</label>
                                                        <input
                                                            type="number"
                                                            value={videoMaxDuration}
                                                            onChange={(e) => setVideoMaxDuration(parseInt(e.target.value) || 60)}
                                                            min="1"
                                                            max="300"
                                                            className="w-full bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
                                                        />
                                                    </div>
                                                </div>
                                            </details>
                                        </div>

                                        <div className="p-4 border-t border-slate-800 bg-slate-900/50">
                                            <form onSubmit={handleVideoChatSubmit} className="flex gap-2">
                                                <input
                                                    type="text"
                                                    value={videoChatInput}
                                                    onChange={(e) => setVideoChatInput(e.target.value)}
                                                    placeholder="Ask about this video..."
                                                    className="flex-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                                                    disabled={videoChatLoading}
                                                />
                                                <button
                                                    type="submit"
                                                    disabled={videoChatLoading || !videoChatInput.trim()}
                                                    className="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-500 transition disabled:opacity-50"
                                                >
                                                    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path></svg>
                                                </button>
                                            </form>
                                        </div>
                                    </>
                                ) : (
                                    <div className="flex-1 flex items-center justify-center text-slate-500">
                                        Select a video to start analysis
                                    </div>
                                )}
                            </div>
                        </div>
                    </div>
                </div>
            )}

            {/* AI Actions Menu & Chat */}
            <div className="fixed bottom-8 right-8 z-40 flex flex-col items-end gap-4">
                {showAiMenu && (
                    <div className="bg-slate-900 border border-slate-700 rounded-xl shadow-2xl p-2 mb-2 w-56 animate-in slide-in-from-bottom-2">
                        <div className="px-3 py-2 text-xs font-semibold text-slate-500 uppercase tracking-wider">AI Tools</div>
                        <button
                            onClick={() => { setShowDramatisModal(true); setShowAiMenu(false); }}
                            className="w-full text-left px-3 py-2 hover:bg-slate-800 rounded-lg text-slate-300 hover:text-white transition flex items-center gap-3 group"
                        >
                            <div className="p-1.5 bg-amber-500/10 rounded-md text-amber-500 group-hover:bg-amber-500 group-hover:text-slate-900 transition">
                                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0z"></path></svg>
                            </div>
                            <div>
                                <div className="font-medium">Dramatis Personae</div>
                                <div className="text-[10px] text-slate-500">Analyze parties & roles</div>
                            </div>
                        </button>
                        <button
                            onClick={() => { setShowVideoModal(true); setShowAiMenu(false); }}
                            className="w-full text-left px-3 py-2 hover:bg-slate-800 rounded-lg text-slate-300 hover:text-white transition flex items-center gap-3 group"
                        >
                            <div className="p-1.5 bg-blue-500/10 rounded-md text-blue-500 group-hover:bg-blue-500 group-hover:text-slate-900 transition">
                                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>
                            </div>
                            <div>
                                <div className="font-medium">Video Analysis</div>
                                <div className="text-[10px] text-slate-500">Chat with video evidence</div>
                            </div>
                        </button>
                    </div>
                )}

                <div className="flex gap-4 items-end">
                    <button
                        onClick={() => setShowAiMenu(!showAiMenu)}
                        className={`p-4 rounded-full shadow-2xl transition flex items-center justify-center ${showAiMenu ? 'bg-slate-800 text-white rotate-90' : 'bg-slate-800 text-amber-500 hover:bg-slate-700'}`}
                        title="AI Tools"
                    >
                        <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.384-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"></path></svg>
                    </button>

                    {!showChat ? (
                        <button
                            onClick={() => setShowChat(true)}
                            className="bg-amber-600 text-slate-900 p-4 rounded-full shadow-2xl hover:bg-amber-500 transition flex items-center gap-2 font-semibold"
                        >
                            <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"></path>
                            </svg>
                            <span>Ask AI</span>
                        </button>
                    ) : (
                        <div className="bg-slate-900 border border-slate-700 rounded-xl shadow-2xl w-96 flex flex-col" style={{ height: '500px' }}>
                            <div className="flex justify-between items-center p-4 border-b border-slate-800">
                                <h3 className="font-bold text-white flex items-center gap-2">
                                    <span className="p-2 bg-amber-500/10 rounded-lg text-amber-500">
                                        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"></path>
                                        </svg>
                                    </span>
                                    Case Assistant
                                </h3>
                                <button onClick={() => setShowChat(false)} className="text-slate-400 hover:text-white">
                                    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12"></path>
                                    </svg>
                                </button>
                            </div>


                            {llmConfigured === false ? (
                                <div className="flex-1 flex items-center justify-center p-6 text-center">
                                    <div>
                                        <div className="text-amber-500 mb-3">
                                            <svg className="w-12 h-12 mx-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path>
                                            </svg>
                                        </div>
                                        <p className="text-slate-300 mb-4">LLM endpoint not configured</p>
                                        <Link href="/admin" className="text-amber-500 hover:text-amber-400 underline text-sm">
                                            Configure in Admin →
                                        </Link>
                                    </div>
                                </div>
                            ) : (
                                <>
                                    <div className="flex-1 overflow-y-auto p-4 space-y-4 bg-slate-950/50">
                                        {chatMessages.length === 0 ? (
                                            <div className="text-center text-slate-500 text-sm mt-8">
                                                Ask me anything about this case...
                                            </div>
                                        ) : (
                                            chatMessages.map((msg, idx) => (
                                                <div key={idx} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                                                    <div className={`max-w-[80%] rounded-lg p-3 ${msg.role === 'user'
                                                        ? 'bg-amber-600 text-slate-900'
                                                        : 'bg-slate-800 text-slate-200'
                                                        }`}>
                                                        <p className="text-sm whitespace-pre-wrap">{msg.content}</p>
                                                    </div>
                                                </div>
                                            ))
                                        )}
                                        {chatLoading && (
                                            <div className="flex justify-start">
                                                <div className="bg-slate-800 rounded-lg p-3">
                                                    <div className="flex gap-1">
                                                        <span className="w-2 h-2 bg-slate-500 rounded-full animate-bounce"></span>
                                                        <span className="w-2 h-2 bg-slate-500 rounded-full animate-bounce" style={{ animationDelay: '0.1s' }}></span>
                                                        <span className="w-2 h-2 bg-slate-500 rounded-full animate-bounce" style={{ animationDelay: '0.2s' }}></span>
                                                    </div>
                                                </div>
                                            </div>
                                        )}
                                    </div>

                                    {availableModels.length > 1 && (
                                        <div className="px-4 py-2 border-t border-slate-800 bg-slate-900/50">
                                            <label className="text-xs text-slate-400 mb-1 block">Model</label>
                                            <select
                                                value={selectedModel}
                                                onChange={(e) => setSelectedModel(e.target.value)}
                                                className="w-full bg-slate-950 border border-slate-700 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-amber-500"
                                            >
                                                {availableModels.map(model => (
                                                    <option key={model} value={model}>{model}</option>
                                                ))}
                                            </select>
                                        </div>
                                    )}

                                    {/* Debug Panel */}
                                    {debugInfo && (
                                        <div className="border-t border-slate-800 bg-slate-950/50">
                                            <button
                                                onClick={() => setShowDebug(!showDebug)}
                                                className="w-full px-4 py-2 text-left text-xs text-slate-400 hover:text-amber-500 flex items-center justify-between transition"
                                            >
                                                <span className="flex items-center gap-2">
                                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"></path>
                                                    </svg>
                                                    Debug Info ({debugInfo.chunks_used} chunks, {debugInfo.message_count} messages)
                                                </span>
                                                <svg className={`w-4 h-4 transition-transform ${showDebug ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M19 9l-7 7-7-7"></path>
                                                </svg>
                                            </button>

                                            {showDebug && (
                                                <div className="px-4 pb-4 max-h-64 overflow-y-auto text-xs space-y-3">
                                                    <div>
                                                        <div className="text-amber-500 font-semibold mb-1">Model Info</div>
                                                        <div className="text-slate-400">Model: {debugInfo.model}</div>
                                                        <div className="text-slate-400">VLM: {debugInfo.is_vlm ? 'Yes' : 'No'}</div>
                                                        <div className="text-slate-400">Tokens (est): ~{debugInfo.total_tokens_estimate}</div>
                                                    </div>

                                                    <div>
                                                        <div className="text-amber-500 font-semibold mb-1">Context Stats</div>
                                                        <div className="text-slate-400">Documents: {debugInfo.total_documents}</div>
                                                        <div className="text-slate-400">Evidence: {debugInfo.evidence_count}</div>
                                                        {debugInfo.non_readable_documents && debugInfo.non_readable_documents.length > 0 && (
                                                            <div className="text-red-400">Non-readable: {debugInfo.non_readable_documents.length}</div>
                                                        )}
                                                    </div>

                                                    {debugInfo.non_readable_documents && debugInfo.non_readable_documents.length > 0 && (
                                                        <div>
                                                            <div className="text-red-500 font-semibold mb-1">Non-Readable Documents</div>
                                                            <div className="bg-slate-900 p-2 rounded text-xs">
                                                                {debugInfo.non_readable_documents.map((doc: string, idx: number) => (
                                                                    <div key={idx} className="text-red-400">• {doc}</div>
                                                                ))}
                                                            </div>
                                                        </div>
                                                    )}

                                                    <div>
                                                        <div className="text-amber-500 font-semibold mb-1">System Message</div>
                                                        <div className="bg-slate-900 p-2 rounded text-slate-400 font-mono whitespace-pre-wrap max-h-40 overflow-y-auto">
                                                            {debugInfo.system_message}
                                                        </div>
                                                    </div>
                                                </div>
                                            )}
                                        </div>
                                    )}

                                    <form onSubmit={handleChatSubmit} className="p-4 border-t border-slate-800">
                                        <div className="flex gap-2">
                                            <input
                                                type="text"
                                                value={chatInput}
                                                onChange={(e) => setChatInput(e.target.value)}
                                                placeholder="Ask about this case..."
                                                className="flex-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-amber-500"
                                                disabled={chatLoading}
                                            />
                                            <button
                                                type="submit"
                                                disabled={chatLoading || !chatInput.trim()}
                                                className="bg-amber-600 text-slate-900 px-4 py-2 rounded-lg hover:bg-amber-500 transition disabled:opacity-50 disabled:cursor-not-allowed"
                                            >
                                                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"></path>
                                                </svg>
                                            </button>
                                        </div>
                                    </form>
                                </>
                            )}
                        </div>
                    )}
                </div>
            </div>

            {/* Document Viewer Modal */}
            {
                selectedDocument && (
                    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm flex items-center justify-center z-50 p-4" onClick={() => setSelectedDocument(null)}>
                        <div className="bg-slate-900 border border-slate-700 rounded-xl w-full max-w-4xl max-h-[90vh] flex flex-col shadow-2xl" onClick={(e) => e.stopPropagation()}>
                            <div className="flex justify-between items-center p-6 border-b border-slate-800">
                                <h3 className="text-xl font-bold text-white flex items-center gap-3">
                                    <span className="p-2 bg-amber-500/10 rounded-lg text-amber-500">
                                        <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
                                    </span>
                                    {selectedDocument.title}
                                </h3>
                                <button
                                    onClick={() => setSelectedDocument(null)}
                                    className="text-slate-400 hover:text-white transition"
                                >
                                    <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12"></path></svg>
                                </button>
                            </div>
                            <div className="p-8 overflow-y-auto bg-slate-950/50 font-mono text-slate-300 leading-relaxed whitespace-pre-wrap">
                                {selectedDocument.content}
                            </div>
                            <div className="p-4 border-t border-slate-800 bg-slate-900 flex justify-end gap-3 rounded-b-xl">
                                <button
                                    onClick={handleDownloadDocument}
                                    className="px-4 py-2 bg-slate-800 text-slate-300 rounded hover:bg-slate-700 transition text-sm flex items-center gap-2"
                                >
                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"></path>
                                    </svg>
                                    Download
                                </button>
                                <button
                                    onClick={handlePrintDocument}
                                    className="px-4 py-2 bg-amber-600 text-slate-900 font-semibold rounded hover:bg-amber-500 transition text-sm flex items-center gap-2"
                                >
                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M17 17h2a2 2 0 002-2v-4a2 2 0 00-2-2H5a2 2 0 00-2 2v4a2 2 0 002 2h2m2 4h6a2 2 0 002-2v-4a2 2 0 00-2-2H9a2 2 0 00-2 2v4a2 2 0 002 2zm8-12V5a2 2 0 00-2-2H9a2 2 0 00-2 2v4h10z"></path>
                                    </svg>
                                    Print
                                </button>
                            </div>
                        </div>
                    </div>
                )
            }
        </main >
    );
}
