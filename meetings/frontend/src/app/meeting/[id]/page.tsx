"use client"
import { useEffect, useState, useRef, use, useMemo } from 'react'
import { useMeetingStore } from '@/store'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Badge } from '@/components/ui/badge'
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion'
import axios from 'axios'

import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { apiClient } from '@/lib/api-client'
import ReactMarkdown from 'react-markdown'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger, DialogFooter } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { 
  FileText, 
  MessageSquare, 
  Settings, 
  Info, 
  Clock, 
  CheckCircle, 
  XCircle, 
  Loader2, 
  Send, 
  Mic, 
  BarChart, 
  Shield, 
  ChevronRight, 
  Paperclip, 
  MoreVertical, 
  Search, 
  FilePlus, 
  ArrowLeft, 
  CheckSquare, 
  RefreshCcw, 
  Terminal
} from "lucide-react"
import AddDocDialog from "@/components/shared/AddDocDialog"
import { toast } from 'sonner'

const CHAT_COLORS = [
  "bg-blue-500/10 border-blue-500/30 text-blue-400",
  "bg-emerald-500/10 border-emerald-500/30 text-emerald-400",
  "bg-amber-500/10 border-amber-500/30 text-amber-400",
  "bg-rose-500/10 border-rose-500/30 text-rose-400",
  "bg-violet-500/10 border-violet-500/30 text-violet-400",
  "bg-cyan-500/10 border-cyan-500/30 text-cyan-400",
  "bg-orange-500/10 border-orange-500/30 text-orange-400",
  "bg-pink-500/10 border-pink-500/30 text-pink-400",
]

