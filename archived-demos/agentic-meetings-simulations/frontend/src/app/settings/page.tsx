import SettingsForm from "@/components/settings/SettingsForm"

export default function SettingsPage() {
  return (
    <div className="container mx-auto p-8 max-w-4xl space-y-8 animate-in fade-in duration-500 pb-20">
      <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
        <div>
          <h1 className="text-4xl font-bold text-foreground mb-2 tracking-tight">Settings</h1>
          <p className="text-muted-foreground text-lg">Configure the autonomous meeting agent.</p>
        </div>
      </div>

      <SettingsForm />
    </div>
  )
}
