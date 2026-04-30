
import React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useTask } from "@/contexts/task";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, CardFooter } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { CheckSquare, Clock, ClipboardList } from "lucide-react";

const NewHireDashboard: React.FC = () => {
  const { user } = useAuth();
  const { personalizedTasks } = useTask();
  
  if (!user) return null;

  // Filter tasks assigned to this new hire
  const myTasks = Object.values(personalizedTasks).filter(task => task.assignedTo === user.id);
  
  const assignedTasks = myTasks.filter(task => task.status === "assigned");
  const inProgressTasks = myTasks.filter(task => task.status === "in-progress");
  const completedTasks = myTasks.filter(task => task.status === "completed");

  // Calculate progress
  const totalTasks = myTasks.length;
  const completionPercentage = totalTasks > 0 
    ? Math.round((completedTasks.length / totalTasks) * 100) 
    : 0;

  // Sort tasks by priority (highest first)
  const sortedTasks = [...assignedTasks, ...inProgressTasks].sort((a, b) => b.priority - a.priority);

  return (
    <div className="container mx-auto">
      <h1 className="text-2xl font-bold mb-6">My Onboarding</h1>
      
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6 mb-8">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Completion</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="text-2xl font-bold">{completionPercentage}%</div>
              <div className="text-sm text-muted-foreground">
                {completedTasks.length}/{totalTasks} tasks
              </div>
            </div>
            <Progress value={completionPercentage} className="h-2" />
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">In Progress</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{inProgressTasks.length}</div>
            <p className="text-xs text-muted-foreground mt-2">
              Tasks you're working on
            </p>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Next Tasks</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{assignedTasks.length}</div>
            <p className="text-xs text-muted-foreground mt-2">
              Waiting to be started
            </p>
          </CardContent>
        </Card>
      </div>
      
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>My Onboarding Schedule</CardTitle>
            <CardDescription>Your personalized onboarding journey</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="relative">
              {/* Very simple timeline visualization */}
              <div className="absolute left-0 top-0 bottom-0 w-0.5 bg-border mx-3"></div>
              
              <div className="space-y-6">
                {myTasks.map((task, index) => {
                  let statusColor = "bg-status-assigned";
                  let icon = <ClipboardList size={16} />;
                  
                  if (task.status === "in-progress") {
                    statusColor = "bg-status-in-progress";
                    icon = <Clock size={16} />;
                  } else if (task.status === "completed") {
                    statusColor = "bg-status-completed";
                    icon = <CheckSquare size={16} />;
                  }
                  
                  return (
                    <div key={task.id} className="flex">
                      <div className={`w-6 h-6 rounded-full ${statusColor} flex items-center justify-center z-10 mt-0.5`}>
                        {icon}
                      </div>
                      <div className="ml-4 flex-1">
                        <div className="flex justify-between items-start">
                          <div>
                            <p className="font-medium">{task.name}</p>
                            <p className="text-sm text-muted-foreground">
                              {task.startDate && (
                                <>
                                  Due: {new Date(task.dueDate!).toLocaleDateString()}
                                  {task.completedDate && <span> · Completed: {new Date(task.completedDate).toLocaleDateString()}</span>}
                                </>
                              )}
                            </p>
                          </div>
                          <div>
                            <div className={`status-badge ${task.status}`}>
                              {task.status.charAt(0).toUpperCase() + task.status.slice(1).replace('-', ' ')}
                            </div>
                          </div>
                        </div>
                        
                        <div className="mt-2">
                          <Link to={`/tasks/${task.id}`}>
                            <Button size="sm" variant={task.status === "completed" ? "outline" : "default"}>
                              {task.status === "completed" ? "View Details" : "Go to Task"}
                            </Button>
                          </Link>
                        </div>
                      </div>
                    </div>
                  );
                })}
                
                {myTasks.length === 0 && (
                  <p className="text-center py-12 text-muted-foreground">
                    No tasks assigned yet! Your manager will assign tasks soon.
                  </p>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

export default NewHireDashboard;
