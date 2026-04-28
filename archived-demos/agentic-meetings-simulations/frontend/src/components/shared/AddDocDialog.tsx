"use client"

import { useState, useRef } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { apiClient } from "@/lib/api-client"
import { toast } from "sonner"
import { Loader2, Search, Upload, FileText, Info } from "lucide-react"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

interface DocRegistryItem {
  id: string;
  document_name: string;
  file_type: string;
  metadata_json?: any;
}

interface AddDocDialogProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  onDocAdded: (docId: string) => void;
  onDocRemoved?: (docId: string) => void;
  attachedDocIds: string[];
  meetingId?: string;
  roles: any[];
}

export default function AddDocDialog({ 
  isOpen, 
  onOpenChange, 
  onDocAdded, 
  onDocRemoved,
  attachedDocIds,
  meetingId,
  roles 
}: AddDocDialogProps) {
  const queryClient = useQueryClient()
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const [uploadScope, setUploadScope] = useState<'meeting' | 'agent' | 'company'>(meetingId ? 'meeting' : 'company')
  const [uploadOwnerId, setUploadOwnerId] = useState<string>("")
  const [cloningDocId, setCloningDocId] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const { data: globalDocs = [] } = useQuery<DocRegistryItem[]>({
    queryKey: ['global-docs'],
    queryFn: () => apiClient.get<DocRegistryItem[]>('/documents?library_scope=company'),
  })

  const linkMutation = useMutation({
    mutationFn: ({ docId, scope, ownerId }: { docId: string, scope: string, ownerId?: string }) => 
        apiClient.post(`/meetings/${meetingId}/documents/${docId}`, { library_scope: scope, owner_agent_id: ownerId }),
    onSuccess: (data: any) => {
        onDocAdded(data.id || data.data?.id)
        onOpenChange(false)
        toast.success("Document linked to session successfully.")
    }
  })

  const handleUpload = async () => {
    if (!uploadFile) return
    const formData = new FormData()
    formData.append('file', uploadFile)
    formData.append('library_scope', uploadScope)
    if (meetingId) formData.append('meeting_id', meetingId)
    if (uploadScope === 'agent' && uploadOwnerId) {
        formData.append('owner_agent_id', uploadOwnerId)
    }
    
    try {
        const response = await fetch('/api/v1/documents', {
            method: 'POST',
            body: formData,
        })
        const data = await response.json()
        if (data.status === 'success') {
            onDocAdded(data.data.id)
            queryClient.invalidateQueries({ queryKey: ['all-docs'] })
            if (meetingId) queryClient.invalidateQueries({ queryKey: ['meeting-docs', meetingId] })
            onOpenChange(false)
            setUploadFile(null)
            toast.success("Document ingested and synchronized.")
        } else {
            toast.error(data.message || "Upload failed")
        }
    } catch (e) {
        toast.error("Network error during ingest")
    }
  }

  const handleCloneOrLinkFromRegistry = async (docId: string) => {
    if (!meetingId) {
        // Special case for dashboard before meeting starts: just track the ID
        if (uploadScope === 'company') {
            onDocAdded(docId)
            return
        }
        
        // Clone for private scenario context
        setCloningDocId(docId)
        const formData = new FormData()
        formData.append('library_scope', uploadScope)
        if (uploadScope === 'agent' && uploadOwnerId) {
            formData.append('owner_agent_id', uploadOwnerId)
        }

        try {
            const response = await fetch(`/api/v1/documents/${docId}/clone`, {
                method: 'POST',
                body: formData,
            })
            const data = await response.json()
            if (data.status === 'success') {
                onDocAdded(data.data.id)
                queryClient.invalidateQueries({ queryKey: ['all-docs'] })
                toast.success("Registry document cloned into scenario context.")
            } else {
                toast.error(data.message || "Cloning failed")
            }
        } catch (e) {
            toast.error("Bridge fault during cloning.")
        } finally {
            setCloningDocId(null)
        }
    } else {
        // Active meeting: Link it
        linkMutation.mutate({ 
            docId, 
            scope: uploadScope, 
            ownerId: uploadScope === 'agent' ? uploadOwnerId : undefined 
        })
    }
  }

  const validateFile = (f: File) => {
    const validTypes = ['text/plain', 'application/json', 'text/html', 'text/markdown', 'text/csv']
    const isText = validTypes.some(t => f.type.includes(t)) || f.name.endsWith('.md') || f.name.endsWith('.txt');
    if (!isText) {
        toast.error("Unsupported fabric type. Use text-based formats.")
        return false
    }
    return true
  }

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-xl max-h-[90vh] p-0 flex flex-col overflow-hidden bg-card border-border shadow-2xl">
            <Tabs defaultValue="upload">
                <DialogHeader className="p-6 pb-2 border-b border-border/10">
                    <DialogTitle className="text-lg font-bold">Attach Context Documents</DialogTitle>
                    <TabsList className="grid w-full grid-cols-2 mt-4">
                        <TabsTrigger value="upload" className="gap-2">
                            <Upload className="h-3.5 w-3.5" /> Upload
                        </TabsTrigger>
                        <TabsTrigger value="library" className="gap-2">
                            <Search className="h-3.5 w-3.5" /> Registry
                        </TabsTrigger>
                    </TabsList>
                </DialogHeader>

                <div className="flex-1 p-6">
                    <TabsContent value="upload" className="m-0 space-y-6">
                        <div className="space-y-4">
                            <div className="space-y-2">
                                <Label className="text-[10px] uppercase font-bold tracking-widest opacity-60">Target Fabric File</Label>
                                <div 
                                    onClick={() => fileInputRef.current?.click()}
                                    onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); }}
                                    onDrop={(e) => {
                                        e.preventDefault(); e.stopPropagation();
                                        const f = e.dataTransfer.files?.[0];
                                        if (f && validateFile(f)) setUploadFile(f)
                                    }}
                                    className="border-2 border-dashed border-border/50 rounded-xl p-8 flex flex-col items-center justify-center gap-3 hover:border-primary/50 hover:bg-primary/5 transition-all bg-muted/10 cursor-pointer relative"
                                >
                                    <Input 
                                        ref={fileInputRef}
                                        type="file" 
                                        className="hidden" 
                                        onChange={(e) => {
                                            const f = e.target.files?.[0]
                                            if (f && validateFile(f)) setUploadFile(f)
                                        }}
                                    />
                                    {uploadFile ? (
                                        <>
                                            <FileText className="h-10 w-10 text-primary animate-in zoom-in" />
                                            <p className="text-sm font-medium text-foreground">{uploadFile.name}</p>
                                        </>
                                    ) : (
                                        <>
                                            <Upload className="h-10 w-10 text-muted-foreground" />
                                            <div className="text-center">
                                                <p className="text-sm font-medium tracking-tight">Select or drag document</p>
                                                <p className="text-[10px] text-muted-foreground mt-1">Foundational data: .txt, .md, .csv, .json</p>
                                            </div>
                                        </>
                                    )}
                                </div>
                                {uploadFile && (
                                     <button className="text-[10px] text-destructive hover:underline mt-1 w-full text-center" onClick={(e) => { e.stopPropagation(); setUploadFile(null); }}>Clear Selection</button>
                                )}
                            </div>
                        </div>

                        <Button 
                            className="w-full h-12 gap-2 text-sm font-bold shadow-lg" 
                            disabled={!uploadFile || (uploadScope === 'agent' && !uploadOwnerId)}
                            onClick={handleUpload}
                        >
                            <Upload className="h-4 w-4" /> Ingest into Fabric
                        </Button>
                    </TabsContent>

                    <TabsContent value="library" className="m-0 flex flex-col h-[400px]">
                        <ScrollArea className="flex-1 -mx-2 px-2">
                            <div className="space-y-2 py-2">
                                {globalDocs.length === 0 && (
                                    <p className="text-center py-12 text-[10px] uppercase text-muted-foreground opacity-40">Registry unreachable or empty.</p>
                                )}
                                {globalDocs.map((doc) => {
                                    const isAttached = attachedDocIds.includes(doc.id)
                                    const isCloning = cloningDocId === doc.id
                                    const isLinking = linkMutation.isPending && linkMutation.variables?.docId === doc.id
                                    
                                    return (
                                        <div key={doc.id} className="flex items-center justify-between p-3 rounded-lg border border-border/10 bg-muted/5 group hover:bg-muted/10 transition-colors">
                                            <div className="min-w-0">
                                                <p className="text-xs font-bold truncate group-hover:text-primary transition-colors">{doc.document_name}</p>
                                                <p className="text-[9px] uppercase text-muted-foreground mt-0.5">{doc.file_type} • {(doc.metadata_json?.size / 1024).toFixed(1)} KB</p>
                                            </div>
                                            <Button 
                                                size="sm" 
                                                variant={isAttached ? "secondary" : "outline"} 
                                                className="h-7 text-[10px] min-w-[80px]" 
                                                disabled={isCloning || isLinking}
                                                onClick={() => {
                                                    if (isAttached) onDocRemoved?.(doc.id)
                                                    else handleCloneOrLinkFromRegistry(doc.id)
                                                }}>
                                                {isCloning || isLinking ? <Loader2 size={12} className="animate-spin" /> : (isAttached ? "Dettach" : "Attach")}
                                            </Button>
                                        </div>
                                    )
                                })}
                            </div>
                        </ScrollArea>
                    </TabsContent>

                    <div className="space-y-4 pt-4 border-t border-border mt-4 bg-muted/5 p-4 rounded-xl">
                        <div className="grid grid-cols-2 gap-4">
                            <div className="space-y-2">
                                <Label className="text-[10px] uppercase font-bold tracking-widest opacity-60">Persistence Layer</Label>
                                <Select value={uploadScope} onValueChange={(v: any) => setUploadScope(v)}>
                                    <SelectTrigger className="h-9">
                                        <SelectValue placeholder="Persistence..." />
                                    </SelectTrigger>
                                    <SelectContent>
                                        <SelectItem value="company">Global Registry (Stable)</SelectItem>
                                        <SelectItem value="meeting">Session Context (Temporary)</SelectItem>
                                        <SelectItem value="agent">Agent Persona Only</SelectItem>
                                    </SelectContent>
                                </Select>
                            </div>

                            {uploadScope === 'agent' && (
                                <div className="space-y-2">
                                    <Label className="text-[10px] uppercase font-bold tracking-widest opacity-60">Owner Persona</Label>
                                    <Select value={uploadOwnerId} onValueChange={(v) => setUploadOwnerId(v || "")}>
                                        <SelectTrigger className="h-9">
                                            <SelectValue placeholder="Assignee..." />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {roles.map(role => (
                                                <SelectItem key={role.id} value={role.id}>{role.display_name} ({role.title})</SelectItem>
                                            ))}
                                        </SelectContent>
                                    </Select>
                                </div>
                            )}
                        </div>
                        <div className="flex items-center gap-2 p-2 rounded bg-primary/5 text-[9px] text-muted-foreground italic">
                            <Info size={12} className="text-primary" />
                            <span>Metadata and scope are applied upon ingestion.</span>
                        </div>
                    </div>
                </div>
            </Tabs>
        </DialogContent>
    </Dialog>
  )
}
