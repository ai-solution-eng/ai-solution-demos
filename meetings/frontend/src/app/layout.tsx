import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import './globals.css'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Toaster } from 'sonner'
import Sidebar from '@/components/layout/sidebar'
import QueryProvider from '@/providers/query-provider'
import SetupGuard from '@/providers/setup-guard'

import { ThemeProvider } from '@/components/ThemeProvider'

const inter = Inter({ subsets: ['latin'], variable: '--font-sans' })

export const metadata: Metadata = {
  title: 'Meeting Simulator',
  description: 'AI Persona Meeting Simulation for Enterprise',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${inter.variable} font-sans bg-background text-foreground flex h-screen overflow-hidden antialiased`}>
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          <Toaster closeButton position="top-right" richColors />
          <QueryProvider>
            <SetupGuard>
              <TooltipProvider>
                  <Sidebar />
                  <main className="flex-1 overflow-auto relative">
                      {children}
                  </main>
              </TooltipProvider>
            </SetupGuard>
          </QueryProvider>
        </ThemeProvider>
      </body>
    </html>
  )
}
