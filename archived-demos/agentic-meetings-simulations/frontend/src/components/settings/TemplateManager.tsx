"use client"
import { useState } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { apiClient } from "@/lib/api-client"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { 
    Loader2, 
    Plus, 
    Edit2, 
    Trash2, 
    Layout, 
    Check, 
    X, 
    Lock,
    Users,
    FileText,
    Target,
    ListTodo,
    Sparkles
} from "lucide-react"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogFooter,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog"
import { Checkbox } from "@/components/ui/checkbox"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from "@/components/ui/tooltip"

interface Template {
    id: string;
    name: string;
    description: string;
    brief: string;
    objective: string;
    expectations: string;
    agenda: string;
    default_selected_attendee_ids: string[];
    default_document_ids: string[];
    is_builtin: boolean;
}

interface Role {
    id: string;
    display_name: string;
    title: string;
    department: string;
}

export default function TemplateManager() {
    const queryClient = useQueryClient()
    const [isDialogOpen, setIsDialogOpen] = useState(false)
    const [editingTemplate, setEditingTemplate] = useState<Partial<Template> | null>(null)
    const [isDeleting, setIsDeleting] = useState<string | null>(null)

    const { data: templates, isLoading: loadingTemplates } = useQuery<Template[]>({
        queryKey: ['templates'],
        queryFn: () => apiClient.get<Template[]>('/templates')
    })

    const { data: roles } = useQuery<Role[]>({
        queryKey: ['roles'],
        queryFn: () => apiClient.get<Role[]>('/roles')
    })

    const saveMutation = useMutation({
        mutationFn: (data: Partial<Template>) => {
            if (data.id) {
                return apiClient.put(`/templates/${data.id}`, data)
            }
            return apiClient.post('/templates', data)
        },
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['templates'] })
            toast.success("Template Saved", { description: "The scenario template has been updated." })
            setIsDialogOpen(false)
            setEditingTemplate(null)
        },
        onError: (err: any) => {
            toast.error("Save Failed", { description: err.message })
        }
    })

    const deleteMutation = useMutation({
        mutationFn: (id: string) => apiClient.delete(`/templates/${id}`),
        onSuccess: () => {
            queryClient.invalidateQueries({ queryKey: ['templates'] })
            toast.success("Template Deleted", { description: "The scenario template has been removed." })
            setIsDeleting(null)
        },
        onError: (err: any) => {
            toast.error("Delete Failed", { description: err.message })
            setIsDeleting(null)
        }
    })

    const handleEdit = (template: Template) => {
        if (template.is_builtin) {
            toast.error("Access Restricted", { description: "Built-in templates are immutable." })
            return
        }
        setEditingTemplate(template)
        setIsDialogOpen(true)
    }

    const handleNew = () => {
        setEditingTemplate({
            name: "",
            description: "",
            brief: "",
            objective: "",
            expectations: "",
            agenda: "",
            default_selected_attendee_ids: [],
            default_document_ids: [],
            is_builtin: false
        })
        setIsDialogOpen(true)
    }

    const toggleAttendee = (roleId: string) => {
        if (!editingTemplate) return
        const current = editingTemplate.default_selected_attendee_ids || []
        const next = current.includes(roleId)
            ? current.filter(id => id !== roleId)
            : [...current, roleId]
        setEditingTemplate({ ...editingTemplate, default_selected_attendee_ids: next })
    }

    if (loadingTemplates) {
        return (
            <div className="flex flex-col items-center justify-center p-20 text-muted-foreground gap-4 bg-muted/5 rounded-2xl border border-dashed border-border/50">
                <Loader2 className="animate-spin h-8 w-8 text-primary/40" />
                <p className="italic">Loading scenario blueprints...</p>
            </div>
        )
    }

    return (
        <div className="space-y-6 animate-in slide-in-from-bottom-4 duration-500">
            <div className="flex items-center justify-between">
                <div>
                    <h3 className="text-2xl font-bold text-foreground">Scenario Blueprints</h3>
                    <p className="text-muted-foreground text-sm">Define meeting templates with objectives and default personas.</p>
                </div>
                <Button 
                    onClick={handleNew}
                    className="bg-primary hover:bg-primary/90 text-primary-foreground rounded-full gap-2 shadow-lg shadow-primary/20 transition-all active:scale-95"
                >
                    <Plus className="h-4 w-4" /> Create Template
                </Button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {templates?.map((template) => (
                    <Card key={template.id} className="bg-card/30 border-border/50 backdrop-blur-sm group hover:border-primary/30 transition-all overflow-hidden relative shadow-xl">
                        {template.is_builtin && (
                            <div className="h-1 absolute top-0 left-0 right-0 bg-primary/40" />
                        )}
                        <CardHeader className="pb-3">
                            <div className="flex justify-between items-start">
                                <div className="space-y-1">
                                    <div className="flex items-center gap-2">
                                        <CardTitle className="text-lg font-bold group-hover:text-primary transition-colors">{template.name}</CardTitle>
                                        {template.is_builtin ? (
                                            <Badge variant="secondary" className="bg-primary/10 text-primary/70 border-primary/20 text-[9px] uppercase tracking-wider font-bold">Built-in</Badge>
                                        ) : (
                                            <Badge variant="outline" className="text-[9px] uppercase tracking-wider font-bold border-muted-foreground/30 text-muted-foreground">Custom</Badge>
                                        )}
                                    </div>
                                    <CardDescription className="line-clamp-2 italic text-muted-foreground/80">{template.description}</CardDescription>
                                </div>
                                <div className="flex gap-2">
                                    {!template.is_builtin ? (
                                        <>
                                            <Button 
                                                variant="ghost" 
                                                size="icon" 
                                                onClick={() => handleEdit(template)}
                                                className="h-8 w-8 rounded-full hover:bg-primary/10 hover:text-primary transition-all"
                                            >
                                                <Edit2 className="h-3.5 w-3.5" />
                                            </Button>
                                            <Button 
                                                variant="ghost" 
                                                size="icon" 
                                                onClick={() => {
                                                    if(confirm("Confirm deletion of this blueprint?")) {
                                                        setIsDeleting(template.id)
                                                        deleteMutation.mutate(template.id)
                                                    }
                                                }}
                                                disabled={isDeleting === template.id}
                                                className="h-8 w-8 rounded-full hover:bg-destructive/10 hover:text-destructive transition-all"
                                            >
                                                {isDeleting === template.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                                            </Button>
                                        </>
                                    ) : (
                                        <TooltipProvider>
                                            <Tooltip>
                                                <TooltipTrigger>
                                                    <div className="p-2 text-muted-foreground/40 bg-muted/20 rounded-full border border-border/50">
                                                        <Lock className="h-3.5 w-3.5" />
                                                    </div>
                                                </TooltipTrigger>
                                                <TooltipContent side="top">
                                                    System level template - Immutable
                                                </TooltipContent>
                                            </Tooltip>
                                        </TooltipProvider>
                                    )}
                                </div>
                            </div>
                        </CardHeader>
                        <CardContent className="space-y-4 pt-0">
                            <div className="flex flex-col gap-2 pt-2 border-t border-border/40 text-xs">
                                <div className="text-muted-foreground"><span className="font-bold text-foreground">Objective:</span> {template.objective || "Not defined"}</div>
                                <div className="text-muted-foreground"><span className="font-bold text-foreground">Brief:</span> <span className="line-clamp-2">{template.brief || "Not defined"}</span></div>
                                <div className="text-muted-foreground"><span className="font-bold text-foreground">Expectations:</span> {template.expectations || "Not defined"}</div>
                            </div>
                            <div className="flex flex-wrap gap-2 pt-2 border-t border-border/40">
                                <Badge variant="secondary" className="bg-muted/50 text-[10px] gap-1 px-2 py-0.5 border-border/50">
                                    <Users className="h-3 w-3" /> {template.default_selected_attendee_ids?.length || 0} Roles
                                </Badge>
                            </div>
                        </CardContent>
                    </Card>
                ))}
            </div>

            <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
                <DialogContent className="max-w-4xl bg-card border-border shadow-2xl backdrop-blur-2xl max-h-[90vh] flex flex-col p-0 overflow-hidden">
                    <DialogHeader className="p-8 pb-4 border-b border-border/50">
                        <div className="flex items-center gap-3 mb-1">
                            <div className="p-2.5 bg-primary/10 rounded-xl">
                                <Sparkles className="h-6 w-6 text-primary" />
                            </div>
                            <div>
                                <DialogTitle className="text-2xl font-bold">{editingTemplate?.id ? "Edit Blueprint" : "Forge New Blueprint"}</DialogTitle>
                                <DialogDescription className="text-muted-foreground">Configure the structural parameters of this meeting scenario.</DialogDescription>
                            </div>
                        </div>
                    </DialogHeader>

                    <ScrollArea className="flex-1 px-8 py-6">
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-12 gap-y-8">
                            <div className="space-y-6">
                                <div className="space-y-2">
                                    <Label className="text-[10px] font-bold uppercase tracking-widest text-primary/70">Blueprint Name</Label>
                                    <Input 
                                        placeholder="e.g. Executive Governance Sync"
                                        value={editingTemplate?.name || ""}
                                        onChange={(e) => setEditingTemplate(prev => ({ ...prev!, name: e.target.value }))}
                                        className="bg-muted/50 border-input h-11 text-sm focus:ring-primary/20"
                                    />
                                </div>
                                <div className="space-y-2">
                                    <Label className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground/70">Summary Description</Label>
                                    <Input 
                                        placeholder="Short punchy summary for the selection screen."
                                        value={editingTemplate?.description || ""}
                                        onChange={(e) => setEditingTemplate(prev => ({ ...prev!, description: e.target.value }))}
                                        className="bg-muted/50 border-input h-11 text-sm"
                                    />
                                </div>
                                <div className="space-y-2">
                                    <Label className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground/70 flex items-center gap-2">
                                        <FileText className="h-3 w-3" /> Contextual Brief
                                    </Label>
                                    <Textarea 
                                        placeholder="Provide deep context for the agents. What has happened? Why are they meeting?"
                                        value={editingTemplate?.brief || ""}
                                        onChange={(e) => setEditingTemplate(prev => ({ ...prev!, brief: e.target.value }))}
                                        className="min-h-[140px] bg-muted/30 border-input text-xs leading-relaxed font-mono"
                                    />
                                </div>
                                <div className="space-y-2">
                                    <Label className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground/70 flex items-center gap-2">
                                        <Target className="h-3 w-3" /> Mission Objective
                                    </Label>
                                    <Input 
                                        placeholder="What is the singular goal of this meeting?"
                                        value={editingTemplate?.objective || ""}
                                        onChange={(e) => setEditingTemplate(prev => ({ ...prev!, objective: e.target.value }))}
                                        className="bg-muted/40 border-input h-11 text-sm italic"
                                    />
                                </div>
                            </div>

                            <div className="space-y-6">
                                <div className="space-y-2">
                                    <Label className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground/70 flex items-center gap-2">
                                        <ListTodo className="h-3 w-3" /> Structured Agenda
                                    </Label>
                                    <Textarea 
                                        placeholder="1. Topic A\n2. Topic B..."
                                        value={editingTemplate?.agenda || ""}
                                        onChange={(e) => setEditingTemplate(prev => ({ ...prev!, agenda: e.target.value }))}
                                        className="min-h-[120px] bg-muted/30 border-input text-xs font-mono leading-relaxed"
                                    />
                                </div>
                                <div className="space-y-2">
                                    <Label className="text-[10px] font-bold uppercase tracking-widest text-muted-foreground/70">Success Expectations</Label>
                                    <Input 
                                        placeholder="e.g. Signed off proposal by EOD"
                                        value={editingTemplate?.expectations || ""}
                                        onChange={(e) => setEditingTemplate(prev => ({ ...prev!, expectations: e.target.value }))}
                                        className="bg-muted/30 border-input h-11 text-xs italic"
                                    />
                                </div>

                                <div className="space-y-4 pt-4 border-t border-border/50">
                                    <Label className="text-[10px] font-bold uppercase tracking-widest text-primary/70 flex items-center gap-2">
                                        <Users className="h-3 w-3" /> Default Personnel
                                    </Label>
                                    <div className="grid grid-cols-2 gap-2 max-h-[180px] overflow-y-auto pr-2 custom-scrollbar">
                                        {roles?.map(role => (
                                            <div 
                                                key={role.id}
                                                className={`flex items-center gap-2 p-2 rounded-lg border transition-all cursor-pointer ${
                                                    editingTemplate?.default_selected_attendee_ids?.includes(role.id)
                                                    ? 'bg-primary/10 border-primary/40 ring-1 ring-primary/20'
                                                    : 'bg-muted/20 border-border/50 hover:bg-muted/40'
                                                }`}
                                                onClick={() => toggleAttendee(role.id)}
                                            >
                                                <Checkbox 
                                                    checked={editingTemplate?.default_selected_attendee_ids?.includes(role.id)}
                                                    onCheckedChange={() => toggleAttendee(role.id)}
                                                    className="border-primary/30 data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground"
                                                />
                                                <div className="grid">
                                                    <span className="text-[9px] font-bold truncate leading-none mb-0.5">{role.display_name}</span>
                                                    <span className="text-[8px] text-muted-foreground truncate leading-none uppercase tracking-tighter">{role.department}</span>
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>
                        </div>
                    </ScrollArea>

                    <DialogFooter className="p-8 pt-4 border-t border-border/50 bg-muted/10">
                        <Button 
                            variant="outline" 
                            onClick={() => setIsDialogOpen(false)}
                            className="rounded-full px-6 h-11 border-border text-muted-foreground hover:bg-muted font-semibold"
                        >
                            <X className="h-4 w-4 mr-2" /> Discard
                        </Button>
                        <Button 
                            onClick={() => editingTemplate && saveMutation.mutate(editingTemplate)}
                            disabled={saveMutation.isPending}
                            className="rounded-full px-12 h-11 bg-primary hover:bg-primary/90 text-primary-foreground font-bold shadow-lg shadow-primary/20 transition-all active:scale-95"
                        >
                            {saveMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : <Check className="h-4 w-4 mr-2" />}
                            {editingTemplate?.id ? "Update Blueprint" : "Seal Blueprint"}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    )
}

