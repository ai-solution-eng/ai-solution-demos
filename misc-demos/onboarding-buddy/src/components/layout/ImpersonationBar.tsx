
import React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { AlertTriangle, ArrowLeft } from "lucide-react";

const ImpersonationBar: React.FC = () => {
  const { isImpersonating, impersonatedUser, stopImpersonation } = useAuth();
  
  if (!isImpersonating || !impersonatedUser) {
    return null;
  }
  
  return (
    <div className="bg-amber-500 py-1 px-4 text-white flex justify-between items-center">
      <div className="flex items-center">
        <AlertTriangle size={16} className="mr-2" />
        <span>
          You are currently viewing as <strong>{impersonatedUser.name}</strong> (
          {impersonatedUser.role === "mentor" && "Mentor"}
          {impersonatedUser.role === "new-hire" && "New Hire"}
          )
        </span>
      </div>
      <Button 
        variant="outline" 
        size="sm"
        onClick={stopImpersonation}
        className="bg-white hover:bg-white hover:text-amber-600 text-amber-600 border-white"
      >
        <ArrowLeft size={14} className="mr-1" />
        Return to your account
      </Button>
    </div>
  );
};

export default ImpersonationBar;
