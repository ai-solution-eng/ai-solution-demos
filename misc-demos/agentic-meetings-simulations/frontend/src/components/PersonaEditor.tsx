"use client"

import React, { useState, useEffect } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { apiClient } from "@/lib/api-client"
import { 
  Dialog, 
  DialogContent, 
  DialogHeader, 
  DialogTitle, 
  DialogDescription, 
  DialogFooter 
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { 
  Select, 
  SelectContent, 
  SelectItem, 
  SelectTrigger, 
  SelectValue 
} from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Loader2, Save, RotateCcw, Info } from "lucide-react"

interface Role {
  id: string;
  display_name: string;
  title: string;
  department: string;
  summary: string;
  seniority?: string;
  responsibilities: string[];
  kpis: string[];
  priorities: string[];
  objectives: string[];
  risk_tolerance: string;
  tone: string[];
  collaboration_style: string;
  challenge_style: string;
  system_prompt: string;
}

interface PersonaEditorProps {
  role: Role | null;
  isOpen: boolean;
  onClose: () => void;
}

const SENIORITY_OPTIONS = ["Executive", "Senior", "Mid-Level", "Junior"];
const RISK_OPTIONS = ["Very Low", "Low", "Medium", "High", "Very High"];
const COLLABORATION_OPTIONS = ["Direct", "Consultative", "Democratic", "Collaborative", "Individualistic", "Instructional"];
const CHALLENGE_OPTIONS = ["Analytical", "Provocative", "Constructive", "Skeptical", "Questioning", "Supportive"];
const TONE_OPTIONS = ["Professional", "Authoritative", "Data-driven", "Supportive", "Direct", "Visionary", "Pragmatic", "Analytical", "Enthusiastic", "Diplomatic", "Critical", "Inspirational"];

