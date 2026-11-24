"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { AlertCircle, Brain, CheckCircle2, Cpu, Eye, Film, Lock, Radio, RefreshCw, Server, X } from "lucide-react";
import { api, type DemoConfig, type LlmModel } from "@/lib/api";
import { useEscapeKey } from "@/lib/useEscapeKey";

interface SettingsViewProps {
  onClose: () => void;
}

const EMPTY: DemoConfig = { llm_model: "", kafka_broker: "", kafka_topic: "" };

function Capability({ label, value, icon }: { label: string; value: boolean | null; icon: React.ReactNode }) {
  const state = value === true ? "yes" : value === false ? "no" : "unknown";
  const title =
    value === null
      ? `The model endpoint does not declare ${label.toLowerCase()} support for this model. Set llm.capabilities in the app's Helm values.`
      : undefined;
  return (
    <span className={`capability ${state}`} title={title}>
      {icon} {label}: {state === "yes" ? "Yes" : state === "no" ? "No" : "Unknown"}
    </span>
  );
}

export default function SettingsView({ onClose }: SettingsViewProps) {
  const [config, setConfig] = useState<DemoConfig>(EMPTY);
  const [canEdit, setCanEdit] = useState(false);
  const [readOnlyReason, setReadOnlyReason] = useState<string | null>(null);
  const [models, setModels] = useState<LlmModel[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [isLoadingModels, setIsLoadingModels] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  useEscapeKey(onClose);

  // State is set in promise callbacks, so the mount effect can call this too.
  const fetchModels = useCallback(
    (refresh: boolean) =>
      api
        .getModels(refresh)
        .then(({ models: list }) => {
          setModels(list);
          setModelsError(null);
        })
        .catch((err: unknown) => {
          setModels([]);
          setModelsError(err instanceof Error ? err.message : "Could not load models from the model endpoint.");
        })
        .finally(() => setIsLoadingModels(false)),
    [],
  );

  const refreshModels = () => {
    setIsLoadingModels(true);
    fetchModels(true);
  };

  useEffect(() => {
    api
      .getDemoConfig()
      .then(({ data, meta }) => {
        setConfig(data);
        setCanEdit(meta.can_edit);
        setReadOnlyReason(meta.reason);
      })
      .catch((err: unknown) => toast.error(err instanceof Error ? err.message : "Could not load the settings."));
    fetchModels(false);
  }, [fetchModels]);

  const selected = models.find((m) => m.id === config.llm_model) ?? null;
  const isReady = selected !== null;

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSaving(true);
    try {
      toast.success(await api.saveDemoConfig(config));
      onClose();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Settings not saved.");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="settings-overlay animate-fade-in">
      <form className="settings-panel" onSubmit={handleSave} role="dialog" aria-modal="true" aria-label="System Config">
        <header className="settings-header">
          <div className="settings-title-group">
            <Cpu size={24} className="settings-title-icon" />
            <div>
              <h2 className="settings-title">System Config</h2>
              <p className="settings-subtitle">Neural Engine & Data Streams</p>
            </div>
          </div>
          <button type="button" onClick={onClose} className="close-btn" aria-label="Close">
            <X size={20} />
          </button>
        </header>

        <div className="settings-content">
          <div className="settings-layout">
            {!canEdit && readOnlyReason && (
              <div className="readonly-box">
                <Lock size={14} />
                <span>{readOnlyReason}</span>
              </div>
            )}

            <section className="settings-section">
              <div className="section-header">
                <Cpu size={18} />
                <h3>Intelligence Hub</h3>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label htmlFor="llm-model">Model</label>
                  <p className="field-hint">
                    Models this app&apos;s key may use on the model endpoint. Pick one that accepts images.
                  </p>
                </div>
                <div className="field-value">
                  <select
                    id="llm-model"
                    value={config.llm_model}
                    disabled={!canEdit || isLoadingModels}
                    onChange={(e) => setConfig((prev) => ({ ...prev, llm_model: e.target.value }))}
                  >
                    <option value="">{isLoadingModels ? "Loading models..." : "Select a model..."}</option>
                    {config.llm_model && !selected && !isLoadingModels && (
                      <option value={config.llm_model}>{config.llm_model} (not available to this key)</option>
                    )}
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.id}
                      </option>
                    ))}
                  </select>
                  {selected && (
                    <div className="capabilities">
                      <Capability label="Vision" value={selected.vision} icon={<Eye size={12} />} />
                      <Capability label="Video" value={selected.video} icon={<Film size={12} />} />
                      <Capability label="Thinking" value={selected.reasoning} icon={<Brain size={12} />} />
                    </div>
                  )}
                  {modelsError && (
                    <div className="error-box">
                      <AlertCircle size={14} />
                      <span>{modelsError}</span>
                    </div>
                  )}
                  <button type="button" onClick={refreshModels} className="icon-btn">
                    <RefreshCw size={12} className={isLoadingModels ? "spin" : ""} />
                    Refresh models
                  </button>
                  <p className="helper-text">
                    Video-capable models get a short clip of the feeds, other vision models get still frames.
                    Thinking is offered only where the model supports it.
                  </p>
                </div>
              </div>
            </section>

            <section className="settings-section">
              <div className="section-header">
                <Radio size={18} />
                <h3>Data Stream (Kafka)</h3>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label htmlFor="kafka-broker">Broker Endpoint</label>
                  <p className="field-hint">The Kafka bootstrap servers, as host:port</p>
                </div>
                <div className="field-value">
                  <div className="input-with-icon">
                    <Server size={14} />
                    <input
                      id="kafka-broker"
                      type="text"
                      placeholder="kafka.example.com:9092"
                      value={config.kafka_broker}
                      disabled={!canEdit}
                      onChange={(e) => setConfig((prev) => ({ ...prev, kafka_broker: e.target.value }))}
                    />
                  </div>
                </div>
              </div>

              <div className="field-group">
                <div className="field-label">
                  <label htmlFor="kafka-topic">Tactical Topic</label>
                  <p className="field-hint">The topic name for live alerts</p>
                </div>
                <div className="field-value">
                  <div className="input-with-icon">
                    <Radio size={14} />
                    <input
                      id="kafka-topic"
                      type="text"
                      placeholder="tactical-alerts"
                      value={config.kafka_topic}
                      disabled={!canEdit}
                      onChange={(e) => setConfig((prev) => ({ ...prev, kafka_topic: e.target.value }))}
                    />
                  </div>
                  <p className="helper-text">
                    Credentials come from the app&apos;s Helm values (kafka.username and kafka.password, or an
                    existing Secret) and are never shown here.
                  </p>
                </div>
              </div>
            </section>

            <div className="persistence-container">
              <div className="persistence-info">
                <div className={`status-pill ${isReady ? "ready" : "pending"}`}>
                  {isReady ? (
                    <>
                      <CheckCircle2 size={14} /> SYSTEM READY
                    </>
                  ) : (
                    <>
                      <AlertCircle size={14} /> CONFIG PENDING
                    </>
                  )}
                </div>
                <span className="pvc-note">Settings are saved to the app&apos;s volume and shared by all users.</span>
              </div>
              <div className="persistence-actions">
                <button type="button" onClick={onClose} className="action-btn outline">
                  {canEdit ? "Cancel" : "Close"}
                </button>
                {canEdit && (
                  <button type="submit" disabled={isSaving} className="action-btn primary lg">
                    {isSaving ? (
                      <>
                        <RefreshCw className="spin" size={18} /> Saving...
                      </>
                    ) : (
                      "Save Configuration"
                    )}
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      </form>

      <style jsx>{`
        .settings-overlay {
          position: fixed;
          inset: 0;
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
          max-height: 85vh;
          display: flex;
          flex-direction: column;
          box-shadow: 0 40px 100px -20px rgba(0, 0, 0, 0.7);
          overflow: hidden;
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
        .settings-title-group :global(.settings-title-icon) {
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
        .input-with-icon :global(svg) {
          position: absolute;
          left: 14px;
          color: #64748b;
          pointer-events: none;
        }
        .input-with-icon input {
          width: 100%;
          padding-left: 42px;
        }
        input,
        select {
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
        input:focus,
        select:focus {
          border-color: #10b981;
          box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.1);
          background: #0f172a;
        }
        input:disabled,
        select:disabled {
          opacity: 0.6;
          cursor: not-allowed;
        }
        .capabilities {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
        }
        .capability {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-size: 12px;
          font-weight: 700;
          padding: 4px 10px;
          border-radius: 100px;
          border: 1px solid #334155;
          color: #94a3b8;
        }
        .capability.yes {
          color: #10b981;
          border-color: rgba(16, 185, 129, 0.3);
          background: rgba(16, 185, 129, 0.08);
        }
        .capability.no {
          color: #f59e0b;
          border-color: rgba(245, 158, 11, 0.3);
          background: rgba(245, 158, 11, 0.08);
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
        .icon-btn {
          background: #1e293b;
          border: 1px solid #334155;
          color: #94a3b8;
          padding: 6px 12px;
          border-radius: 8px;
          cursor: pointer;
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
        .error-box,
        .readonly-box {
          display: flex;
          align-items: center;
          gap: 10px;
          font-size: 13px;
          padding: 12px 16px;
          border-radius: 12px;
        }
        .error-box {
          color: #ef4444;
          background: rgba(239, 68, 68, 0.1);
          border: 1px solid rgba(239, 68, 68, 0.2);
        }
        .readonly-box {
          color: #f59e0b;
          background: rgba(245, 158, 11, 0.08);
          border: 1px solid rgba(245, 158, 11, 0.2);
        }
        .helper-text {
          font-size: 12px;
          color: #64748b;
          margin-top: 6px;
        }
        .spin,
        .icon-btn :global(.spin),
        .action-btn :global(.spin) {
          animation: spin 1s linear infinite;
        }
        @keyframes spin {
          from {
            transform: rotate(0deg);
          }
          to {
            transform: rotate(360deg);
          }
        }
        .animate-fade-in {
          animation: fadeIn 0.4s cubic-bezier(0.4, 0, 0.2, 1);
        }
        @keyframes fadeIn {
          from {
            opacity: 0;
            transform: scale(0.95) translateY(20px);
          }
          to {
            opacity: 1;
            transform: scale(1) translateY(0);
          }
        }
        @media (max-width: 900px) {
          .settings-overlay {
            padding: 0;
          }
          .field-group {
            grid-template-columns: 1fr;
            gap: 12px;
          }
          .settings-panel {
            max-width: 100%;
            height: 100vh;
            max-height: 100vh;
            border-radius: 0;
          }
          .settings-header,
          .settings-content {
            padding: 20px 16px;
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
