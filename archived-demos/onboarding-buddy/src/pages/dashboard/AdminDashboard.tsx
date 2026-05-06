
import React from "react";
import { useUsers } from "@/contexts/user/UserContext";
import { useTask } from "@/contexts/task";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import TaskStatusCard from "@/components/dashboard/TaskStatusCard";
import { CheckSquare, Clock, AlertTriangle, UserCheck, ClipboardList, FileCheck } from "lucide-react";

const AdminDashboard: React.FC = () => {
  const { users } = useUsers();
  const { personalizedTasks, taskReviews } = useTask();

  // Count users by role
  const admins = users.filter(u => u.role === "admin").length;
  const managers = users.filter(u => u.role === "manager").length;
  const mentors = users.filter(u => u.role === "mentor").length;
  const newHires = users.filter(u => u.role === "new-hire").length;

  // Count tasks by status
  const taskArray = Object.values(personalizedTasks);
  const assigned = taskArray.filter(t => t.status === "assigned").length;
  const inProgress = taskArray.filter(t => t.status === "in-progress").length;
  const completed = taskArray.filter(t => t.status === "completed").length;
  const cancelled = taskArray.filter(t => t.status === "cancelled").length;
  const total = taskArray.length;

  // Count reviews by status
  const pendingReviews = taskReviews.filter(r => r.status === "assigned").length;
  const completedReviews = taskReviews.filter(r => r.status === "completed").length;

  // Calculate completion percentage
  const completionPercentage = total > 0 
    ? Math.round((completed / total) * 100) 
    : 0;

  return (
    <div className="container mx-auto">
      <h1 className="text-2xl font-bold mb-6">Admin Dashboard</h1>
      
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Total Users</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{users.length}</div>
            <p className="text-xs text-muted-foreground mt-2">
              {admins} Admins, {managers} Managers, {mentors} Mentors, {newHires} New Hires
            </p>
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
            <CardTitle className="text-sm font-medium text-muted-foreground">Completed Tasks</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{completed}</div>
            <p className="text-xs text-muted-foreground mt-2">
              {completionPercentage}% of all tasks
            </p>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Pending Reviews</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{pendingReviews}</div>
            <p className="text-xs text-muted-foreground mt-2">
              {completedReviews} Reviews completed
            </p>
          </CardContent>
        </Card>
      </div>
      
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-8">
        <Card>
          <CardHeader>
            <CardTitle>Task Status Overview</CardTitle>
            <CardDescription>Distribution of tasks by status</CardDescription>
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
            <CardTitle>User Role Distribution</CardTitle>
            <CardDescription>Breakdown of users by role</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 gap-4">
              <div className="flex items-center space-x-4">
                <div className="bg-blue-100 p-2 rounded-full">
                  <UserCheck size={24} className="text-blue-600" />
                </div>
                <div>
                  <p className="text-sm font-medium">Admins</p>
                  <p className="text-2xl font-bold">{admins}</p>
                </div>
              </div>
              <div className="flex items-center space-x-4">
                <div className="bg-green-100 p-2 rounded-full">
                  <UserCheck size={24} className="text-green-600" />
                </div>
                <div>
                  <p className="text-sm font-medium">Managers</p>
                  <p className="text-2xl font-bold">{managers}</p>
                </div>
              </div>
              <div className="flex items-center space-x-4">
                <div className="bg-amber-100 p-2 rounded-full">
                  <UserCheck size={24} className="text-amber-600" />
                </div>
                <div>
                  <p className="text-sm font-medium">Mentors</p>
                  <p className="text-2xl font-bold">{mentors}</p>
                </div>
              </div>
              <div className="flex items-center space-x-4">
                <div className="bg-purple-100 p-2 rounded-full">
                  <UserCheck size={24} className="text-purple-600" />
                </div>
                <div>
                  <p className="text-sm font-medium">New Hires</p>
                  <p className="text-2xl font-bold">{newHires}</p>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>
      
      <div className="grid grid-cols-1 gap-6">
        <Card>
          <CardHeader>
            <CardTitle>Recent Activity</CardTitle>
            <CardDescription>Latest tasks and reviews</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {Object.values(personalizedTasks)
                .sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime())
                .slice(0, 5)
                .map(task => (
                  <div key={task.id} className="flex items-center justify-between border-b pb-2">
                    <div className="flex items-center space-x-3">
                      {task.status === "completed" ? (
                        <FileCheck size={18} className="text-green-600" />
                      ) : (
                        <ClipboardList size={18} className="text-blue-600" />
                      )}
                      <div>
                        <p className="font-medium text-sm">{task.name}</p>
                        <p className="text-xs text-muted-foreground">
                          Assigned to: {users.find(u => u.id === task.assignedTo)?.name || "Unknown"}
                        </p>
                      </div>
                    </div>
                    <div className={`status-badge ${task.status}`}>
                      {task.status.charAt(0).toUpperCase() + task.status.slice(1)}
                    </div>
                  </div>
                ))}
              
              {Object.values(personalizedTasks).length === 0 && (
                <p className="text-center text-muted-foreground py-4">No tasks created yet</p>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

export default AdminDashboard;
