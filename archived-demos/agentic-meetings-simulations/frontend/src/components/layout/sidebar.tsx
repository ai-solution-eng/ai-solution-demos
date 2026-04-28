"use client"
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { Settings, Users, FileText, History, MonitorPlay } from 'lucide-react'
import { ThemeToggle } from './ThemeToggle'

export default function Sidebar() {
  const pathname = usePathname()
  
  const navItems = [
    { name: 'Simulator', href: '/', icon: MonitorPlay },
    { name: 'Roles', href: '/roles', icon: Users },
    { name: 'Scenarios', href: '/scenarios', icon: FileText },
    { name: 'Settings', href: '/settings', icon: Settings },
  ]
  
  return (
    <aside className="w-64 border-r border-border bg-sidebar backdrop-blur-md flex flex-col h-full shrink-0">
      <div className="p-8">
        <h1 className="text-xl font-extrabold text-primary tracking-tight">Nexus Global</h1>
        <p className="text-[10px] text-muted-foreground mt-1 tracking-widest font-bold opacity-60">MEETING SIMULATOR</p>
      </div>
      
      <nav className="flex-1 px-4 mt-2 space-y-1">
        {navItems.map((item) => {
          const isActive = pathname === item.href || (pathname.startsWith(item.href) && item.href !== '/')
          
          return (
            <Link 
              key={item.name} 
              href={item.href}
              className={`flex items-center gap-3 px-4 py-3 rounded-lg transition-all duration-300 group relative overflow-hidden ${isActive ? 'bg-primary/10 text-primary shadow-sm' : 'text-muted-foreground hover:bg-muted/40 hover:text-foreground'}`}
            >
              <item.icon className={`h-4.5 w-4.5 ${isActive ? 'text-primary' : 'group-hover:text-foreground opacity-70'}`} />
              <span className={`text-sm ${isActive ? 'font-bold' : 'font-medium'}`}>{item.name}</span>
              {isActive && (
                 <div className="absolute left-0 top-2 bottom-2 w-1.5 bg-primary rounded-full"></div>
              )}
            </Link>
          )
        })}
      </nav>
      
      <div className="p-4 border-t border-border flex items-center justify-between">
        <div className="text-xs text-muted-foreground">
           HPE PCAI
        </div>
        <ThemeToggle />
      </div>
    </aside>
  )
}
