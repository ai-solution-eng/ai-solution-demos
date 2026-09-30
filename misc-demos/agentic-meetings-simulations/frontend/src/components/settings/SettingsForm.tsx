"use client"
import { useState, useEffect, useCallback, useRef } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { apiClient } from "@/lib/api-client"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Loader2, RefreshCw, Zap, Database, Wand2, RotateCcw, AlertCircle, Info, Layout, Lock } from "lucide-react"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Badge } from "@/components/ui/badge"
import { toast } from "sonner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"

interface Settings {
  inference_endpoint?: string | null;
  inference_api_key?: string | null;
  inference_model_name?: string | null;
  inference_ignore_tls?: boolean;
  embedding_endpoint?: string | null;
  embedding_api_key?: string | null;
  embedding_model_name?: string | null;
  embedding_ignore_tls?: boolean;
  sqlalchemy_uri?: string;
  recreate?: boolean;
  supervisor_prompt?: string | null;
  agent_prompt?: string | null;
}

interface Model {
  id: string;
  name?: string;
}

interface DiscoveryResponse {
  models: any[];
  error?: string;
}

interface PromptMetadataItem {
    title: string;
    description: string;
    placeholders: string[];
    default: string;
}

interface PromptMetadata {
    [key: string]: PromptMetadataItem;
}

interface SettingsFormProps {
    isInitialSetup?: boolean;
    onSuccess?: () => void;
}

