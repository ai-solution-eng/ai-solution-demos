
import React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useTask } from "@/contexts/task";
import { useUser } from "@/contexts/user/UserContext";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { Link } from "react-router-dom";
import TaskStatusCard from "@/components/dashboard/TaskStatusCard";
import { CheckSquare, Clock, AlertTriangle, ClipboardList } from "lucide-react";

const ManagerDashboard: React.FC = () => {
  const { user } = useAuth();
  const { personalizedTasks, taskReviews } = useTask();
  const { getSubordinates } = useUser();

  if (!user) return null;

  // Get only subordinates of the current manager
  const subordinates = getSubordinates(user.id);
  const subordinateIds = subordinates.map(sub => sub.id);
  
  // Count users by role among subordinates
  const mentors = subordinates.filter(u => u.role === "mentor").length;
  const newHires = subordinates.filter(u => u.role === "new-hire").length;
  const managerCount = subordinates.filter(u => u.role === "manager").length;

  // Filter tasks by subordinates
  const subordinateTasks = Object.values(personalizedTasks).filter(t => 
    t.assignedTo && subordinateIds.includes(t.assignedTo)
  );

  // Count tasks by status (only for subordinates)
  const assigned = subordinateTasks.filter(t => t.status === "assigned").length;
  const inProgress = subordinateTasks.filter(t => t.status === "in-progress").length;
  const completed = subordinateTasks.filter(t => t.status === "completed").length;
  const cancelled = subordinateTasks.filter(t => t.status === "cancelled").length;
  const total = subordinateTasks.length;

  // Filter reviews by subordinates (where task assignee is a subordinate)
  const subordinateReviews = taskReviews.filter(r => {
    const task = Object.values(personalizedTasks).find(t => t.id === r.taskId);
    return task && subordinateIds.includes(task.assignedTo);
  });
  
  // Count reviews by status
  const pendingReviews = subordinateReviews.filter(r => r.status === "assigned").length;
  const completedReviews = subordinateReviews.filter(r => r.status === "completed").length;

  // Calculate completion percentage
  const completionPercentage = total > 0 
    ? Math.round((completed / total) * 100) 
    : 0;

  return (
    <div className="container mx-auto">
      <h1 className="text-2xl font-bold mb-6">Manager Dashboard</h1>
      
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">New Hires</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{newHires}</div>
            <Link to="/users" className="text-xs text-blue-600 hover:underline mt-2 block">
              Manage New Hires
            </Link>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Mentors</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{mentors}</div>
            <Link to="/users" className="text-xs text-blue-600 hover:underline mt-2 block">
              Manage Mentors
            </Link>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Active Tasks</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{assigned + inProgress}</div>
            <p className="text-xs text-muted-foreground mt-2">
              {assigned} Assigned, {inProgress} In Progress
            </p>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Pending Reviews</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{pendingReviews}</div>
            <Link to="/tasks" className="text-xs text-blue-600 hover:underline mt-2 block">
              Manage Reviews
            </Link>
          </CardContent>
        </Card>
      </div>
      
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        <Card>
          <CardHeader>
            <CardTitle>Task Overview</CardTitle>
            <CardDescription>Status of subordinates' onboarding tasks</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <TaskStatusCard 
                title="Assigned" 
                count={assigned} 
                icon={<ClipboardList size={20} />} 
                status="assigned"
              />
              <TaskStatusCard 
                title="In Progress" 
                count={inProgress} 
                icon={<Clock size={20} />} 
                status="in-progress" 
              />
              <TaskStatusCard 
                title="Completed" 
                count={completed} 
                icon={<CheckSquare size={20} />} 
                status="completed" 
              />
              <TaskStatusCard 
                title="Cancelled" 
                count={cancelled} 
                icon={<AlertTriangle size={20} />} 
                status="cancelled" 
              />
            </div>
            
            <div>
              <div className="flex justify-between mb-1 text-xs">
                <span>Overall Completion</span>
                <span>{completionPercentage}%</span>
              </div>
              <Progress value={completionPercentage} className="h-2" />
            </div>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader>
            <CardTitle>Quick Actions</CardTitle>
            <CardDescription>Common tasks for managers</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              <Link to="/task-templates/new">
                <Button variant="outline" className="w-full justify-start">
                  <ClipboardList className="mr-2 h-4 w-4" />
                  Create Task Template
                </Button>
              </Link>
              <Link to="/users/new">
                <Button variant="outline" className="w-full justify-start">
                  <ClipboardList className="mr-2 h-4 w-4" />
                  Add New User
                </Button>
              </Link>
              <Link to="/tasks">
                <Button variant="outline" className="w-full justify-start">
                  <ClipboardList className="mr-2 h-4 w-4" />
                  Manage Tasks
                </Button>
              </Link>
            </div>
          </CardContent>
        </Card>
      </div>
      
      <div className="grid grid-cols-1 gap-6">
        <Card>
          <CardHeader>
            <div className="flex justify-between items-center">
              <div>
                <CardTitle>New Hires Progress</CardTitle>
                <CardDescription>Onboarding status by user</CardDescription>
              </div>
              <Link to="/users">
                <Button variant="outline" size="sm">View All</Button>
              </Link>
            </div>
          </CardHeader>
          <CardContent>
            {subordinates
              .filter(user => user.role === "new-hire")
              .map(newHire => {
                const userTasks = Object.values(personalizedTasks).filter(t => t.assignedTo === newHire.id);
                const userCompleted = userTasks.filter(t => t.status === "completed").length;
                const userTotal = userTasks.length;
                const userProgress = userTotal > 0 ? Math.round((userCompleted / userTotal) * 100) : 0;
                
                return (
                  <div key={newHire.id} className="mb-4 last:mb-0">
                    <div className="flex items-center space-x-2 mb-1">
                      {newHire.avatar && (
                        <img 
                          src={newHire.avatar} 
                          alt={newHire.name} 
                          className="w-6 h-6 rounded-full"
                        />
                      )}
                      <span className="font-medium text-sm">{newHire.name}</span>
                    </div>
                    <div className="flex items-center space-x-4">
                      <Progress value={userProgress} className="h-2 flex-grow" />
                      <span className="text-xs text-muted-foreground whitespace-nowrap">
                        {userCompleted}/{userTotal} tasks
                      </span>
                    </div>
                  </div>
                );
              })}
              
            {subordinates.filter(u => u.role === "new-hire").length === 0 && (
              <p className="text-center text-muted-foreground py-4">No new hires found</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

export default ManagerDashboard;
