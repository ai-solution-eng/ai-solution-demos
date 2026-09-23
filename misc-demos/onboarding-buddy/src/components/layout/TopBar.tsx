import React from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { LogOut, Menu } from "lucide-react";
import { useSidebar } from "@/components/ui/sidebar";
import { toast } from "sonner";

const TopBar: React.FC = () => {
  const { logout, user } = useAuth();
  const { toggleSidebar, isMobile } = useSidebar();
  const navigate = useNavigate();
  
  const handleMenuClick = () => {
    console.log('Menu clicked, isMobile:', isMobile);
    toggleSidebar();
  };
  
  const handleLogout = async () => {
    try {
      const success = await logout();
      if (success) {
        navigate('/login', { replace: true });
      }
    } catch (error) {
      console.error('Error during logout:', error);
      toast.error('Failed to log out');
    }
  };

  return (
    <header className="h-16 border-b border-border bg-background flex items-center justify-between px-4 md:px-6 w-full sticky top-0 z-50">
      <div className="flex items-center">
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden mr-2"
          onClick={handleMenuClick}
        >
          <Menu className="h-5 w-5" />
          <span className="sr-only">Toggle Sidebar</span>
        </Button>
        <div className="text-xl font-bold">Onboarding Buddy</div>
      </div>
      <div className="flex-1" />
      <div className="flex items-center space-x-4">
        <div className="hidden md:block">
          <span className="text-sm text-muted-foreground">Signed in as </span>
          <span className="text-sm font-medium">{user?.email}</span>
        </div>
        <Button variant="ghost" size="sm" onClick={handleLogout}>
          <LogOut size={18} className="mr-2" />
          Logout
        </Button>
      </div>
    </header>
  );
};

export default TopBar;