export default function LiveMeetingPage({ params }: { params: Promise<{ id: string }> }) {
  const unwrappedParams = use(params)
  const meetingId = unwrappedParams.id
  
  const store = useMeetingStore()
  const [socket, setSocket] = useState<WebSocket | null>(null)

  const { data: meeting, isLoading } = useQuery({
    queryKey: ['meeting', meetingId],
    queryFn: () => apiClient.get<any>(`/meetings/${meetingId}`),
    enabled: !!meetingId
  })

  // Fetch all roles to map IDs to Names
  const { data: roles = [] } = useQuery<any[]>({
    queryKey: ['roles'],
    queryFn: () => apiClient.get<any[]>('/roles'),
  })

  const roleMap = useMemo(() => roles.reduce((acc, r) => {
    acc[r.id] = r
    return acc
  }, {} as Record<string, any>), [roles])

  const getAgentLabel = (id: string | undefined) => {
    if (!id) return "System"
    if (id === 'supervisor') return "Supervisor"
    if (id === 'FINISH') return "Conclusion"
    const role = roleMap[id]
    if (role) return `${role.display_name} (${role.title})`
    return id.slice(0, 8)
  }
  
  const initialSyncDone = useRef(false)
  
    useEffect(() => {
    // Only proceed once meeting data is loaded
    if (!meetingId || isLoading || !meeting) return;
    
    // Safety: if we switched meetings, force a reset of the store first
    if (store.activeMeetingId && store.activeMeetingId !== meetingId) {
       store.resetMeeting();
       initialSyncDone.current = false;
    }

    // Only do initial sync once to avoid loops
    if (!initialSyncDone.current) {
      store.setMeetingData({
        status: meeting.status as any,
        eventLog: meeting.meeting_log || [],
        objective: meeting.objective || "",
        typingAgentId: null,
      })
      initialSyncDone.current = true
    }
    
    store.setActiveMeetingId(meetingId)

    // If meeting is already finished, do NOT open WebSocket
    // Safety check: also consider it finished if the log contains a completion event
    const isFinished = ['completed', 'terminated', 'failed'].includes(meeting.status) || 
                       (meeting.meeting_log || []).some((e: any) => e.type === 'meeting_completed' || e.is_conclusion);
    if (isFinished) return;
    
    setSocket(null); // Clear previous socket if any before reconnecting

    // Only start WebSocket if meeting is draft, queued, or running
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    const wsUrl = `${protocol}//${host}/api/v1/meetings/${meetingId}/ws`
    const ws = new WebSocket(wsUrl)
    
    ws.onopen = () => {
      // If it was a draft, automatically start it
      if (meeting.status === 'draft') {
        ws.send(JSON.stringify({ command: 'start_meeting' }))
      }
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        store.addEvent(data)
        if (data.type === 'meeting_started') store.setStatus('running')
        if (data.type === 'meeting_completed') store.setStatus('completed')
        if (data.type === 'meeting_terminated') store.setStatus('terminated')
        if (data.type === 'error') {
          console.error("Backend Error:", data.content)
          store.setStatus('failed')
        }
      } catch (e) {
        console.error("WS Parsing Error", e)
      }
    }
    
    setSocket(ws)
    return () => ws.close()
  }, [meetingId, meeting, isLoading])

  const queryClient = useQueryClient()

  // Documents Logic
  const { data: meetingDocs = [], isLoading: isLoadingDocs } = useQuery<any[]>({
    queryKey: ['meeting-docs', meetingId],
    queryFn: () => apiClient.get<any[]>(`/documents?meeting_id=${meetingId}`),
    enabled: !!meetingId
  })

  const [isAddDocOpen, setIsAddDocOpen] = useState(false)
  const [viewingDoc, setViewingDoc] = useState<any | null>(null)
  const [docContent, setDocContent] = useState<string | null>(null)
  const [isLoadingContent, setIsLoadingContent] = useState(false)

  const fetchDocContent = async (docId: string) => {
    setIsLoadingContent(true)
    try {
      const res = await apiClient.get<any>(`/documents/${docId}/content`)
      setDocContent((res as any).content)
    } catch (e) {
      toast.error("Failed to load document content")
    } finally {
      setIsLoadingContent(false)
    }
  }

  // Helper to sync attendees into store
  useEffect(() => {
    if (roles.length > 0) {
      store.setMeetingData({ attendees: roleMap })
    }
  }, [roles, roleMap])

  const handleStop = () => socket?.send(JSON.stringify({ command: 'stop_meeting' }))
  const handleTerminate = () => socket?.send(JSON.stringify({ command: 'terminate_meeting' }))

  const colorsRef = useRef<Record<string, string>>({})
  
  const getColor = (agentId: string) => {
    if (agentId === 'supervisor') return "bg-primary/15 border-primary/30 text-primary font-bold shadow-sm"
    if (!colorsRef.current[agentId]) {
      const idx = Object.keys(colorsRef.current).length
      colorsRef.current[agentId] = CHAT_COLORS[idx % CHAT_COLORS.length]
    }
    return colorsRef.current[agentId]
  }

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const eventsEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    // Auto-scroll to bottom of chat area when new events come in
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
    eventsEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [store.eventLog, store.typingAgentId])

  // Filter for chat bubbles - keep only agent speech (supervisor goes to event log reasoning)
  const chatEvents = store.eventLog.filter((e) => 
    e.type === "agent_spoke" && !(e as any).is_conclusion
  )
  
  const conclusionEvent = store.eventLog.find((e: any) => e.is_conclusion)

  return (
    <div className="flex h-full p-4 gap-4 overflow-hidden">
      {/* LEFT: Transcript */}
      <Card className="flex-[3] flex flex-col bg-card/40 border-border backdrop-blur-md min-h-0 h-full overflow-hidden">
        <CardHeader className="flex flex-row items-center justify-between pb-2">
          <div>
            <CardTitle className="truncate" title={meeting?.objective || "Meeting Transcript"}>
              {meeting?.objective || "Meeting Transcript"}
            </CardTitle>
            <CardDescription>Simulation Session</CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className={
              store.status === 'running' ? "border-primary text-primary bg-primary/15" : 
              store.status === 'completed' ? "border-primary/40 text-primary/80 bg-primary/5" : 
              store.status === 'terminated' ? "border-amber-500/40 text-amber-500 bg-amber-500/10" :
              store.status === 'failed' ? "border-destructive/40 text-destructive bg-destructive/10" :
              "border-muted-foreground/40 text-muted-foreground bg-muted/10"
            }>
              {store.status === 'running' ? 'LIVE SIMULATION' : 
               store.status === 'completed' ? 'SIMULATION ENDED' : 
               store.status === 'terminated' ? 'SESSION TERMINATED' :
               store.status === 'failed' ? 'ERROR' : 
               'READYING FABRIC...'}
            </Badge>
            {store.status === 'running' && (
               <button onClick={handleTerminate} className="px-3 py-1 text-xs rounded bg-red-500/20 text-red-300 hover:bg-red-500/40 transition">
                 Terminate
               </button>
            )}
          </div>
        </CardHeader>
        <CardContent className="flex-1 relative overflow-hidden p-0 min-h-0 flex flex-col">
          {/* Meeting Brief Summary */}
          <div className="px-4 py-3 border-b border-border/10 bg-muted/5">
            <Accordion className="w-full">
              <AccordionItem value="brief" className="border-none">
                <AccordionTrigger className="py-0 hover:no-underline group">
                  <div className="flex items-center gap-2 text-xs font-bold text-primary/80">
                    <Info className="h-3.5 w-3.5" />
                    <span>MEETING BRIEF & OBJECTIVES</span>
                  </div>
                </AccordionTrigger>
                <AccordionContent className="pt-3 pb-1">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="space-y-3">
                      <div>
                        <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-widest mb-1">Scenario/Topic</h4>
                        <p className="text-sm font-medium text-foreground">{meeting?.brief || "Strategic Discussion"}</p>
                      </div>
                      <div>
                        <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-widest mb-1">Primary Objective</h4>
                        <p className="text-xs leading-relaxed text-foreground/80">{meeting?.objective || "No objective defined."}</p>
                      </div>
                      {meeting?.agenda && (
                        <div>
                          <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-widest mb-1">Agenda</h4>
                          <div className="text-[11px] leading-relaxed text-foreground/70 prose prose-invert prose-sm max-w-none">
                             <ReactMarkdown>{meeting.agenda}</ReactMarkdown>
                          </div>
                        </div>
                      )}
                    </div>
                    <div className="space-y-4">
                      <div>
                        <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-widest mb-1">Participants</h4>
                        <div className="flex flex-wrap gap-1.5 mt-1.5">
                          {Object.values(roleMap).map((role: any) => (
                            <Badge key={role.id} variant="secondary" className="text-[9px] bg-primary/5 border-primary/20 text-primary/90 h-5 px-1.5 font-medium">
                              {role.display_name} ({role.title})
                            </Badge>
                          ))}
                        </div>
                      </div>
                      <div>
                        <h4 className="text-[10px] font-bold text-muted-foreground uppercase tracking-widest mb-1">Documents Available</h4>
                        <div className="space-y-1.5 mt-1.5">
                          {meetingDocs.length === 0 ? (
                            <p className="text-[10px] italic text-muted-foreground">No shared documents attached.</p>
                          ) : (
                            meetingDocs.slice(0, 3).map((doc, i) => (
                              <div key={i} className="flex items-center gap-2 text-[10px] text-foreground/70">
                                <FileText className="h-3 w-3 text-primary/60" />
                                <span className="truncate">{doc.document_name}</span>
                                <Badge variant="outline" className="text-[8px] px-1 h-3.5 border-primary/10 text-primary/40 leading-none">
                                  {doc.library_scope}
                                </Badge>
                              </div>
                            ))
                          )}
                          {meetingDocs.length > 3 && (
                            <p className="text-[9px] text-primary/60 font-medium pl-5">+ {meetingDocs.length - 3} more in library tab</p>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>
                </AccordionContent>
              </AccordionItem>
            </Accordion>
          </div>

          <ScrollArea className="flex-1 min-h-0 w-full p-4">
            <div className="space-y-6">
              {chatEvents.map((evt, idx) => {
                let name = "Unknown"
                let role = ""
                let tone = ""
                let cleanContent = evt.content || ""
                const agentId = evt.agent_id || "unknown"

                if (evt.type === 'agent_spoke') {
                  const agent = roleMap[agentId]
                  if (agent) {
                    name = agent.display_name
                    role = agent.title
                    tone = agent.tone ? (Array.isArray(agent.tone) ? agent.tone.join(", ") : agent.tone) : ""
                  }

                  // Handle legacy prefixed content
                  const match = cleanContent.match(/^\[(.*?) - (.*?)\]\s([\s\S]*)$/)
                  if (match) {
                    cleanContent = match[3]
                    if (!agent) {
                       name = match[1]
                       role = match[2]
                    }
                  }

                  // Strip "Tone: ..." if present in content
                  cleanContent = cleanContent.replace(/^Tone:\s*.*?\n/i, "").trim()
                  
                  // Strip redundant "Name: " prefix if present (some LLMs do this)
                  const namePrefixRegex = new RegExp(`^${name}:\\s*`, 'i')
                  cleanContent = cleanContent.replace(namePrefixRegex, "").trim()
                }
                
                const bubbleColor = getColor(agentId)
                const isSupervisor = agentId === 'supervisor'
                const timestamp = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : new Date().toLocaleTimeString()

                return (
                  <div key={idx} className={`flex flex-col ${isSupervisor ? 'items-center' : 'items-start'} max-w-full`}>
                    <div className={`p-4 rounded-xl border max-w-[90%] flex flex-col gap-2 ${bubbleColor} shadow-sm backdrop-blur-md`}>
                      <div className="flex items-center justify-between gap-4 border-b border-border/20 pb-2 mb-1">
                        <div className="flex flex-col gap-0.5">
                          <div className="flex items-baseline gap-2">
                            <span className="font-bold text-sm tracking-tight">{name}</span>
                            {role && <span className="text-[10px] font-semibold uppercase tracking-widest opacity-70">{role}</span>}
                          </div>
                          {tone && <span className="text-[9px] italic opacity-50">tone: {tone}</span>}
                        </div>
                        <span className="text-[10px] opacity-60 font-mono self-start">{timestamp}</span>
                      </div>
                      
                      <div className="text-sm prose prose-invert prose-sm max-w-none prose-p:leading-relaxed">
                        <ReactMarkdown>{cleanContent}</ReactMarkdown>
                      </div>
                      
                      {/* Debug / Unrestricted Private Reasoning Expansion */}
                      {evt.private_reasoning && evt.private_reasoning !== "No internal tools used." && (
                        <Accordion className="w-full mt-2">
                          <AccordionItem value="debug" className="border-none">
                            <AccordionTrigger className="text-xs text-primary/80 py-1 hover:no-underline hover:text-primary">
                              Inspect Private Reasoning (Debug)
                            </AccordionTrigger>
                            <AccordionContent className="pt-2">
                                <div className="p-3 bg-muted rounded border border-primary/20 text-xs text-foreground/70 font-mono whitespace-pre-wrap overflow-x-auto">
                                  <span className="block mb-2 text-primary uppercase font-bold tracking-widest text-[10px]">! Warning: Diagnostic output. May be speculative.</span>
                                  {evt.private_reasoning}
                                </div>
                            </AccordionContent>
                          </AccordionItem>
                        </Accordion>
                      )}
                    </div>
                  </div>
                )
              })}

              {store.typingAgentId && !conclusionEvent && (
                <div className={`flex flex-col items-start max-w-full animate-in fade-in slide-in-from-bottom-4 duration-500`}>
                    <div className={`p-4 rounded-xl border max-w-[90%] flex flex-col gap-2 ${getColor(store.typingAgentId)} shadow-sm backdrop-blur-md opacity-80`}>
                      <div className="flex items-center gap-3">
                        <div className="flex gap-1">
                          <div className="h-1.5 w-1.5 bg-current rounded-full animate-bounce [animation-delay:-0.3s]" />
                          <div className="h-1.5 w-1.5 bg-current rounded-full animate-bounce [animation-delay:-0.15s]" />
                          <div className="h-1.5 w-1.5 bg-current rounded-full animate-bounce" />
                        </div>
                        <span className="text-xs font-medium italic opacity-70">
                          {store.typingAgentId === 'supervisor' ? "Supervisor orchestrating..." : `${getAgentLabel(store.typingAgentId)} preparing response...`}
                        </span>
                      </div>
                    </div>
                </div>
              )}

              {conclusionEvent && (
                <div className="mt-8 pt-8 border-t border-dashed border-border/40">
                  <div className="p-6 rounded-xl bg-primary/10 border border-primary/20 shadow-xl backdrop-blur-xl">
                    <div className="flex items-center gap-3 mb-4 border-b border-primary/20 pb-4">
                      <div className="h-8 w-8 rounded-full bg-primary/20 flex items-center justify-center border border-primary/40">
                        <span className="text-primary font-bold text-lg">✓</span>
                      </div>
                      <div>
                        <h3 className="font-bold text-lg text-foreground">Meeting Concluded</h3>
                        <p className="text-xs text-muted-foreground uppercase tracking-widest font-medium">Final Decision & Summary</p>
                      </div>
                    </div>
                    <div className="prose prose-invert prose-sm max-w-none text-foreground/90">
                      <ReactMarkdown>{conclusionEvent.content?.replace(/^\[Supervisor\]\sConcluded the meeting\.\s*/, "") || (conclusionEvent as any).reasoning}</ReactMarkdown>
                    </div>
                  </div>
                </div>
              )}
              
              {store.eventLog.length === 0 && (
                 <div className="flex flex-col items-center justify-center h-64 text-muted-foreground gap-4">
                    <div className="relative">
                       <div className="h-10 w-10 border-2 border-primary/20 rounded-full animate-ping absolute" />
                       <div className="h-10 w-10 border-2 border-primary/50 rounded-full relative bg-primary/10 flex items-center justify-center">
                         <div className="h-2 w-2 bg-primary rounded-full animate-pulse" />
                       </div>
                    </div>
                   <div className="text-center">
                     <p className="text-sm font-medium text-muted-foreground">Orchestrating Agents...</p>
                     <p className="text-[10px] uppercase tracking-tighter opacity-40 mt-1">Syncing with Nexus Data Fabric</p>
                   </div>
                   
                   {/* Manual Start Button Fallback */}
                   {store.status !== 'running' && (
                     <button 
                       onClick={() => socket?.send(JSON.stringify({ command: 'start_meeting' }))}
                       className="mt-2 px-4 py-2 bg-primary hover:bg-primary/90 text-primary-foreground text-xs font-bold rounded-lg transition-all shadow-lg"
                     >
                       Force Start Simulation
                     </button>
                   )}
                 </div>
              )}
              {/* Anchor for auto-scroll */}
              <div ref={messagesEndRef} />
            </div>
          </ScrollArea>
        </CardContent>
      </Card>      {/* RIGHT: Combined Pane */}
      <Card className="flex-[1] flex flex-col bg-card/40 border-border backdrop-blur-md overflow-hidden h-full min-h-0">
        <Tabs defaultValue="events" className="flex flex-col h-full">
            <CardHeader className="pb-0 pt-4 px-4">
                <TabsList className="grid w-full grid-cols-2 bg-muted/40 border-border/10">
                    <TabsTrigger value="events" className="text-[10px] uppercase font-bold tracking-wider">Events Log</TabsTrigger>
                    <TabsTrigger value="library" className="text-[10px] uppercase font-bold tracking-wider">Document Library</TabsTrigger>
                </TabsList>
            </CardHeader>

            <TabsContent value="events" className="flex-1 relative overflow-hidden p-0 min-h-0">
                <ScrollArea className="h-full w-full p-4">
                    <div className="space-y-2">
                        {store.eventLog.slice(-50).map((evt, i) => {
                        const label = getAgentLabel(evt.agent_id)
                        let msg = evt.type.replace(/_/g, ' ')
                        const reasoning = (evt as any).reasoning
                        
                        if (evt.type === 'meeting_started') msg = `simulation started with ID: ${evt.meeting_id || meetingId}`
                        if (evt.type === 'supervisor_selected_next_agent') msg = `Next speaker chosen: ${label}`
                        if (evt.type === 'agent_thinking') msg = `${label} is thinking...`
                        if (evt.type === 'agent_spoke' || evt.type === 'supervisor_spoke') msg = `${label} spoke`
                        
                        return (
                            <div key={i} className="text-[10px] p-2 rounded bg-muted/20 font-mono text-muted-foreground border border-border/5 flex flex-col gap-1">
                            <div className="flex justify-between items-center">
                                <span className="text-primary font-bold uppercase text-[9px]">{evt.type}</span>
                                <span className="opacity-40">{evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : ''}</span>
                            </div>
                            <div className="text-foreground/80 lowercase">{msg}</div>
                            {reasoning && (
                                <div className="mt-1 p-1.5 bg-primary/5 rounded border-l-2 border-primary/30 text-[9px] italic text-muted-foreground leading-relaxed">
                                    {reasoning}
                                </div>
                            )}
                            </div>
                        )
                        })}
                        <div ref={eventsEndRef} />
                    </div>
                </ScrollArea>
            </TabsContent>

            <TabsContent value="library" className="flex-1 relative overflow-hidden flex flex-col p-0 min-h-0">
                <div className="p-4 border-b border-border/10 bg-muted/5 flex items-center justify-between">
                    <div>
                        <h4 className="text-xs font-bold text-foreground">Attached Documents</h4>
                        <p className="text-[9px] text-muted-foreground uppercase">Available to all participants</p>
                    </div>
                    <Button variant="ghost" size="sm" className="h-7 px-2 hover:bg-primary/20 hover:text-primary transition-colors gap-1.5" onClick={() => setIsAddDocOpen(true)}>
                        <FilePlus className="h-3.5 w-3.5" />
                        <span className="text-[10px] font-bold">Add</span>
                    </Button>
                </div>
                
                <ScrollArea className="flex-1 min-h-0 w-full p-4">
                    <div className="space-y-3">
                        {meetingDocs.length === 0 && !isLoadingDocs && (
                            <div className="flex flex-col items-center justify-center py-12 text-muted-foreground opacity-40">
                                <FileText className="h-8 w-8 mb-2" />
                                <p className="text-[10px] uppercase font-bold tracking-tighter">No Active Documents</p>
                            </div>
                        )}
                        {isLoadingDocs && (
                             <div className="flex items-center justify-center py-12">
                                <Loader2 className="h-4 w-4 animate-spin text-primary" />
                             </div>
                        )}
                        {meetingDocs.map((doc, i) => (
                            <div key={i} 
                                 onClick={() => { setViewingDoc(doc); fetchDocContent(doc.id); }}
                                 className="flex items-start gap-3 p-3 rounded-lg bg-muted/20 border border-border/5 hover:border-primary/30 hover:bg-muted/40 cursor-pointer transition-all group">
                                <div className="p-2 rounded bg-primary/10 text-primary group-hover:bg-primary/20">
                                    <FileText className="h-4 w-4" />
                                </div>
                                <div className="flex-1 min-w-0">
                                    <div className="flex items-center justify-between gap-2">
                                        <h5 className="text-xs font-bold text-foreground truncate">{doc.document_name}</h5>
                                        <Badge variant="outline" className="text-[8px] h-4 py-0 px-1 border-primary/20 text-primary/80">
                                            {doc.library_scope}
                                        </Badge>
                                    </div>
                                    <div className="flex items-center gap-2 mt-1 text-[9px] text-muted-foreground">
                                        <span className="uppercase">{doc.file_type?.split('/')?.[1] || 'DOC'}</span>
                                        <span>•</span>
                                        <span>{(doc.metadata_json?.size / 1024).toFixed(1)} KB</span>
                                        {doc.owner_agent_id && (
                                            <>
                                                <span>•</span>
                                                <span className="text-primary truncate">Private to {roleMap[doc.owner_agent_id]?.title}</span>
                                            </>
                                        )}
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                </ScrollArea>
            </TabsContent>
        </Tabs>
      </Card>

      {/* MODAL: View Document */}
      <Dialog open={!!viewingDoc} onOpenChange={(open) => !open && setViewingDoc(null)}>
        <DialogContent className="max-w-4xl max-h-[80vh] flex flex-col p-0 overflow-hidden bg-card border-border backdrop-blur-3xl">
            <DialogHeader className="p-6 border-b border-border/10 bg-muted/5">
                <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3">
                        <div className="p-3 rounded-xl bg-primary/15 text-primary border border-primary/20">
                            <FileText className="h-6 w-6" />
                        </div>
                        <div>
                            <DialogTitle className="text-xl font-bold tracking-tight text-foreground">{viewingDoc?.document_name}</DialogTitle>
                            <p className="text-xs text-muted-foreground uppercase tracking-widest flex items-center gap-2 mt-1">
                                {viewingDoc?.library_scope} access
                                <span className="h-1 w-1 rounded-full bg-border" />
                                {viewingDoc?.file_type}
                            </p>
                        </div>
                    </div>
                </div>
            </DialogHeader>
            
            <div className="flex-1 overflow-hidden relative">
                <ScrollArea className="h-full w-full">
                    <div className="p-8">
                        {isLoadingContent ? (
                            <div className="flex flex-col items-center justify-center py-24 gap-4">
                                <Loader2 className="h-8 w-8 animate-spin text-primary" />
                                <p className="text-[10px] uppercase font-bold tracking-widest text-muted-foreground animate-pulse">Decrypting content...</p>
                            </div>
                        ) : [ 'text/plain', 'application/json', 'text/html', 'text/markdown', 'text/csv' ].find(t => viewingDoc?.file_type?.includes(t)) ? (
                            <div className="prose prose-invert prose-sm max-w-none prose-pre:bg-muted/50 prose-pre:border prose-pre:border-border/10">
                                <ReactMarkdown>{docContent || "No content found."}</ReactMarkdown>
                            </div>
                        ) : (
                            <div className="flex flex-col items-center justify-center py-16 gap-6 max-w-md mx-auto text-center">
                                <div className="p-6 rounded-full bg-amber-500/10 text-amber-500 border border-amber-500/20">
                                    <Info className="h-10 w-10" />
                                </div>
                                <div>
                                    <h4 className="text-lg font-bold text-foreground">Content Preview Not Supported</h4>
                                    <p className="text-sm text-muted-foreground mt-2">
                                        This file type ({viewingDoc?.file_type}) cannot be rendered directly in the transcript pane. Agents can still access it via the semantic fabric if indexed.
                                    </p>
                                </div>
                                <div className="w-full bg-muted/30 rounded-xl border border-border/10 p-4 text-left">
                                    <p className="text-[10px] uppercase font-bold text-muted-foreground mb-3 tracking-widest border-b border-border/5 pb-2">Technical Metadata</p>
                                    <pre className="text-[10px] text-primary/80 font-mono whitespace-pre-wrap">
                                        {JSON.stringify(viewingDoc?.metadata_json, null, 2)}
                                    </pre>
                                </div>
                            </div>
                        )}
                    </div>
                </ScrollArea>
                <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-card to-transparent pointer-events-none" />
            </div>

            <DialogFooter className="p-4 border-t border-border/10 bg-muted/5">
                <Button variant="outline" className="gap-2" onClick={() => setViewingDoc(null)}>Close</Button>
            </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* MODAL: Add Document */}
      <AddDocDialog 
        isOpen={isAddDocOpen}
        onOpenChange={setIsAddDocOpen}
        onDocAdded={() => queryClient.invalidateQueries({ queryKey: ['meeting-docs', meetingId] })}
        attachedDocIds={meetingDocs.map(d => d.id)}
        meetingId={meetingId}
        roles={Object.values(roleMap)}
      />
    </div>
  )
}
