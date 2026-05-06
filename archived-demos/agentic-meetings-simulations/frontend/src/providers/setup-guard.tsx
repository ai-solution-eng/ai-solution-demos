"use client"
import { type ReactNode, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/lib/api-client'
import { Loader2, ShieldCheck, Zap, Laptop, Database, ShieldAlert } from 'lucide-react'
import { Button } from '@/components/ui/button'
import SettingsForm from '@/components/settings/SettingsForm'
import { toast } from 'sonner'


interface SystemStatus {
  configured: boolean;
  ready: boolean;
  db_configured: boolean;
  inference_configured: boolean;
  embedding_configured: boolean;
  reason?: string;
  error?: string;
  last_op?: {
    status: 'pending' | 'success' | 'error';
    type: 'setup' | 'reset';
    message?: string;
    timestamp?: string;
  };
}

export default function SetupGuard({ children }: { children: ReactNode }) {

  const { data: status, isLoading, refetch } = useQuery<SystemStatus>({ 
      queryKey: ['system_status'], 
      queryFn: () => apiClient.get<SystemStatus>('/system/status').catch(() => ({ 
        ready: false, 
        db_configured: false, 
        inference_configured: false, 
        embedding_configured: false 
      } as SystemStatus)),
      retry: 2,
      refetchInterval: (query) => {
          const data = query.state.data as SystemStatus | undefined;
          // Continue polling if initializing or if last op is pending
          if (data?.last_op?.status === 'pending') return 2000;
          return (data && data.configured) ? false : 3000;
      }
  })

  useEffect(() => {
    if (status?.reason === 'no_tables' || status?.reason === 'no_data') {
        const isData = status?.reason === 'no_data';
        toast.warning(isData ? "Database Registry Empty" : "Database Topology Missing", {
            description: isData 
              ? "The database is ready but contains no meeting identities or templates. Please run the initialization/seed process." 
              : "The database is reachable but contains no infrastructure. Please initialize/re-create the database to continue.",
            duration: 10000,
        });
    }
  }, [status?.reason]);




  if (isLoading || status?.reason === 'initializing' || status?.last_op?.status === 'pending') {
    const isError = status?.last_op?.status === 'error';
    return (
      <div className="fixed inset-0 bg-zinc-950 flex flex-col items-center justify-center z-[9999] gap-6">
        <div className="relative">
            <div className={`h-16 w-16 border-2 ${isError ? 'border-red-500/20' : 'border-indigo-500/20'} rounded-full animate-ping absolute`} />
            <div className={`h-16 w-16 border-2 ${isError ? 'border-red-500/50 bg-red-500/10' : 'border-indigo-500/50 bg-indigo-500/10'} rounded-full flex items-center justify-center`}>
                {isError ? <ShieldAlert className="h-8 w-8 text-red-500" /> : <Loader2 className="h-8 w-8 animate-spin text-indigo-400" />}
            </div>
        </div>
        <div className="text-center font-mono">
            <p className={`text-sm tracking-widest uppercase mb-1 ${isError ? 'text-red-500' : 'text-white/60 animate-pulse'}`}>
                {isError ? 'Fabric Synchronization Failed' : status?.reason === 'initializing' ? 'Synchronizing Data Fabric' : 'Authenticating System Fabric'}
            </p>
            {isError ? (
                <>
                <p className="text-red-300/40 text-[10px] max-w-sm mx-auto mb-4">
                    {status?.last_op?.message || 'Unknown infrastructure error. Check backend logs for details.'}
                </p>
                <Button variant="outline" size="sm" onClick={() => refetch()} className="border-red-500/20 text-red-400 hover:bg-red-500/10 h-8 text-[10px] uppercase font-bold tracking-widest">
                    Retry Authentication
                </Button>
                </>
            ) : (
                <p className="text-white/20 text-[10px]">
                    {status?.reason === 'initializing' ? 'Applying migrations and seeding initial state...' : 'Establishing secure connection to clusters...'}
                </p>
            )}
        </div>
      </div>
    )
  }


  if (status?.configured) {
    return <>{children}</>
  }

  return (
    <div className="fixed inset-0 bg-[#050505] z-[9999] overflow-auto selection:bg-indigo-500/30">
      {/* Background Decor */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none">
          <div className="absolute top-[-10%] left-[-10%] w-[40%] h-[40%] bg-indigo-900/10 rounded-full blur-[120px]" />
          <div className="absolute bottom-[-10%] right-[-10%] w-[40%] h-[40%] bg-blue-900/10 rounded-full blur-[120px]" />
      </div>

      <div className="relative container max-w-4xl mx-auto py-20 px-6 font-sans">
        <div className="flex flex-col items-center text-center mb-16 space-y-6">
            <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-indigo-500/10 border border-indigo-500/20 text-[10px] font-bold text-indigo-400 uppercase tracking-widest animate-in fade-in slide-in-from-top-4 duration-700">
                <ShieldCheck className="h-3 w-3" />
                System Initialization Required
            </div>
            
            <h1 className="text-6xl font-black text-white tracking-tighter sm:text-7xl">
                NEXUS <span className="text-transparent bg-clip-text bg-gradient-to-r from-indigo-400 to-blue-400">MEETINGS</span>
            </h1>
            
            <p className="text-white/40 text-lg max-w-xl leading-relaxed">
                Welcome to the next generation of collaborative intelligence. 
                Configure your data fabric and cognitive endpoints to begin.
            </p>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-8 w-full max-w-2xl pt-4">
                <div className="flex flex-col items-center gap-2 p-4 rounded-2xl bg-white/5 border border-white/5">
                    <Database className="h-6 w-6 text-indigo-400" />
                    <span className="text-[10px] font-bold text-white/40 uppercase">Persistence</span>
                </div>
                <div className="flex flex-col items-center gap-2 p-4 rounded-2xl bg-white/5 border border-white/5">
                    <Zap className="h-6 w-6 text-blue-400" />
                    <span className="text-[10px] font-bold text-white/40 uppercase">Cognition</span>
                </div>
                <div className="flex flex-col items-center gap-2 p-4 rounded-2xl bg-white/5 border border-white/5">
                    <Laptop className="h-6 w-6 text-emerald-400" />
                    <span className="text-[10px] font-bold text-white/40 uppercase">Orchestration</span>
                </div>
            </div>
        </div>

        <div className="animate-in fade-in slide-in-from-bottom-8 duration-1000 delay-300">
            <SettingsForm isInitialSetup onSuccess={() => refetch()} />
        </div>

        <footer className="mt-20 text-center py-10 border-t border-white/5">
            <p className="text-white/10 text-[10px] font-mono tracking-widest uppercase">
                &copy; 2026 Nexus Strategic Group • Advanced Agentic Coding
            </p>
        </footer>
      </div>
    </div>
  )
}