export default function SettingsForm({ isInitialSetup, onSuccess }: SettingsFormProps) {
  const queryClient = useQueryClient()
  
  const { data: initialSettings, isLoading: loadingSettings } = useQuery<Settings>({ 
    queryKey: ['settings'], 
    queryFn: () => apiClient.get<Settings>('/settings').catch(() => ({})),
    enabled: !isInitialSetup
  })

  const { data: systemStatus } = useQuery<any>({
    queryKey: ['system_status'],
    queryFn: () => apiClient.get<any>('/system/status').catch(() => ({}))
  })

  const { data: promptMetadata } = useQuery<PromptMetadata>({
    queryKey: ['prompt_metadata'],
    queryFn: () => apiClient.get<PromptMetadata>('/settings/prompts/metadata').catch(() => ({})),
  })

  const [formData, setFormData] = useState<Settings>({
    inference_ignore_tls: false,
    embedding_ignore_tls: false,
    sqlalchemy_uri: '',
    recreate: false,
    inference_endpoint: "",
    inference_api_key: "",
    embedding_endpoint: "",
    embedding_api_key: "",
    inference_model_name: "base-llm-model",
    embedding_model_name: "base-embedding-model",
    supervisor_prompt: null,
    agent_prompt: null
  })
  
  const [inferenceModels, setInferenceModels] = useState<Model[]>([])
  const [embeddingModels, setEmbeddingModels] = useState<Model[]>([])
  const [loadingModels, setLoadingModels] = useState({ inference: false, embedding: false })
  const [discoveryErrors, setDiscoveryErrors] = useState({ inference: null as string | null, embedding: null as string | null })
  const [loading, setLoading] = useState(false)

  const lastFetched = useRef({ inference: "", embedding: "" })

  useEffect(() => {
    if (initialSettings && Object.keys(initialSettings).length > 0) {
      setFormData(prev => ({
        ...prev,
        ...initialSettings,
        inference_ignore_tls: initialSettings.inference_ignore_tls ?? false,
        embedding_ignore_tls: initialSettings.embedding_ignore_tls ?? false,
      }))
    }
  }, [initialSettings])

  const fetchModels = useCallback(async (endpoint: string, token: string | null, ignoreTls: boolean, type: 'inference' | 'embedding') => {
    if (!endpoint) return;
    const cacheKey = `${endpoint}-${token || ''}-${ignoreTls}`;
    if (lastFetched.current[type] === cacheKey) return;
    lastFetched.current[type] = cacheKey;

    setLoadingModels(prev => ({ ...prev, [type]: true }));
    setDiscoveryErrors(prev => ({ ...prev, [type]: null }));
    
    try {
      const res = await apiClient.post<DiscoveryResponse>('/settings/discover', { endpoint, api_key: token, ignore_tls: ignoreTls });
      if (res.error) {
        setDiscoveryErrors(prev => ({ ...prev, [type]: res.error }));
        if (type === 'inference') setInferenceModels([]); else setEmbeddingModels([]);
      } else {
        const models = Array.isArray(res.models) ? res.models : [];
        const formatted = models.map((m: any) => typeof m === 'string' ? { id: m } : (m.id ? m : { id: m.name || JSON.stringify(m) }));
        
        if (type === 'inference') {
          const current = formData.inference_model_name;
          if (current && !formatted.find(m => m.id === current)) formatted.unshift({ id: current });
          setInferenceModels(formatted);
        } else {
          const current = formData.embedding_model_name;
          if (current && !formatted.find(m => m.id === current)) formatted.unshift({ id: current });
          setEmbeddingModels(formatted);
        }
      }
    } catch (e) {
        setDiscoveryErrors(prev => ({ ...prev, [type]: `Discovery failed: ${e instanceof Error ? e.message : "Unknown error"}` }));
    } finally {
      setLoadingModels(prev => ({ ...prev, [type]: false }));
    }
  }, [formData.inference_model_name, formData.embedding_model_name]);

  useEffect(() => {
    const handler = setTimeout(() => {
      if (formData.inference_endpoint) fetchModels(formData.inference_endpoint, formData.inference_api_key || null, !!formData.inference_ignore_tls, 'inference');
    }, 800);
    return () => clearTimeout(handler);
  }, [formData.inference_endpoint, formData.inference_api_key, formData.inference_ignore_tls, fetchModels]);

  useEffect(() => {
    const handler = setTimeout(() => {
      if (formData.embedding_endpoint) fetchModels(formData.embedding_endpoint, formData.embedding_api_key || null, !!formData.embedding_ignore_tls, 'embedding');
    }, 800);
    return () => clearTimeout(handler);
  }, [formData.embedding_endpoint, formData.embedding_api_key, formData.embedding_ignore_tls, fetchModels]);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    const { name, value, type } = e.target;
    const checked = (e.target as HTMLInputElement).checked;
    setFormData(prev => ({ ...prev, [name]: type === 'checkbox' ? checked : value }))
  }

  const handleRevertPrompt = (key: string) => {
    if (promptMetadata && promptMetadata[key]) {
        setFormData(prev => ({ ...prev, [key]: promptMetadata[key].default }));
        toast.info("Prompt Reset", { description: `Reset ${promptMetadata[key].title} to default.` });
    }
  }

  const handleSave = async () => {
    setLoading(true);
    try {
        await apiClient.post<any>('/system/setup-db', formData);
        if (!isInitialSetup) {
           await apiClient.patch('/settings', formData);
        }
        toast.success("Settings Synchronized", { description: "Parameters have been synced with the orchestration layer." });
        if (onSuccess) onSuccess();
        queryClient.invalidateQueries({ queryKey: ['settings'] });
        queryClient.invalidateQueries({ queryKey: ['system_status'] });
    } catch (err: any) {
        toast.error("Sync Failed", { description: err.message });
    } finally {
        setLoading(false);
    }
  }

  if (loadingSettings && !isInitialSetup) return (
     <div className="flex items-center justify-center p-12 text-muted-foreground italic">
        <Loader2 className="animate-spin mr-3 h-5 w-5" /> Syncing network parameters...
     </div>
  )

  return (
    <div className="space-y-8 animate-in fade-in duration-500 pb-10">
      <Tabs defaultValue="system" className="w-full">
        <TabsList className="grid w-full grid-cols-2 mb-8 bg-muted/20 border border-border p-1 rounded-xl h-14">
          <TabsTrigger value="system" className="rounded-lg data-[state=active]:bg-primary data-[state=active]:text-primary-foreground transition-all flex items-center gap-2 text-sm font-semibold">
            <Database className="h-4 w-4" /> System Configuration
          </TabsTrigger>
          <TabsTrigger value="behaviour" className="rounded-lg data-[state=active]:bg-primary data-[state=active]:text-primary-foreground transition-all flex items-center gap-2 text-sm font-semibold">
            <Wand2 className="h-4 w-4" /> Agent Behaviour
          </TabsTrigger>
        </TabsList>

        <TabsContent value="system" className="space-y-8 animate-in slide-in-from-left-4 duration-300">
           {/* DB SECTION */}
           <Card className="bg-card/40 border-border backdrop-blur-xl overflow-hidden relative shadow-2xl">
             <div className="h-1.5 absolute top-0 left-0 right-0 bg-primary/60" />
             <CardHeader className="pb-2">
                 <div className="flex items-center justify-between">
                     <div className="flex items-center gap-3">
                         <Database className="h-5 w-5 text-primary" />
                         <CardTitle className="text-xl">Persistence Layer</CardTitle>
                     </div>
                     {isInitialSetup && <Badge variant="outline" className="text-[10px] border-primary/30 text-primary">Bootstrap Required</Badge>}
                 </div>
                 <CardDescription className="text-muted-foreground">PostgreSQL Vector Database URI for state and memory persistence.</CardDescription>
             </CardHeader>
             <CardContent className="space-y-6 pt-4">
                 <div className="space-y-2">
                     <Label className="text-muted-foreground text-[10px] font-bold uppercase tracking-widest">SQLAlchemy Connection URI</Label>
                     <Input 
                         name="sqlalchemy_uri"
                         value={formData.sqlalchemy_uri || ""}
                         onChange={handleChange}
                         className="bg-muted/50 border-input text-foreground h-12 text-xs font-mono"
                         placeholder="postgresql+asyncpg://user:pass@host:port/db"
                     />
                 </div>

                 <div className="flex items-start space-x-3 p-4 rounded-xl bg-destructive/10 border border-destructive/20 transition-all hover:bg-destructive/15 group">
                     <input 
                       type="checkbox" 
                       id="recreate" 
                       name="recreate" 
                       checked={formData.recreate} 
                       onChange={handleChange}
                       className="mt-1 w-4 h-4 rounded border-destructive/30 bg-background text-destructive focus:ring-destructive/20 cursor-pointer"
                     />
                     <div className="grid gap-1 leading-none cursor-pointer" onClick={() => setFormData(p => ({ ...p, recreate: !p.recreate }))}>
                       <label htmlFor="recreate" className="text-sm font-semibold text-destructive cursor-pointer">Destructive Re-creation</label>
                       <p className="text-[10px] text-destructive/60 leading-relaxed group-hover:text-destructive/80 transition-colors italic">
                         Wipe all existing tables, schemas, and documents. Re-seeds fresh sample identities. Use with caution.
                       </p>
                     </div>
                  </div>
             </CardContent>
           </Card>

           {/* LLM Section */}
           <Card className="bg-card/40 border-border backdrop-blur-xl overflow-hidden relative shadow-2xl">
             <div className={`h-1.5 absolute top-0 left-0 right-0 ${discoveryErrors.inference ? 'bg-destructive' : 'bg-primary'}`} />
             <CardHeader className="pb-2">
                 <div className="flex justify-between items-center">
                     <div className="flex items-center gap-3">
                         <Zap className="h-5 w-5 text-primary" />
                         <CardTitle className="text-xl">Inference Engine</CardTitle>
                     </div>
                     <div className="flex items-center gap-2 bg-muted/40 rounded-full px-3 py-1 border border-border">
                       <span className="text-[9px] uppercase tracking-widest text-muted-foreground/50 font-bold">Ignore TLS</span>
                       <input 
                         type="checkbox" 
                         name="inference_ignore_tls"
                         checked={formData.inference_ignore_tls}
                         onChange={handleChange}
                         className="w-3.5 h-3.5 rounded bg-muted/20 border-input checked:bg-primary cursor-pointer"
                       />
                     </div>
                 </div>
               <CardDescription className="text-muted-foreground">The thinking engine driving agent discourse.</CardDescription>
             </CardHeader>
             <CardContent className="space-y-6 pt-4">
               <div className="grid gap-6 sm:grid-cols-2">
                 <div className="space-y-2">
                   <Label className="text-muted-foreground text-[10px] font-bold uppercase tracking-widest">Host Endpoint</Label>
                   <Input name="inference_endpoint" value={formData.inference_endpoint || ""} onChange={handleChange} className="bg-muted/30 border-input h-11 text-sm font-mono" />
                 </div>
                 <div className="space-y-2">
                   <Label className="text-muted-foreground text-[10px] font-bold uppercase tracking-widest">Secret Key</Label>
                   <Input type="password" name="inference_api_key" value={formData.inference_api_key || ""} onChange={handleChange} className="bg-muted/30 border-input h-11 text-sm font-mono" />
                 </div>
               </div>
               
               <div className="space-y-2">
                 <Label className="text-muted-foreground text-[10px] font-bold uppercase tracking-widest">Active Model ID</Label>
                 <div className="flex gap-3">
                   <div className="flex-1">
                     {inferenceModels.length > 0 ? (
                       <Select value={formData.inference_model_name || ""} onValueChange={(v) => setFormData(p => ({ ...p, inference_model_name: v }))}>
                         <SelectTrigger className="bg-muted/40 border-input h-11 w-full">
                           <SelectValue placeholder="Select model..." />
                         </SelectTrigger>
                         <SelectContent className="bg-popover border-border">
                           {inferenceModels.map((m) => (
                             <SelectItem key={m.id} value={m.id} className="py-2.5">{m.id}</SelectItem>
                           ))}
                         </SelectContent>
                       </Select>
                     ) : (
                       <Input name="inference_model_name" value={formData.inference_model_name || ""} onChange={handleChange} placeholder="e.g. gpt-4o" className="bg-muted/40 border-input h-11 text-sm font-mono" />
                     )}
                   </div>
                    <div className="flex items-center gap-2">
                        {loadingModels.inference ? (
                            <Loader2 className="h-4 w-4 animate-spin text-primary" />
                        ) : discoveryErrors.inference ? (
                            <div className="text-[10px] text-destructive bg-destructive/10 px-3 py-1 rounded-md border border-destructive/20 font-mono">Discovery Failed</div>
                        ) : (
                            <TooltipProvider>
                                <Tooltip>
                                    <TooltipTrigger>
                                        <Badge 
                                            variant="outline" 
                                            className={`h-7 px-3 flex items-center gap-1.5 transition-all ${
                                                systemStatus?.inference_verified 
                                                ? "border-primary/30 text-primary bg-primary/5" 
                                                : "border-amber-500/30 text-amber-500 bg-amber-500/5 cursor-help"
                                            }`}
                                        >
                                            {systemStatus?.inference_verified ? (
                                                <>
                                                    <div className="h-1.5 w-1.5 rounded-full bg-primary" />
                                                    Verified
                                                </>
                                            ) : (
                                                <>
                                                    <AlertCircle className="h-3 w-3" />
                                                    Unverified Model
                                                </>
                                            )}
                                        </Badge>
                                    </TooltipTrigger>
                                    {!systemStatus?.inference_verified && systemStatus?.inference_status && (
                                        <TooltipContent className="bg-popover border-border max-w-[200px] text-[10px] font-mono leading-relaxed">
                                            {systemStatus.inference_status}
                                        </TooltipContent>
                                    )}
                                </Tooltip>
                            </TooltipProvider>
                        )}
                        <Button 
                            variant="ghost" 
                            size="icon" 
                            className="h-8 w-8 text-muted-foreground hover:text-primary rounded-full"
                            onClick={() => fetchModels(formData.inference_endpoint || "", formData.inference_api_key || null, !!formData.inference_ignore_tls, "inference")}
                        >
                            <RefreshCw className="h-3 w-3" />
                        </Button>
                    </div>
                 </div>
               </div>
             </CardContent>
           </Card>

           {/* Embedding Section */}
           <Card className="bg-card/40 border-border backdrop-blur-xl overflow-hidden relative shadow-2xl">
             <div className={`h-1.5 absolute top-0 left-0 right-0 ${discoveryErrors.embedding ? 'bg-destructive' : 'bg-primary/80'}`} />
             <CardHeader className="pb-2">
                 <div className="flex justify-between items-center">
                     <div className="flex items-center gap-3">
                         <RefreshCw className="h-5 w-5 text-primary" />
                         <CardTitle className="text-xl">Embedding Model</CardTitle>
                     </div>
                     <div className="flex items-center gap-2 bg-muted/40 rounded-full px-3 py-1 border border-border">
                       <span className="text-[9px] uppercase tracking-widest text-muted-foreground/30 font-bold">Ignore TLS</span>
                       <input 
                         type="checkbox" 
                         name="embedding_ignore_tls"
                         checked={formData.embedding_ignore_tls}
                         onChange={handleChange}
                         className="w-3.5 h-3.5 rounded bg-muted/20 border-input checked:bg-primary cursor-pointer"
                       />
                     </div>
                 </div>
               <CardDescription className="text-muted-foreground">Responsible for vectorization and semantic search indexing.</CardDescription>
             </CardHeader>
             <CardContent className="space-y-6 pt-4">
               <div className="grid gap-6 sm:grid-cols-2">
                 <div className="space-y-2">
                   <Label className="text-muted-foreground text-[10px] font-bold uppercase tracking-widest">Vector Endpoint</Label>
                   <Input name="embedding_endpoint" value={formData.embedding_endpoint || ""} onChange={handleChange} className="bg-muted/30 border-input h-11 text-sm font-mono" />
                 </div>
                 <div className="space-y-2">
                   <Label className="text-muted-foreground text-[10px] font-bold uppercase tracking-widest">Secret Key</Label>
                   <Input type="password" name="embedding_api_key" value={formData.embedding_api_key || ""} onChange={handleChange} className="bg-muted/30 border-input h-11 text-sm font-mono" />
                 </div>
               </div>
               
               <div className="space-y-2">
                 <Label className="text-muted-foreground text-[10px] font-bold uppercase tracking-widest">Embedding Model ID</Label>
                 <div className="flex gap-3">
                   <div className="flex-1">
                     {embeddingModels.length > 0 ? (
                       <Select value={formData.embedding_model_name || ""} onValueChange={(v) => setFormData(p => ({ ...p, embedding_model_name: v }))}>
                         <SelectTrigger className="bg-muted/40 border-input h-11 w-full">
                           <SelectValue placeholder="Select model..." />
                         </SelectTrigger>
                         <SelectContent className="bg-popover border-border">
                           {embeddingModels.map((m) => (
                             <SelectItem key={m.id} value={m.id} className="py-2.5">{m.id}</SelectItem>
                           ))}
                         </SelectContent>
                       </Select>
                     ) : (
                       <Input name="embedding_model_name" value={formData.embedding_model_name || ""} onChange={handleChange} placeholder="e.g. text-embedding-3-small" className="bg-muted/40 border-input h-11 text-sm font-mono" />
                     )}
                   </div>
                    <div className="flex items-center gap-2">
                        {loadingModels.embedding ? (
                            <Loader2 className="h-4 w-4 animate-spin text-primary" />
                        ) : discoveryErrors.embedding ? (
                            <div className="text-[10px] text-destructive bg-destructive/10 px-3 py-1 rounded-md border border-destructive/20 font-mono">Discovery Failed</div>
                        ) : (
                            <TooltipProvider>
                                <Tooltip>
                                    <TooltipTrigger>
                                        <Badge 
                                            variant="outline" 
                                            className={`h-7 px-3 flex items-center gap-1.5 transition-all ${
                                                systemStatus?.embedding_verified 
                                                ? "border-primary/30 text-primary bg-primary/5" 
                                                : "border-amber-500/30 text-amber-500 bg-amber-500/5 cursor-help"
                                            }`}
                                        >
                                            {systemStatus?.embedding_verified ? (
                                                <>
                                                    <div className="h-1.5 w-1.5 rounded-full bg-primary" />
                                                    Verified
                                                </>
                                            ) : (
                                                <>
                                                    <AlertCircle className="h-3 w-3" />
                                                    Unverified Model
                                                </>
                                            )}
                                        </Badge>
                                    </TooltipTrigger>
                                    {!systemStatus?.embedding_verified && systemStatus?.embedding_status && (
                                        <TooltipContent className="bg-popover border-border max-w-[200px] text-[10px] font-mono leading-relaxed">
                                            {systemStatus.embedding_status}
                                        </TooltipContent>
                                    )}
                                </Tooltip>
                            </TooltipProvider>
                        )}
                        <Button 
                            variant="ghost" 
                            size="icon" 
                            className="h-8 w-8 text-muted-foreground hover:text-primary rounded-full"
                            onClick={() => fetchModels(formData.embedding_endpoint || "", formData.embedding_api_key || null, !!formData.embedding_ignore_tls, "embedding")}
                        >
                            <RefreshCw className="h-3 w-3" />
                        </Button>
                    </div>
                 </div>
               </div>
             </CardContent>
           </Card>
        </TabsContent>

        <TabsContent value="behaviour" className="space-y-8 animate-in slide-in-from-right-4 duration-300">
            {promptMetadata && Object.entries(promptMetadata).map(([key, meta]) => (
                <Card key={key} className="bg-card/40 border-border backdrop-blur-xl overflow-hidden relative shadow-2xl">
                    <div className="h-1.5 absolute top-0 left-0 right-0 bg-primary/40" />
                    <CardHeader className="pb-2">
                        <div className="flex items-center justify-between">
                            <div className="flex items-center gap-3">
                                <Wand2 className="h-5 w-5 text-primary" />
                                <CardTitle className="text-xl">{meta.title}</CardTitle>
                            </div>
                            <Button 
                                variant="ghost" 
                                size="sm" 
                                onClick={() => handleRevertPrompt(key)}
                                className="text-muted-foreground hover:text-primary gap-2 h-8 px-3 rounded-full"
                            >
                                <RotateCcw className="h-3 w-3" /> Revert to Default
                            </Button>
                        </div>
                        <CardDescription className="text-muted-foreground">{meta.description}</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-6 pt-4">
                        <div className="space-y-4">
                            <div className="flex flex-wrap gap-2">
                                <span className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground self-center">Available Placeholders:</span>
                                {meta.placeholders.map(ph => (
                                    <Badge key={ph} variant="secondary" className="bg-primary/5 text-primary/70 border-primary/20 text-[9px] font-mono">
                                        {ph}
                                    </Badge>
                                ))}
                            </div>
                            
                            <Textarea 
                                name={key}
                                value={formData[key as keyof Settings] as string || meta.default}
                                onChange={handleChange}
                                className="min-h-[250px] bg-muted/30 border-input font-mono text-xs leading-relaxed focus:bg-muted/50 transition-colors"
                                placeholder={`Custom instruction for ${meta.title}...`}
                            />

                            <div className="flex items-start gap-2 text-[10px] text-muted-foreground/60 bg-muted/20 p-3 rounded-lg border border-border/50 italic">
                                <Info className="h-3 w-3 mt-0.5" />
                                <p>Modifying this will affect all future meeting cycles. Placeholders will be dynamically injected during runtime. Ensure structural tags are preserved if used by the model for logic.</p>
                            </div>
                        </div>
                    </CardContent>
                </Card>
            ))}

            {!promptMetadata && (
                <div className="flex flex-col items-center justify-center p-20 text-muted-foreground gap-4 bg-muted/10 rounded-2xl border border-dashed border-border">
                    <Loader2 className="animate-spin h-8 w-8 text-primary/40" />
                    <p className="italic">Downloading behaviour registry from orchestration layer...</p>
                </div>
            )}
        </TabsContent>
      </Tabs>

      <div className="flex flex-col sm:flex-row justify-end pt-8 gap-4 border-t border-border">
        <Button 
          variant="outline"
          onClick={() => isInitialSetup ? window.location.reload() : setFormData({ ...initialSettings, recreate: false })}
          disabled={loading}
          className="border-border text-muted-foreground hover:text-foreground hover:bg-muted h-12 px-8 rounded-full transition-all"
        >
          {isInitialSetup ? "Reload View" : "Cancel Changes"}
        </Button>
        <Button 
          onClick={handleSave} 
          disabled={loading} 
          className="bg-primary hover:bg-primary/90 text-primary-foreground px-12 h-12 rounded-full transition-all shadow-primary/10 shadow-2xl active:scale-95 font-bold uppercase tracking-widest"
        >
          {loading ? (
            <>
              <Loader2 className="animate-spin mr-3 h-5 w-5" />
              Applying Configuration...
            </>
          ) : (
            <>
              <Zap className="mr-3 h-5 w-5 fill-current" />
              {isInitialSetup ? "Initialize Nexus Protocol" : "Synchronize Cluster"}
            </>
          )}
        </Button>
      </div>
    </div>
  )
}
