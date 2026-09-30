import React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { SidebarProvider, useSidebar } from "@/components/ui/sidebar";
import Sidebar from "./Sidebar";
import TopBar from "./TopBar";
import ImpersonationBar from "./ImpersonationBar";

const SidebarWithToggle = () => {
  const { open, openMobile, toggleSidebar, isMobile } = useSidebar();
  
  // Log state for debugging
  React.useEffect(() => {
    console.log('Sidebar state:', { isMobile, open, openMobile });
  }, [isMobile, open, openMobile]);
  
  // Determine if sidebar should be visible based on mobile state
  const isVisible = isMobile ? openMobile : open;

  // On mobile, we want to hide the sidebar by default
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => {
    setMounted(true);
  }, []);

  // Don't render anything on the server to avoid hydration mismatch
  if (!mounted) {
    return null;
  }

  return (
    <>
      {/* Sidebar - positioned absolutely on mobile, relative on desktop */}
      <aside 
        className={`fixed inset-y-0 left-0 z-40 w-64 bg-sidebar transition-transform duration-300 ease-in-out ${
          isMobile ? (isVisible ? 'translate-x-0' : '-translate-x-full') : 'translate-x-0'
        } md:relative`}
      >
        <Sidebar />
      </aside>
      
      {/* Backdrop for mobile */}
      {isMobile && isVisible && (
        <div 
          className="fixed inset-0 z-30 bg-black/50 md:hidden"
          onClick={toggleSidebar}
        />
      )}
    </>
  );
};

interface LayoutProps {
  children: React.ReactNode;
}

const Layout: React.FC<LayoutProps> = ({ children }) => {
  const { isAuthenticated, isLoading } = useAuth();
  
  // Show loading state while checking auth on initial load
  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-primary"></div>
      </div>
    );
  }

  // If not logged in, render the login page
  if (!isAuthenticated) {
    return <>{children}</>;
  }

  return (
    <SidebarProvider>
      <div className="min-h-screen flex flex-col bg-background">
        <TopBar />
        <ImpersonationBar />
        
        <div className="relative flex flex-1 overflow-hidden">
          <SidebarWithToggle />
          
          {/* Main content */}
          <main className="flex-1 overflow-auto">
            <div className="max-w-7xl mx-auto p-4 md:p-6 w-full">
              {children}
            </div>
          </main>
        </div>
      </div>
    </SidebarProvider>
  );
};

export default Layout;
