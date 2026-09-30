
import React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useTask } from "@/contexts/task";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import AdminDashboard from "./AdminDashboard";
import ManagerDashboard from "./ManagerDashboard";
import MentorDashboard from "./MentorDashboard";
import NewHireDashboard from "./NewHireDashboard";

const Dashboard: React.FC = () => {
  const { user } = useAuth();
  
  if (!user) return null;

  switch (user.role) {
    case "admin":
      return <AdminDashboard />;
    case "manager":
      return <ManagerDashboard />;
    case "mentor":
      return <MentorDashboard />;
    case "new-hire":
      return <NewHireDashboard />;
    default:
      return (
        <div className="container mx-auto">
          <h1 className="text-2xl font-bold mb-6">Dashboard</h1>
          <p>Unknown role. Please contact an administrator.</p>
        </div>
      );
  }
};

export default Dashboard;