export const PersonaEditor = ({ role, isOpen, onClose }: PersonaEditorProps) => {
  const queryClient = useQueryClient();
  const [formData, setFormData] = useState<Partial<Role>>({});

  useEffect(() => {
    if (role) {
      const incomingTone = (role as any).tone;
      const initialTone = Array.isArray(incomingTone) 
        ? incomingTone 
        : (typeof incomingTone === 'string' ? incomingTone : "").split(",").map((t: string) => t.trim()).filter((t: string) => t !== "");

      setFormData({
        ...role,
        responsibilities: role.responsibilities || [],
        kpis: role.kpis || [],
        priorities: role.priorities || [],
        objectives: role.objectives || [],
        tone: initialTone,
      });
    }
  }, [role]);

  const mutation = useMutation({
    mutationFn: async (updatedRole: Partial<Role>) => {
        return apiClient.put(`/roles/${role?.id}`, updatedRole);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['roles'] });
      onClose();
    },
  });

  if (!role) return null;

  const handleSave = () => {
    mutation.mutate(formData);
  };

  const handleListChange = (key: keyof Role, value: string) => {
    const list = value.split('\n').map(s => s.trim()).filter(s => s !== '');
    setFormData(prev => ({ ...prev, [key]: list }));
  };

  const toggleTone = (tone: string) => {
    const currentTones = (formData.tone || []);
    let newTones;
    if (currentTones.includes(tone)) {
      newTones = currentTones.filter(t => t !== tone);
    } else {
      newTones = [...currentTones, tone];
    }
    setFormData(prev => ({ ...prev, tone: newTones }));
  };

  const revertToDefault = (key: keyof Role) => {
    if (key === 'summary') {
      setFormData(prev => ({ ...prev, summary: `The ${role.display_name} responsible for ${role.department} strategy and execution.` }));
    } else if (key === 'system_prompt') {
      setFormData(prev => ({ ...prev, system_prompt: "Always represent your department's interests strongly. You are allowed to take up to 10 turns during the meeting, and have access to previous conversations from other attendees. When needed, feel free to challenge or answer their comments regarding your department." }));
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-5xl w-[95vw] bg-card border-border text-foreground shadow-2xl p-0 overflow-hidden ring-1 ring-border/5">
        <DialogHeader className="p-8 pb-4">
          <div className="flex justify-between items-start">
            <div>
              <DialogTitle className="text-3xl font-bold tracking-tight">{role.display_name}</DialogTitle>
              <DialogDescription className="text-muted-foreground text-base mt-1">
                {role.title} <span className="mx-2 text-border">|</span> {role.department}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <Tabs defaultValue="profile" className="w-full">
          <div className="px-8 border-b border-border">
            <TabsList className="bg-transparent h-14 gap-8 p-0">
              <TabsTrigger value="profile" className="data-[state=active]:bg-transparent data-[state=active]:text-primary data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-full px-1 text-sm font-bold uppercase tracking-widest text-muted-foreground/50">Identity</TabsTrigger>
              <TabsTrigger value="style" className="data-[state=active]:bg-transparent data-[state=active]:text-primary data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-full px-1 text-sm font-bold uppercase tracking-widest text-muted-foreground/50">Behaviors</TabsTrigger>
              <TabsTrigger value="focus" className="data-[state=active]:bg-transparent data-[state=active]:text-primary data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-full px-1 text-sm font-bold uppercase tracking-widest text-muted-foreground/50">Strategic Focus</TabsTrigger>
              <TabsTrigger value="prompt" className="data-[state=active]:bg-transparent data-[state=active]:text-primary data-[state=active]:border-b-2 data-[state=active]:border-primary rounded-none h-full px-1 text-sm font-bold uppercase tracking-widest text-muted-foreground/50">Logic Prompt</TabsTrigger>
            </TabsList>
          </div>

          <ScrollArea className="h-[500px] p-8">
            <TabsContent value="profile" className="space-y-8 mt-0">
              <div className="grid grid-cols-2 gap-8">
                <div className="space-y-3">
                  <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Standard Seniority</Label>
                  <Select 
                    value={formData.seniority || ""} 
                    onValueChange={(v) => setFormData(p => ({ ...p, seniority: v || undefined }))}
                  >
                    <SelectTrigger className="bg-muted border-border h-12 focus:ring-1 focus:ring-primary/50">
                      <SelectValue placeholder="Select rank..." />
                    </SelectTrigger>
                    <SelectContent className="bg-popover border-border text-popover-foreground">
                      {SENIORITY_OPTIONS.map(o => <SelectItem key={o} value={o}>{o}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-3">
                  <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Risk Tolerance Profile</Label>
                  <Select 
                    value={formData.risk_tolerance || ""} 
                    onValueChange={(v) => setFormData(p => ({ ...p, risk_tolerance: v || undefined }))}
                  >
                    <SelectTrigger className="bg-muted border-border h-12 focus:ring-1 focus:ring-primary/50">
                      <SelectValue placeholder="Select risk level..." />
                    </SelectTrigger>
                    <SelectContent className="bg-popover border-border text-popover-foreground">
                      {RISK_OPTIONS.map(o => <SelectItem key={o} value={o}>{o}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="space-y-4">
                <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Tone of Voice (Multiselect)</Label>
                <div className="flex flex-wrap gap-2">
                  {TONE_OPTIONS.map(tone => {
                    const active = (formData.tone || []).includes(tone);
                    return (
                      <Button
                        key={tone}
                        variant="outline"
                        size="sm"
                        onClick={() => toggleTone(tone)}
                        className={`rounded-full border-border transition-all ${active ? 'bg-primary/15 border-primary/30 text-primary font-bold' : 'bg-muted text-muted-foreground hover:text-foreground hover:bg-muted/80'}`}
                      >
                        {tone}
                      </Button>
                    );
                  })}
                </div>
                <p className="text-[11px] text-muted-foreground italic">Selected: <span className="text-primary font-mono">{(formData.tone || []).join(", ") || "None"}</span></p>
              </div>

              <div className="space-y-3">
                <div className="flex justify-between items-center">
                  <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Professional Summary</Label>
                  <Button variant="ghost" size="sm" onClick={() => revertToDefault('summary')} className="h-6 text-[11px] text-primary hover:text-primary/80 gap-1.5 px-2 bg-primary/5 hover:bg-primary/10 rounded-md">
                    <RotateCcw className="h-3.5 w-3.5" /> Revert
                  </Button>
                </div>
                <Textarea 
                  value={formData.summary || ""} 
                  onChange={(e) => setFormData(p => ({ ...p, summary: e.target.value.slice(0, 500) }))}
                  placeholder="High-level description of the role's purpose..."
                  className="bg-muted border-border min-h-[120px] text-sm leading-relaxed focus:ring-1 focus:ring-primary/50"
                />
                <div className="text-[11px] text-right text-muted-foreground font-mono tracking-wider">{(formData.summary || "").length} / 500 characters</div>
              </div>
            </TabsContent>

            <TabsContent value="style" className="space-y-8 mt-0">
               <div className="space-y-6">
                  <div className="bg-primary/[0.08] border border-primary/20 rounded-xl p-5 flex gap-4 text-[13px] text-foreground/80 leading-relaxed shadow-inner font-medium">
                    <Info className="h-5 w-5 text-primary shrink-0 mt-0.5" />
                    These behavioral parameters influence how the agent interacts with other personas in a meeting. They define the "social persona" within a discussion.
                  </div>
                  <div className="grid grid-cols-2 gap-8">
                    <div className="space-y-3">
                      <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Collaboration Style</Label>
                      <Select 
                        value={formData.collaboration_style || ""} 
                        onValueChange={(v) => setFormData(p => ({ ...p, collaboration_style: v || undefined }))}
                      >
                        <SelectTrigger className="bg-muted border-border h-14 focus:ring-1 focus:ring-primary/50">
                          <SelectValue placeholder="Select style..." />
                        </SelectTrigger>
                        <SelectContent className="bg-popover border-border text-popover-foreground">
                          {COLLABORATION_OPTIONS.map(o => <SelectItem key={o} value={o}>{o}</SelectItem>)}
                        </SelectContent>
                      </Select>
                      <p className="text-[11px] text-muted-foreground font-medium">Determines approach to general consensus and teamwork.</p>
                    </div>
                    <div className="space-y-3">
                      <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Challenge Style</Label>
                      <Select 
                        value={formData.challenge_style || ""} 
                        onValueChange={(v) => setFormData(p => ({ ...p, challenge_style: v || undefined }))}
                      >
                        <SelectTrigger className="bg-muted border-border h-14 focus:ring-1 focus:ring-primary/50">
                          <SelectValue placeholder="Select style..." />
                        </SelectTrigger>
                        <SelectContent className="bg-popover border-border text-popover-foreground">
                          {CHALLENGE_OPTIONS.map(o => <SelectItem key={o} value={o}>{o}</SelectItem>)}
                        </SelectContent>
                      </Select>
                      <p className="text-[11px] text-muted-foreground font-medium">Defines how the agent handles friction and disagreements.</p>
                    </div>
                  </div>
               </div>
            </TabsContent>

            <TabsContent value="focus" className="space-y-8 mt-0">
              <div className="grid grid-cols-2 gap-8">
                <div className="space-y-3">
                  <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Core Responsibilities</Label>
                  <Textarea 
                    value={(formData.responsibilities || []).join('\n')} 
                    onChange={(e) => handleListChange('responsibilities', e.target.value)}
                    placeholder="One responsibility per line..."
                    className="bg-muted border-border min-h-[140px] text-sm leading-relaxed"
                  />
                  <p className="text-[10px] text-muted-foreground uppercase tracking-widest font-bold">Enter manually, one per line</p>
                </div>
                <div className="space-y-3">
                  <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Key Performance Indicators</Label>
                  <Textarea 
                    value={(formData.kpis || []).join('\n')} 
                    onChange={(e) => handleListChange('kpis', e.target.value)}
                    placeholder="One KPI per line..."
                    className="bg-muted border-border min-h-[140px] text-sm leading-relaxed"
                  />
                  <p className="text-[10px] text-muted-foreground uppercase tracking-widest font-bold">Primary success metrics</p>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-8">
                <div className="space-y-3">
                  <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Strategic Priorities</Label>
                  <Textarea 
                    value={(formData.priorities || []).join('\n')} 
                    onChange={(e) => handleListChange('priorities', e.target.value)}
                    placeholder="One priority per line..."
                    className="bg-muted border-border min-h-[140px] text-sm leading-relaxed"
                  />
                </div>
                <div className="space-y-3">
                  <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Quarterly Objectives</Label>
                  <Textarea 
                    value={(formData.objectives || []).join('\n')} 
                    onChange={(e) => handleListChange('objectives', e.target.value)}
                    placeholder="One objective per line..."
                    className="bg-muted border-border min-h-[140px] text-sm leading-relaxed"
                  />
                </div>
              </div>
            </TabsContent>

            <TabsContent value="prompt" className="space-y-6 mt-0">
               <div className="bg-primary/[0.08] border border-primary/20 rounded-xl p-5 flex gap-4 text-[13px] text-foreground/80 leading-relaxed shadow-inner font-medium">
                  <Info className="h-5 w-5 text-primary shrink-0 mt-0.5" />
                  This prompt defines the "internal logic" and departmental bias of the AI agent. It is hidden from other agents during the simulation.
               </div>
               <div className="space-y-3">
                <div className="flex justify-between items-center">
                  <Label className="text-xs uppercase tracking-[0.2em] text-muted-foreground font-bold">Logic Directive (System Prompt)</Label>
                  <Button variant="ghost" size="sm" onClick={() => revertToDefault('system_prompt')} className="h-6 text-[11px] text-foreground hover:text-foreground/80 gap-1.5 px-2 bg-foreground/5 hover:bg-foreground/10 rounded-md">
                    <RotateCcw className="h-3.5 w-3.5" /> Revert
                  </Button>
                </div>
                <Textarea 
                  value={formData.system_prompt || ""} 
                  onChange={(e) => setFormData(p => ({ ...p, system_prompt: e.target.value.slice(0, 2000) }))}
                  placeholder="Primary operational directives for the agent's cognition..."
                  className="bg-muted border-border min-h-[300px] text-sm font-mono leading-relaxed focus:ring-1 focus:ring-primary/50"
                />
                <div className="text-[11px] text-right text-muted-foreground font-mono tracking-wider">{(formData.system_prompt || "").length} / 2000 characters</div>
              </div>
            </TabsContent>
          </ScrollArea>
        </Tabs>

        <DialogFooter className="p-8 border-t border-border bg-card">
           <Button variant="ghost" onClick={onClose} className="rounded-full px-8 text-muted-foreground hover:text-foreground hover:bg-muted font-bold uppercase tracking-wider text-xs h-12">Cancel</Button>
           <Button 
            onClick={handleSave} 
            disabled={mutation.isPending}
            className="bg-primary hover:bg-primary/90 text-primary-foreground rounded-full px-16 h-12 font-bold uppercase tracking-widest text-xs shadow-lg shadow-primary/20 active:scale-95 transition-all"
          >
            {mutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-3" /> : <Save className="h-4 w-4 mr-3" />}
            Sync Persona
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default PersonaEditor;
