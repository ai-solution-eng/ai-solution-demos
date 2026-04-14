"use client"
import { useState } from "react"
import { useRouter } from "next/navigation"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { apiClient } from "@/lib/api-client"
import { Card, CardContent, CardHeader, CardTitle, CardFooter } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { toast } from "sonner"
import { 
  Plus, 
  Users, 
  Target, 
  FileText, 
  Trash2, 
  Play, 
  Settings, 
  History as HistoryIcon, 
  Check, 
  ChevronRight,
  TrendingUp,
  Briefcase,
  AlertCircle,
  Loader2,
  PlayCircle
} from "lucide-react"
import AddDocDialog from "@/components/shared/AddDocDialog"
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogFooter } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { useRef, useMemo, useEffect } from "react"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"

interface Role {
  id: string;
  display_name: string;
  title: string;
  department: string;
}

interface Template {
  id: string;
  name: string;
  brief: string;
  agenda: string;
  objective: string;
  expectations: string;
  default_selected_attendee_ids: string[];
}

interface MeetingHistoryItem {
  id: string;
  status: string;
  objective?: string;
  brief?: string;
  selected_attendee_ids: string[];
  created_at: string;
}

interface Document {
  id: string;
  document_name: string;
}

export default function Home() {
  const router = useRouter()
  const { data: roles = [], isLoading: loadingRoles, isError: rolesError } = useQuery<Role[]>({ 
    queryKey: ['roles'], 
    queryFn: () => apiClient.get<Role[]>('/roles') 
  })
  const { data: templates = [], isLoading: loadingTemplates, isError: templatesError } = useQuery<Template[]>({ 
    queryKey: ['templates'], 
    queryFn: () => apiClient.get<Template[]>('/templates') 
  })
  const queryClient = useQueryClient()
  const { data: status } = useQuery({
    queryKey: ['system_status'],
    queryFn: () => apiClient.get<any>('/system/status')
  })

  const { data: pastMeetings = [], isLoading: loadingHistory } = useQuery<MeetingHistoryItem[]>({
    queryKey: ['meetings'],
    queryFn: () => apiClient.get<MeetingHistoryItem[]>('/meetings')
  })

  const deleteMeeting = useMutation({
    mutationFn: (meetingId: string) => apiClient.delete(`/meetings/${meetingId}`),
    onSuccess: () => {
      toast.success("Meeting Archived Deleted", { description: "Simulation results and logs completely purged." })
      queryClient.invalidateQueries({ queryKey: ["meetings"] })
    },
    onError: (err: any) => {
      toast.error("Deletion Failed", { description: err.message || "Failed to remove meeting record." })
    }
  })
  
  const [selectedTemplate, setSelectedTemplate] = useState("")
  const [brief, setBrief] = useState("")
  const [selectedRoleIds, setSelectedRoleIds] = useState<string[]>([])
  const [starting, setStarting] = useState(false)
  
  // Documents State
  const [attachedDocIds, setAttachedDocIds] = useState<string[]>([])
  const [isAddDocOpen, setIsAddDocOpen] = useState(false)

  // We also need meetingDocs list to show what's currently attached
  const { data: allDocs = [] } = useQuery<Document[]>({
    queryKey: ['all-docs'],
    queryFn: () => apiClient.get<Document[]>('/documents'),
  })

  const attachedDocs = useMemo(() => 
    allDocs.filter(d => attachedDocIds.includes(d.id)), 
    [allDocs, attachedDocIds]
  )
  
  const handleTemplateChange = (val: string | null) => {
    if (val) {
      const tpl = templates.find(t => t.id === val)
      if (tpl) {
        setSelectedTemplate(val)
        if (!brief) setBrief(tpl.brief || "")
        setSelectedRoleIds(tpl.default_selected_attendee_ids || [])
      }
    }
  }

  const toggleRole = (roleId: string) => {
    setSelectedRoleIds(prev => 
      prev.includes(roleId) ? prev.filter(id => id !== roleId) : [...prev, roleId]
    )
  }

  const startMeeting = async () => {
    setStarting(true)
    try {
      const tpl = templates.find(t => t.id === selectedTemplate)
      if (!tpl) throw new Error("Select a template first")
      
      if (selectedRoleIds.length === 0) {
        toast.warning("Incomplete Setup", { description: "You must select at least one participant for the simulation." })
        setStarting(false)
        return
      }
      
      const res: any = await apiClient.post('/meetings', {
        brief: brief,
        agenda: tpl.agenda,
        objective: tpl.objective,
        expectations: tpl.expectations,
        selected_attendee_ids: selectedRoleIds,
        template_id: tpl.id,
        turn_limit: 50,
        uploaded_brief_docs: attachedDocIds
      })
      
      toast.success("Meeting Synchronised", { description: "Connecting to agent supervisor..." })
      router.push(`/meeting/${res.id}`)
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || err.response?.data?.message || err.message || "Failed to start meeting session.";
      toast.error("Bridge Error", { description: errorMessage })
      setStarting(false)
    }
  }

  return (
    <div className="container mx-auto p-8 max-w-7xl h-full flex flex-col gap-10 overflow-auto">
      <div className="flex justify-between items-end shrink-0">
        <div>
          <h1 className="text-4xl font-extrabold tracking-tight mb-2 text-primary">Meeting Simulator</h1>
          <p className="text-muted-foreground font-medium">Orchestrate and replay cross-functional agent meetings.</p>
        </div>
      </div>
      
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
          {/* LEFT: New Meeting Form */}
          <div className="lg:col-span-5">
            <Card className="w-full border-border bg-card/50 backdrop-blur-md shadow-xl overflow-hidden">
              <div className="h-1 bg-primary w-full opacity-80" />
              <CardHeader className="pt-8">
                <CardTitle className="text-xl font-bold flex items-center gap-2">
                  <PlayCircle className="text-primary size-5" />
                  New Meeting
                </CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-6">
                  <div className="space-y-2">
                    <label className="text-sm font-medium text-muted-foreground">Meeting Template</label>
                    <Select value={selectedTemplate} onValueChange={handleTemplateChange} disabled={loadingTemplates}>
                        <SelectTrigger className="w-full bg-muted/50 border-input text-foreground">
                          <span className="flex-1 text-left truncate">
                            {templates.find(t => t.id === selectedTemplate)?.name || "Choose a scenario..."}
                          </span>
                        </SelectTrigger>
                      <SelectContent className="bg-popover border-border">
                        {templates.map(t => (
                          <SelectItem key={t.id} value={t.id}>{t.name}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  
                  <div className="space-y-2">
                    <label className="text-sm font-medium text-muted-foreground">Meeting Brief</label>
                    <Textarea 
                      value={brief} 
                      onChange={e => setBrief(e.target.value)} 
                      className="h-24 bg-muted/50 border-input text-foreground placeholder:text-muted-foreground text-sm" 
                      placeholder="Enter the scenario brief..."
                    />
                  </div>

                  <div className="space-y-3">
                    <div className="flex justify-between items-center">
                      <label className="text-sm font-medium text-muted-foreground">Select Participants</label>
                      <span className="text-[10px] text-primary uppercase tracking-widest font-bold">{selectedRoleIds.length} Selected</span>
                    </div>
                    <div className="grid grid-cols-2 gap-2 max-h-[200px] overflow-y-auto pr-2 custom-scrollbar">
                      {roles.map(role => {
                        const isSelected = selectedRoleIds.includes(role.id)
                        return (
                          <div 
                            key={role.id}
                            onClick={() => toggleRole(role.id)}
                            className={`
                              p-3 rounded-lg border cursor-pointer transition-all flex items-center justify-between group
                                ${isSelected 
                                  ? 'bg-primary/10 border-primary/40 text-foreground shadow-sm' 
                                  : 'bg-muted/10 border-border/40 text-muted-foreground hover:border-primary/20 hover:bg-primary/5'}
                              `}
                          >
                            <div className="flex flex-col gap-0.5 overflow-hidden">
                              <span className="text-[10px] font-bold truncate tracking-tight">{role.display_name}</span>
                              <span className="text-[8px] opacity-60 truncate uppercase tracking-widest">{role.department}</span>
                            </div>
                            {isSelected && <div className="h-1.5 w-1.5 rounded-full bg-primary" />}
                          </div>
                        )
                      })}
                    </div>
                  </div>

                  {/* Knowledge Fabric Selection */}
                  <div className="space-y-3 pt-2 border-t border-border/10">
                    <div className="flex justify-between items-center">
                      <label className="text-sm font-medium text-muted-foreground italic">Attached Context</label>
                      <Button variant="ghost" size="sm" className="h-6 px-1.5 hover:text-primary transition-colors text-[9px] font-bold uppercase tracking-tighter gap-1" onClick={() => setIsAddDocOpen(true)}>
                        <Plus size={12} /> Add
                      </Button>
                    </div>
                    <div className="flex flex-wrap gap-2">
                        {attachedDocs.length === 0 && (
                            <p className="text-[9px] text-muted-foreground uppercase opacity-40 font-bold py-2">No documents attached to this scenario</p>
                        )}
                        {attachedDocs.map(doc => (
                            <Badge key={doc.id} variant="secondary" className="bg-muted/30 border-border/10 text-[9px] gap-1.5 py-1 pr-1 pl-2 font-mono group">
                                <span className="truncate max-w-[100px]">{doc.document_name}</span>
                                <button className="hover:bg-destructive/20 p-0.5 rounded transition-colors" onClick={() => setAttachedDocIds(prev => prev.filter(id => id !== doc.id))}>
                                    <Trash2 size={10} className="text-muted-foreground group-hover:text-destructive" />
                                </button>
                            </Badge>
                        ))}
                    </div>
                  </div>
                  
                  {(rolesError || templatesError) && (
                    <div className="flex items-center gap-2 p-3 rounded-md bg-destructive/10 border border-destructive/20 text-destructive text-sm">
                      <AlertCircle size={16} />
                      <span>Failed to synchronise with Nexus Data Fabric.</span>
                    </div>
                  )}
                  {status && !status.ready && (
                    <div className="flex flex-col gap-2 p-3 rounded-md bg-amber-500/10 border border-amber-500/20 text-amber-200/70 text-[10px] leading-relaxed italic">
                      <div className="flex items-start gap-2">
                        <AlertCircle className="size-3 shrink-0 mt-0.5" />
                        <span>System in unverified state. Simulation disabled until verified.</span>
                      </div>
                      {status.reasons && status.reasons.length > 0 && (
                        <div className="pl-5 space-y-1 mt-1 opacity-80 not-italic font-mono">
                          {status.reasons.map((reason: string, i: number) => (
                            <div key={i}>• {reason}</div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
              </CardContent>
              <CardFooter className="pb-8">
                  <Button 
                      className="w-full bg-primary hover:bg-primary/90 text-primary-foreground font-bold h-12" 
                      onClick={startMeeting}
                      disabled={starting || !selectedTemplate || !status?.ready}
                  >
                      {starting ? <Loader2 className="animate-spin mr-2 h-4 w-4" /> : "Start Simulation"}
                  </Button>
              </CardFooter>
            </Card>
          </div>

          {/* RIGHT: Past Meetings History */}
          <div className="lg:col-span-7 flex flex-col gap-4">
              <div className="flex items-center gap-2 text-muted-foreground mb-1">
                <HistoryIcon size={18} />
                <h2 className="text-lg font-bold tracking-tight text-foreground/80">Past Simulations</h2>
              </div>

              <div className="grid grid-cols-1 gap-4">
                  {loadingHistory ? (
                    <div className="flex flex-col items-center justify-center p-12 text-muted-foreground gap-3 bg-muted/20 rounded-xl border border-border border-dashed">
                      <Loader2 className="animate-spin h-6 w-6 text-primary" />
                      <span className="text-xs uppercase tracking-widest font-bold opacity-50">Syncing History...</span>
                    </div>
                  ) : pastMeetings.length === 0 ? (
                    <div className="flex flex-col items-center justify-center p-12 text-muted-foreground gap-3 bg-muted/20 rounded-xl border border-border border-dashed">
                      <HistoryIcon size={24} className="opacity-20" />
                      <span className="text-xs uppercase tracking-widest font-bold opacity-30">No simulations archived yet.</span>
                    </div>
                  ) : (
                    pastMeetings.map((mtg) => (
                      <Card 
                        key={mtg.id} 
                        className="bg-card/40 border-border hover:border-primary/30 transition-all group cursor-pointer"
                        onClick={() => router.push(`/meeting/${mtg.id}`)}
                      >
                        <CardContent className="p-4 flex flex-col gap-3">
                          <div className="flex justify-between items-start">
                            <div className="flex flex-col">
                              <span className={`text-[10px] font-bold uppercase tracking-widest ${
                                mtg.status === 'completed' ? 'text-primary' :
                                mtg.status === 'running' ? 'text-emerald-500' :
                                mtg.status === 'terminated' ? 'text-amber-500' :
                                mtg.status === 'failed' ? 'text-destructive' :
                                'text-muted-foreground'
                              }`}>
                                {mtg.status === 'completed' ? 'COMPLETED' : 
                                 mtg.status === 'terminated' ? 'TERMINATED' : 
                                 mtg.status.toUpperCase()}
                              </span>
                              <h3 className="font-bold text-foreground group-hover:text-primary transition-colors line-clamp-1 text-sm">
                                {mtg.objective || "General Sync"}
                              </h3>
                            </div>
                            <div className="flex items-center gap-2">
                               <button 
                                  onClick={(e) => {
                                    e.stopPropagation()
                                    if (window.confirm("Are you sure you want to permanently delete this simulation? All logs and artifacts will be lost.")) {
                                      deleteMeeting.mutate(mtg.id)
                                    }
                                  }}
                                  className="p-1.5 rounded-md hover:bg-destructive/20 text-muted-foreground hover:text-destructive transition-colors relative z-20"
                                >
                                  <Trash2 size={14} />
                                </button>
                                <span className="text-[10px] text-muted-foreground font-mono">
                                  {new Date(mtg.created_at).toLocaleDateString()}
                                </span>
                            </div>
                          </div>
                          
                          <p className="text-xs text-muted-foreground line-clamp-2 leading-relaxed">
                            {mtg.brief || "No brief documentation available for this session."}
                          </p>

                          <div className="flex items-center gap-3 pt-2">
                              <div className="flex -space-x-2">
                                {mtg.selected_attendee_ids.slice(0, 5).map((aid: string, i: number) => (
                                  <div key={i} className="h-6 w-6 rounded-full bg-muted border border-background flex items-center justify-center">
                                    <Users size={10} className="text-primary" />
                                  </div>
                                ))}
                                {mtg.selected_attendee_ids.length > 5 && (
                                  <div className="h-6 w-6 rounded-full bg-muted border border-background flex items-center justify-center text-[8px] text-muted-foreground font-bold">
                                    +{mtg.selected_attendee_ids.length - 5}
                                  </div>
                                )}
                              </div>
                          </div>
                        </CardContent>
                      </Card>
                    ))
                  )}
              </div>
          </div>
      </div>

      {/* MODAL: Add Document (Shared with Live Page logic) */}
      <AddDocDialog 
        isOpen={isAddDocOpen} 
        onOpenChange={setIsAddDocOpen}
        onDocAdded={(id: string) => setAttachedDocIds(prev => [...prev, id])}
        onDocRemoved={(id: string) => setAttachedDocIds(prev => prev.filter(docId => docId !== id))}
        attachedDocIds={attachedDocIds}
        roles={roles}
      />
    </div>
  )
}
