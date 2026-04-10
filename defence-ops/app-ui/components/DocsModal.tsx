"use client";

import { useState, useEffect, useRef } from "react";
import { X, Loader2, BookOpen } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import mermaid from "mermaid";
import styles from "./DocsModal.module.css";

// Mermaid component to render diagrams
const Mermaid = ({ content }: { content: string }) => {
    const ref = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (ref.current) {
            mermaid.initialize({
                startOnLoad: true,
                theme: 'dark',
                themeVariables: {
                    primaryColor: '#0070F8',
                    primaryTextColor: '#F7F7F7',
                    primaryBorderColor: '#62E5F6',
                    lineColor: '#05CC93',
                    secondaryColor: '#7764FC',
                    tertiaryColor: '#00E0AF',
                    nodeBorder: '#B1B9BE',
                    mainBkg: '#111212',
                    textColor: '#E6E8E9',
                    edgeLabelBackground: '#3E4550',
                    clusterBkg: '#111212',
                    clusterBorder: '#7D8A92'
                }
            });
            mermaid.contentLoaded();
            
            // Re-render when content changes
            const renderDiagram = async () => {
                try {
                    const { svg } = await mermaid.render(`mermaid-${Math.random().toString(36).substr(2, 9)}`, content);
                    if (ref.current) {
                        ref.current.innerHTML = svg;
                    }
                } catch (e) {
                    console.error("Mermaid check error", e);
                }
            };
            renderDiagram();
        }
    }, [content]);

    return <div ref={ref} className={styles.mermaidContainer} />;
};

interface DocsModalProps {
    onClose: () => void;
}

export default function DocsModal({ onClose }: DocsModalProps) {
    const [selectedTab, setSelectedTab] = useState<'USAGE.md' | 'DIAGRAM.md'>('USAGE.md');
    const [content, setContent] = useState<string>("");
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        async function fetchDoc() {
            setLoading(true);
            try {
                const res = await fetch(`/api/v1/docs/${selectedTab}`);
                const data = await res.json();
                if (data.status === 'success') {
                    setContent(data.data);
                }
            } catch (err) {
                console.error("Failed to fetch doc", err);
            } finally {
                setLoading(false);
            }
        }
        fetchDoc();
    }, [selectedTab]);

    return (
        <div className={styles.overlay}>
            <div className={styles.modal}>
                <header className={styles.header}>
                    <div className={styles.titleArea}>
                        <BookOpen size={20} className={styles.icon} />
                        <h2>Operational Documentation</h2>
                    </div>
                    <div className={styles.tabs}>
                        <button 
                            className={`${styles.tab} ${selectedTab === 'USAGE.md' ? styles.activeTab : ''}`}
                            onClick={() => setSelectedTab('USAGE.md')}
                        >
                            USAGE
                        </button>
                        <button 
                            className={`${styles.tab} ${selectedTab === 'DIAGRAM.md' ? styles.activeTab : ''}`}
                            onClick={() => setSelectedTab('DIAGRAM.md')}
                        >
                            DIAGRAM
                        </button>
                    </div>
                    <button className={styles.closeBtn} onClick={onClose}>
                        <X size={24} />
                    </button>
                </header>

                <div className={styles.content}>
                    {loading ? (
                        <div className={styles.loading}>
                            <Loader2 size={32} className={styles.spin} />
                            <span>Loading documentation...</span>
                        </div>
                    ) : (
                        <div className={styles.markdownWrapper}>
                            <ReactMarkdown 
                                remarkPlugins={[remarkGfm]}
                                components={{
                                    code({ className, children, ...props }: { className?: string, children?: React.ReactNode }) {
                                        const match = /language-mermaid/.exec(className || '');
                                        if (match) {
                                            return <Mermaid content={String(children).replace(/\n$/, '')} />;
                                        }
                                        return (
                                            <code className={className} {...props}>
                                                {children}
                                            </code>
                                        );
                                    }
                                }}
                            >
                                {content}
                            </ReactMarkdown>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
