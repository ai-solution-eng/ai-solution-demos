"use client"
import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { apiClient } from "@/lib/api-client"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Loader2, Users, Edit3, Shield, Activity, Target } from "lucide-react"
import { PersonaEditor } from "@/components/PersonaEditor"

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

export default function RolesPage() {
  const { data: roles = [], isLoading, isError } = useQuery<Role[]>({ 
    queryKey: ['roles'], 
    queryFn: () => apiClient.get<Role[]>('/roles') 
  })

  const [selectedRole, setSelectedRole] = useState<Role | null>(null);
  const [isEditorOpen, setIsEditorOpen] = useState(false);

  const handleEdit = (role: Role) => {
    setSelectedRole(role);
    setIsEditorOpen(true);
  };

  if (isLoading) return (
    <div className="p-8 flex flex-col items-center justify-center gap-4 h-[60vh]">
      <Loader2 className="animate-spin text-primary h-8 w-8" /> 
      <span className="text-muted-foreground animate-pulse font-medium tracking-tight">Accessing Agent Roles...</span>
    </div>
  )

  if (isError) return (
    <div className="p-8 flex flex-col items-center justify-center gap-4 h-[60vh]">
        <div className="bg-red-500/20 p-6 rounded-full border border-red-500/20">
            <Users className="h-10 w-10 text-red-500" />
        </div>
        <div className="text-center">
            <h3 className="text-2xl font-bold text-red-400 tracking-tight">Directory Access Failed</h3>
            <p className="text-muted-foreground text-sm max-w-xs mt-2 leading-relaxed">The Nexus Global agent roles is currently offline or unreachable.</p>
        </div>
    </div>
  )

  return (
    <div className="container mx-auto p-8 max-w-7xl animate-in fade-in duration-700">
      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4 mb-10 border-b border-border pb-8">
        <div>
          <h1 className="text-4xl font-bold tracking-tight text-foreground mb-2">Agent Roles</h1>
          <p className="text-muted-foreground text-lg">Central registry of AI agents representing organisational departments.</p>
        </div>
        <div className="flex items-center gap-3 bg-muted px-4 py-2 rounded-full border border-border">
          <Badge variant="outline" className="bg-primary/10 text-primary border-none px-3 font-mono">{roles.length}</Badge>
          <span className="text-xs uppercase tracking-widest text-muted-foreground/30 font-bold">Active Agents</span>
        </div>
      </div>
      
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {roles.map((role: Role) => (
          <Card key={role.id} className="bg-card border-border/50 hover:border-primary/40 transition-all duration-500 group relative flex flex-col shadow-lg hover:shadow-2xl border">
            <div className={`h-1 absolute top-0 left-0 right-0 opacity-20 rounded-t-xl bg-gradient-to-r from-primary to-transparent`} />
            
            <CardHeader className="pb-3 flex-none">
              <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-xl font-bold text-foreground tracking-tight">{role.display_name}</CardTitle>
                    <Badge variant="secondary" className="bg-primary/10 text-primary border-none mt-2 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider">{role.department}</Badge>
                  </div>
                  <Button 
                    variant="ghost" 
                    size="icon" 
                    onClick={() => handleEdit(role)}
                    className="h-10 w-10 rounded-full hover:bg-accent text-muted-foreground hover:text-foreground transition-all opacity-0 group-hover:opacity-100"
                  >
                    <Edit3 className="h-4 w-4" />
                  </Button>
              </div>
              <CardDescription className="text-muted-foreground font-semibold text-sm mt-3">{role.title}</CardDescription>
            </CardHeader>
            <CardContent className="flex-1 flex flex-col">
              <p className="text-sm text-muted-foreground line-clamp-3 leading-relaxed mb-6 italic font-medium">"{role.summary}"</p>
              
              <div className="mt-auto pt-6 border-t border-border space-y-4">
                 <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-1">
                      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground/60 font-bold">
                        <Activity className="h-3 w-3" /> Tone
                      </div>
                      <div className="text-xs text-foreground/80 font-medium truncate">{(role.tone || []).join(", ")}</div>
                    </div>
                    <div className="space-y-1">
                      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground/60 font-bold">
                        <Shield className="h-3 w-3" /> Risk
                      </div>
                      <div className="text-xs text-muted-foreground font-medium">{role.risk_tolerance}</div>
                    </div>
                 </div>
                 
                 <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-1">
                      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-widest text-muted-foreground/60 font-bold">
                        <Target className="h-3 w-3" /> KPIs
                      </div>
                      <div className="text-[10px] text-muted-foreground">{role.kpis?.length || 0} performance metrics</div>
                    </div>
                 </div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <PersonaEditor 
        role={selectedRole} 
        isOpen={isEditorOpen} 
        onClose={() => {
          setIsEditorOpen(false);
          setSelectedRole(null);
        }} 
      />
    </div>
  )
}
