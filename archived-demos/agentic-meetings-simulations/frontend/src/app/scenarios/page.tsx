"use client"
import TemplateManager from "@/components/settings/TemplateManager"

export default function ScenariosPage() {
  return (
    <div className="container mx-auto p-8 max-w-6xl space-y-8 animate-in fade-in duration-500 pb-20">
      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
        <div>
          <h1 className="text-4xl font-bold text-foreground mb-2 tracking-tight">Meeting Scenarios</h1>
          <p className="text-muted-foreground text-lg italic">Design and manage simulation blueprints for enterprise narratives.</p>
        </div>
      </div>

      <TemplateManager />
    </div>
  )
}
