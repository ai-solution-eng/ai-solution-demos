"use client";

import { useState, useEffect } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { ShieldCheck, Server, Key, AlertCircle, RefreshCw, CheckCircle2, MessageSquare, Cpu, X, Radio, User, Lock } from "lucide-react";

interface SettingsViewProps {
  onClose: () => void;
}

export default function SettingsView({ onClose }: SettingsViewProps) {
  const [demoConfig, setDemoConfig] = useState({
    llm_endpoint: "",
    llm_api_key: "",
    llm_model: "",
    llm_provider: "openai",
    kafka_broker: "",
    kafka_topic: "",
    kafka_username: "",
    kafka_password: "",
  });
  const [isSavingDemoConfig, setIsSavingDemoConfig] = useState(false);
  const [isDiscovering, setIsDiscovering] = useState(false);
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);
  const [llmEndpointError, setLlmEndpointError] = useState(false);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  
  // Endpoint Discovery
  const [discoveredEndpoints, setDiscoveredEndpoints] = useState<{url: string, name: string, type: string}[]>([]);
  const [isLoadingEndpoints, setIsLoadingEndpoints] = useState(false);


  const normalizeLlmEndpoint = (url: string) => {
    if (!url) return url;
    let clean = url.trim();
    if (clean && !clean.startsWith('http')) {
      clean = 'http://' + clean;
    }
    clean = clean.replace(/\/+$/, "");
    return clean;
  };

  useEffect(() => {
    async function loadConfig() {
      try {
        const demoData = await api.getDemoConfig();
        if (demoData?.status === 'success' && demoData.data) {
          setDemoConfig(prev => ({...prev, ...demoData.data}));
        }

        try {
          const modelsData = await api.getModels();
          if (modelsData && modelsData.models) {
            // availableModels state removed as it is not used in the UI
          }
        } catch (_mErr) {
          console.error("Failed to fetch models", _mErr);
        }

        // Fetch discovered endpoints
        await fetchEndpoints();

      } catch (_err) {
        console.error("Failed to load demo config", _err);
      }
    }
    loadConfig();
  }, []);

  const fetchEndpoints = async () => {
    setIsLoadingEndpoints(true);
    try {
      const resp = await api.getDiscoveredEndpoints();
      if (resp.status === "success" && resp.endpoints) {
        setDiscoveredEndpoints(resp.endpoints);
      }
    } catch (_err) {
      console.error("Failed to fetch discovered endpoints", _err);
    } finally {
      setIsLoadingEndpoints(false);
    }
  };


  const handleDiscoverModels = async () => {
    if (!demoConfig.llm_endpoint) {
      toast.error("Please provide an LLM endpoint first");
      return;
    }
    
    setIsDiscovering(true);
    setLlmEndpointError(false);
    setDiscoveryError(null);
    
    const normalizedEndpoint = normalizeLlmEndpoint(demoConfig.llm_endpoint);
    if (normalizedEndpoint !== demoConfig.llm_endpoint) {
      setDemoConfig(prev => ({ ...prev, llm_endpoint: normalizedEndpoint }));
    }

    try {
      const data = await api.discoverModels(normalizedEndpoint, demoConfig.llm_api_key);
      if (data.status === "success") {
        const models = data.models || [];
        setDiscoveredModels(models);
        setLlmEndpointError(false);
        setDiscoveryError(null);
        
        if (models.length === 1) {
          setDemoConfig(prev => ({...prev, llm_model: models[0]}));
          toast.success(`Connected! Automatically selected model: ${models[0]}`);
        } else if (models.length > 1) {
          // showModelModal state removed as it is not used in the UI
          toast.success(`Discovered ${models.length} models. Please select one.`);
        } else {
          toast.warning("Successfully reached endpoint, but no models were found.");
        }
      } else {
        setLlmEndpointError(true);
        setDiscoveryError(data.message || "Unknown error");
        toast.error(`Discovery failed: ${data.message || "Unknown error"}`);
      }
    } catch (err) {
      setLlmEndpointError(true);
      const msg = err instanceof Error ? err.message : "Discovery failed";
      setDiscoveryError(msg);
      toast.error(`Error: ${msg}`);
    } finally {
      setIsDiscovering(false);
    }
  };

  const handleSaveDemoConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSavingDemoConfig(true);
    const finalConfig = { ...demoConfig };
    if (finalConfig.llm_endpoint) {
      finalConfig.llm_endpoint = normalizeLlmEndpoint(finalConfig.llm_endpoint);
    }
    try {
      const data = await api.saveDemoConfig(finalConfig);
      if (data.status === "success") {
        setDemoConfig(finalConfig);
        toast.success("Configuration saved!");
        onClose();
      } else {
        toast.error("Save failed.");
      }
    } catch (_err) {
      toast.error("Save error.");
    } finally {
      setIsSavingDemoConfig(false);
    }
  };

  return (
    <div className="settings-overlay animate-fade-in">
      <div className="settings-panel">
        <header className="settings-header">
          <div className="settings-title-group">
            <Cpu size={24} className="settings-title-icon" />
            <div>
              <h2 className="settings-title">System Config</h2>
              <p className="settings-subtitle">Neural Engine & Data Streams</p>
            </div>
          </div>
          <button onClick={onClose} className="close-btn">
            <X size={20} />
          </button>
        </header>

        <div className="settings-content">
          <div className="settings-layout">
            {/* Intelligence Hub Section */}
            <section className="settings-section">
              <div className="section-header">
                <Cpu size={18} />
                <h3>Intelligence Hub</h3>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label>Provider Type</label>
                  <p className="field-hint">Select the compatibility mode for the LLM endpoint</p>
                </div>
                <div className="field-value">
                  <select 
                    value={demoConfig.llm_provider || "openai"}
                    onChange={e => setDemoConfig(prev => ({...prev, llm_provider: e.target.value}))}
                    className="w-full"
                  >
                    <option value="openai">OpenAI Compatible (Vision via video_url)</option>
                    <option value="ollama">Ollama Compatible (Vision via frames)</option>
                  </select>
                </div>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label>Discovered KServe Endpoints</label>
                  <p className="field-hint">Automatic scanning of available in-cluster inference services</p>
                </div>
                <div className="field-value">
                  <div className="flex-col gap-2">
                    {isLoadingEndpoints ? (
                      <div className="discovery-loading">
                        <RefreshCw className="spin" size={14} />
                        <span>Scanning in-cluster services...</span>
                      </div>
                    ) : discoveredEndpoints.length > 0 ? (
                      <select 
                        onChange={(e) => {
                          const val = e.target.value;
                          if (val) {
                            setDemoConfig(prev => ({...prev, llm_endpoint: val}));
                          }
                        }}
                        className="w-full"
                      >
                        <option value="">Select a discovered endpoint...</option>
                        {discoveredEndpoints.map(ep => (
                          <option key={ep.url} value={ep.url}>{ep.name} ({ep.type})</option>
                        ))}
                      </select>
                    ) : (
                      <div className="discovery-error">No KServe endpoints discovered in-cluster.</div>
                    )}
                    <button onClick={fetchEndpoints} className="icon-btn text-xs py-1">
                      <RefreshCw size={12} className={isLoadingEndpoints ? "spin" : ""} />
                      Manual Refresh
                    </button>
                  </div>
                </div>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label>LLM Endpoint</label>
                  <p className="field-hint">Specify the connection string for your inference service</p>
                </div>
                <div className="field-value">
                  <div className="input-with-icon">
                    <Server size={14} />
                    <input 
                      type="text" 
                      placeholder="https://your-llm-provider.com/v1"
                      value={demoConfig.llm_endpoint}
                      onChange={e => setDemoConfig(prev => ({...prev, llm_endpoint: e.target.value}))}
                    />
                  </div>
                </div>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label>API Authentication Token</label>
                  <p className="field-hint">(Optional) Bearer token for accessing the endpoint</p>
                </div>
                <div className="field-value">
                  <div className="input-with-icon">
                    <Key size={14} />
                    <input 
                      type="password" 
                      placeholder="sk-..."
                      value={demoConfig.llm_api_key}
                      onChange={e => setDemoConfig(prev => ({...prev, llm_api_key: e.target.value}))}
                    />
                  </div>
                </div>
              </div>

              <div className="field-group no-label">
                <div className="field-value">
                  <div className="verification-row">
                    <button 
                      onClick={handleDiscoverModels} 
                      disabled={isDiscovering || !demoConfig.llm_endpoint} 
                      className={`action-btn ${llmEndpointError ? 'danger' : 'outline'}`}
                      style={{ width: '100%' }}
                    >
                      {isDiscovering ? (
                        <><RefreshCw className="spin" size={16} /> Verifying Connection...</>
                      ) : (
                        <><ShieldCheck size={16} /> Verify & Discover Models</>
                      )}
                    </button>
                  </div>
                  {discoveryError && (
                    <div className="error-box" style={{ marginTop: '12px' }}>
                      <AlertCircle size={14} />
                      <span>{discoveryError}</span>
                    </div>
                  )}
                </div>
              </div>
            </section>

            {/* Model Selection Section */}
            <section className="settings-section">
              <div className="section-header">
                <MessageSquare size={18} />
                <h3>Model Selection</h3>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label>Active Neural Model</label>
                  <p className="field-hint">Select the specific model to use for chat and analysis</p>
                </div>
                <div className="field-value">
                  {discoveredModels.length > 0 ? (
                    <select 
                      value={demoConfig.llm_model}
                      onChange={e => setDemoConfig(prev => ({...prev, llm_model: e.target.value}))}
                      className="w-full"
                    >
                      <option value="">Select a model...</option>
                      {discoveredModels.map(m => (
                        <option key={m} value={m}>{m}</option>
                      ))}
                    </select>
                  ) : (
                    <div className="input-with-icon">
                      <Cpu size={14} />
                      <input 
                        type="text" 
                        placeholder="e.g. gpt-4, llama-3.1-8b..."
                        value={demoConfig.llm_model}
                        onChange={e => setDemoConfig(prev => ({...prev, llm_model: e.target.value}))}
                      />
                    </div>
                  )}
                  <p className="helper-text">You must verify the endpoint to populate available models.</p>
                </div>
              </div>
            </section>

            {/* Data Stream Section */}
            <section className="settings-section">
              <div className="section-header">
                <Radio size={18} />
                <h3>Data Stream (Kafka)</h3>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label>Broker Endpoint</label>
                  <p className="field-hint">The Kafka bootstrap server URL</p>
                </div>
                <div className="field-value">
                  <div className="input-with-icon">
                    <Server size={14} />
                    <input 
                      type="text" 
                      placeholder="datafabric.cloud.hpe.com:9092"
                      value={demoConfig.kafka_broker}
                      onChange={e => setDemoConfig(prev => ({...prev, kafka_broker: e.target.value}))}
                    />
                  </div>
                </div>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label>Tactical Topic</label>
                  <p className="field-hint">The topic name for live alerts</p>
                </div>
                <div className="field-value">
                  <div className="input-with-icon">
                    <Radio size={14} />
                    <input 
                      type="text" 
                      placeholder="tactical-alerts"
                      value={demoConfig.kafka_topic}
                      onChange={e => setDemoConfig(prev => ({...prev, kafka_topic: e.target.value}))}
                    />
                  </div>
                </div>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label>SASL Username</label>
                  <p className="field-hint">API Key or Username for authentication</p>
                </div>
                <div className="field-value">
                  <div className="input-with-icon">
                    <User size={14} />
                    <input 
                      type="text" 
                      placeholder="Username"
                      value={demoConfig.kafka_username}
                      onChange={e => setDemoConfig(prev => ({...prev, kafka_username: e.target.value}))}
                    />
                  </div>
                </div>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label>SASL Password</label>
                  <p className="field-hint">API Secret or Password</p>
                </div>
                <div className="field-value">
                  <div className="input-with-icon">
                    <Lock size={14} />
                    <input 
                      type="password" 
                      placeholder="Password"
                      value={demoConfig.kafka_password}
                      onChange={e => setDemoConfig(prev => ({...prev, kafka_password: e.target.value}))}
                    />
                  </div>
                  <p className="helper-text">Connection will always use SASL_PLAINTEXT / PLAIN mechanism.</p>
                </div>
              </div>
            </section>

            {/* Persistence & Actions */}
            <div className="persistence-container">
               <div className="persistence-info">
                  <div className={`status-pill ${demoConfig.llm_endpoint && !llmEndpointError && discoveredModels.length > 0 ? "ready" : "pending"}`}>
                    {demoConfig.llm_endpoint && !llmEndpointError && discoveredModels.length > 0 ? (
                      <><CheckCircle2 size={14} /> SYSTEM READY</>
                    ) : (
                      <><AlertCircle size={14} /> CONFIG PENDING</>
                    )}
                  </div>
                  <span className="pvc-note">System configuration is persisted automatically to the PVC for multi-session continuity.</span>
               </div>
               <div className="persistence-actions">
                  <button onClick={onClose} className="action-btn outline">Cancel</button>
                  <button onClick={handleSaveDemoConfig} disabled={isSavingDemoConfig} className="action-btn primary lg">
                    {isSavingDemoConfig ? (
                      <><RefreshCw className="spin" size={18} /> Optimizing...</>
                    ) : (
                      "Save Configuration"
                    )}
                  </button>
               </div>
            </div>
          </div>
        </div>
      </div>

      <style jsx>{`
        .settings-overlay {
          position: fixed;
          top: 0;
          left: 0;
          right: 0;
          bottom: 0;
          background: rgba(2, 6, 23, 0.7);
          backdrop-filter: blur(8px);
          display: flex;
          align-items: center;
          justify-content: center;
          z-index: 1000;
          padding: 40px;
        }
        .settings-panel {
          background: #0f172a;
          border: 1px solid #1e293b;
          border-radius: 24px;
          width: 100%;
          max-width: 1100px;
          height: auto;
          max-height: 85vh;
          display: flex;
          flex-direction: column;
          box-shadow: 0 40px 100px -20px rgba(0, 0, 0, 0.7);
          overflow: hidden;
          transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .settings-header {
          padding: 32px 40px;
          border-bottom: 1px solid #1e293b;
          display: flex;
          justify-content: space-between;
          align-items: center;
          background: rgba(30, 41, 59, 0.3);
        }
        .settings-title-group {
          display: flex;
          align-items: center;
          gap: 20px;
        }
        .settings-title-icon {
          color: #10b981;
          background: rgba(16, 185, 129, 0.1);
          padding: 10px;
          border-radius: 12px;
          box-sizing: content-box;
        }
        .settings-title {
          font-size: 24px;
          font-weight: 800;
          color: white;
          margin: 0;
          text-transform: uppercase;
          letter-spacing: 1px;
        }
        .settings-subtitle {
          font-size: 14px;
          color: #94a3b8;
          margin: 4px 0 0 0;
        }
        .close-btn {
          background: #1e293b;
          border: 1px solid #334155;
          color: #94a3b8;
          cursor: pointer;
          padding: 10px;
          border-radius: 12px;
          transition: all 0.2s;
        }
        .close-btn:hover {
          background: #ef4444;
          border-color: #ef4444;
          color: white;
          transform: rotate(90deg);
        }
        .settings-content {
          padding: 40px;
          overflow-y: auto;
          flex: 1;
        }
        .settings-layout {
          display: flex;
          flex-direction: column;
          gap: 40px;
          max-width: 900px;
          margin: 0 auto;
        }
        .settings-section {
          display: flex;
          flex-direction: column;
          gap: 24px;
        }
        .section-header {
          display: flex;
          align-items: center;
          gap: 12px;
          color: #10b981;
          border-bottom: 1px solid #1e293b;
          padding-bottom: 16px;
        }
        .section-header h3 {
          margin: 0;
          font-size: 18px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 1px;
        }
        
        .field-group {
          display: grid;
          grid-template-columns: 320px 1fr;
          gap: 32px;
          align-items: start;
        }
        .field-group.no-label {
          grid-template-columns: 1fr;
        }
        .field-group.no-label .field-value {
          margin-left: 320px + 32px;
        }
        .field-label label {
          display: block;
          font-size: 14px;
          font-weight: 700;
          color: #f1f5f9;
          margin-bottom: 6px;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }
        .field-hint {
          font-size: 12px;
          color: #64748b;
          line-height: 1.5;
        }
        .field-value {
          display: flex;
          flex-direction: column;
          gap: 8px;
        }

        .input-with-icon {
          position: relative;
          display: flex;
          align-items: center;
        }
        .input-with-icon svg {
          position: absolute;
          left: 14px;
          color: #64748b;
        }
        .input-with-icon input {
          width: 100%;
          padding-left: 42px !important;
        }
        input, select {
          background: #020617;
          border: 1px solid #334155;
          color: #f1f5f9;
          padding: 14px 16px;
          border-radius: 12px;
          font-size: 15px;
          outline: none;
          transition: all 0.2s;
          width: 100%;
        }
        input:focus, select:focus {
          border-color: #10b981;
          box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.1);
          background: #0f172a;
        }
        
        .action-btn {
          padding: 12px 20px;
          border-radius: 12px;
          font-size: 14px;
          font-weight: 700;
          cursor: pointer;
          transition: all 0.2s;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 10px;
          border: none;
        }
        .action-btn.primary {
          background: #10b981;
          color: white;
          box-shadow: 0 4px 12px rgba(16, 185, 129, 0.2);
        }
        .action-btn.primary:hover {
          background: #059669;
          transform: translateY(-1px);
          box-shadow: 0 6px 16px rgba(16, 185, 129, 0.3);
        }
        .action-btn.primary.lg {
           padding: 16px 32px;
           font-size: 16px;
        }
        .action-btn.outline {
          background: transparent;
          border: 1px solid #334155;
          color: #cbd5e1;
        }
        .action-btn.outline:hover {
          background: rgba(255, 255, 255, 0.05);
          border-color: #475569;
        }
        .action-btn.danger {
          background: rgba(239, 68, 68, 0.1);
          color: #ef4444;
          border: 1px solid rgba(239, 68, 68, 0.2);
        }
        .action-btn.danger:hover {
          background: rgba(239, 68, 68, 0.2);
        }

        .icon-btn {
          background: #1e293b;
          border: 1px solid #334155;
          color: #94a3b8;
          padding: 6px 12px;
          border-radius: 8px;
          cursor: pointer;
          transition: all 0.2s;
          display: inline-flex;
          align-items: center;
          gap: 6px;
          width: fit-content;
        }
        .icon-btn:hover {
          background: #334155;
          color: white;
        }

        .persistence-container {
          margin-top: 20px;
          padding-top: 32px;
          border-top: 1px solid #1e293b;
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 40px;
        }
        .persistence-info {
          display: flex;
          flex-direction: column;
          gap: 10px;
        }
        .status-pill {
          display: flex;
          align-items: center;
          gap: 8px;
          font-weight: 800;
          font-size: 12px;
          padding: 6px 12px;
          border-radius: 100px;
          width: fit-content;
          letter-spacing: 0.5px;
        }
        .status-pill.ready {
          background: rgba(16, 185, 129, 0.1);
          color: #10b981;
          border: 1px solid rgba(16, 185, 129, 0.2);
        }
        .status-pill.pending {
          background: rgba(245, 158, 11, 0.1);
          color: #f59e0b;
          border: 1px solid rgba(245, 158, 11, 0.2);
        }
        .pvc-note {
           font-size: 11px;
           color: #64748b;
           font-style: italic;
        }
        .persistence-actions {
          display: flex;
          align-items: center;
          gap: 16px;
        }

        .discovery-loading {
          display: flex;
          align-items: center;
          gap: 12px;
          color: #10b981;
          font-size: 14px;
          padding: 12px 16px;
          background: rgba(16, 185, 129, 0.05);
          border: 1px solid rgba(16, 185, 129, 0.2);
          border-radius: 12px;
        }
        .discovery-error {
          color: #94a3b8;
          font-size: 13px;
          padding: 12px 16px;
          background: rgba(255, 255, 255, 0.03);
          border: 1px solid #1e293b;
          border-radius: 12px;
          font-style: italic;
        }
        .error-box {
          display: flex;
          align-items: center;
          gap: 10px;
          color: #ef4444;
          font-size: 13px;
          background: rgba(239, 68, 68, 0.1);
          padding: 12px 16px;
          border-radius: 12px;
          border: 1px solid rgba(239, 68, 68, 0.2);
        }
        .helper-text {
          font-size: 12px;
          color: #64748b;
          margin-top: 6px;
        }
        .flex-col {
           display: flex;
           flex-direction: column;
        }
        .gap-2 {
           gap: 12px;
        }
        .w-full {
           width: 100%;
        }
        .spin {
          animation: spin 1s linear infinite;
        }
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
        .animate-fade-in {
          animation: fadeIn 0.4s cubic-bezier(0.4, 0, 0.2, 1);
        }
        @keyframes fadeIn {
          from { opacity: 0; transform: scale(0.95) translateY(20px); }
          to { opacity: 1; transform: scale(1) translateY(0); }
        }

        @media (max-width: 900px) {
          .field-group {
            grid-template-columns: 1fr;
            gap: 12px;
          }
          .field-group.no-label .field-value {
            margin-left: 0;
          }
          .settings-panel {
            max-width: 100%;
            height: 100vh;
            max-height: 100vh;
            border-radius: 0;
          }
          .persistence-container {
            flex-direction: column;
            align-items: flex-start;
          }
          .persistence-actions {
            width: 100%;
          }
          .persistence-actions button {
            flex: 1;
          }
        }
      `}</style>
    </div>
  );
}
