
import React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useTask } from "@/contexts/task";
import { useUser } from "@/contexts/user/UserContext";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, CardFooter } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Link } from "react-router-dom";
import { CheckSquare, Clock, AlertCircle } from "lucide-react";
import { toast } from "sonner";

const MentorDashboard: React.FC = () => {
  const { user, isImpersonating } = useAuth();
  const { users } = useUser();
  const { 
    taskReviews, 
    personalizedTasks, 
    getReviewableTasks, 
    addTaskReview, 
    updateReviewStatus,
    updatePersonalizedTask 
  } = useTask();
  
  if (!user) return null;

  // Filter reviews assigned to this mentor
  const myReviews = taskReviews.filter(review => review.assignedTo === user.id);
  
  const assignedReviews = myReviews.filter(review => review.status === "assigned");
  const inProgressReviews = myReviews.filter(review => review.status === "in-progress");
  const completedReviews = myReviews.filter(review => review.status === "completed");
  
  // Get tasks available for review
  const reviewableTasks = getReviewableTasks(user.id, users);

  // Handle self-assign for mentors
  const handleSelfAssign = (taskId: string) => {
    if (!user) return;
    
    const task = personalizedTasks[taskId];
    if (!task) {
      toast.error("Task not found");
      return;
    }
    
    const reviewData = {
      taskId: taskId,
      assignedTo: user.id,
      status: 'assigned',
      notes: "",
      score: undefined,
      createdBy: user.id
    };
    
    addTaskReview(
      reviewData,
      user.id,
      async (taskId: string, reviewId: string) => {
        try {
          // Update the task with the new review ID
          await updatePersonalizedTask(taskId, { reviewId });
          console.log(`Task ${taskId} updated with review ${reviewId}`);
        } catch (error) {
          console.error('Failed to update task with review ID:', error);
          throw error; // This will be caught by addTaskReview
        }
      }
    );
    toast.success("Task assigned to you for review");
  };

  return (
    <div className="container mx-auto">
      <h1 className="text-2xl font-bold mb-6">Mentor Dashboard</h1>
      
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Assigned Reviews</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{assignedReviews.length}</div>
            <p className="text-xs text-muted-foreground mt-2">
              Waiting for your review
            </p>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">In Progress</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{inProgressReviews.length}</div>
            <p className="text-xs text-muted-foreground mt-2">
              Reviews you're working on
            </p>
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Completed</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{completedReviews.length}</div>
            <p className="text-xs text-muted-foreground mt-2">
              Reviews you've finished
            </p>
          </CardContent>
        </Card>
      </div>
      
      <Card className="mb-8">
        <CardHeader>
          <CardTitle>Available to Review</CardTitle>
          <CardDescription>Tasks waiting for a reviewer</CardDescription>
        </CardHeader>
        <CardContent>
          {reviewableTasks.length > 0 ? (
            <div className="space-y-4">
              {reviewableTasks.map(task => {
                const newHire = users.find(u => u.id === task.assignedTo);
                
                return (
                  <Card key={task.id} className="border shadow-none">
                    <CardHeader className="pb-2">
                      <div className="flex justify-between items-start">
                        <CardTitle className="text-base">{task.name}</CardTitle>
                        <Badge className="bg-status-completed text-green-800">Ready for Review</Badge>
                      </div>
                      <CardDescription>Submitted by {newHire?.name || 'Unknown'}</CardDescription>
                    </CardHeader>
                    <CardContent className="pb-2">
                      <p className="text-sm line-clamp-2">{task.description}</p>
                      <div className="mt-2">
                        <p className="text-xs text-muted-foreground">Deliverables:</p>
                        <ul className="text-xs list-disc ml-4">
                          {task.deliverables.map((item, i) => (
                            <li key={i}>{item}</li>
                          ))}
                        </ul>
                      </div>
                    </CardContent>
                    <CardFooter>
                      <div className="flex gap-2 w-full">
                        {isImpersonating ? (
                          <Button 
                            size="sm" 
                            className="w-full"
                            disabled
                          >
                            Volunteer to Review (Disabled)
                          </Button>
                        ) : (
                          <Button 
                            size="sm" 
                            className="w-full"
                            onClick={() => handleSelfAssign(task.id)}
                          >
                            Volunteer to Review
                          </Button>
                        )}
                        <Link to={`/tasks/${task.id}`} className="w-full">
                          <Button size="sm" variant="outline" className="w-full">
                            View Task
                          </Button>
                        </Link>
                      </div>
                    </CardFooter>
                  </Card>
                );
              })}
            </div>
          ) : (
            <div className="text-center py-8">
              <p className="text-muted-foreground">No tasks are currently available for review.</p>
              <p className="text-sm text-muted-foreground mt-2">
                Completed tasks from new hires will appear here for review.
              </p>
            </div>
          )}
        </CardContent>
      </Card>
      
      <div className="grid grid-cols-1 gap-6">
        <Card>
          <CardHeader>
            <CardTitle>Your Review Assignment</CardTitle>
            <CardDescription>Tasks waiting for your review</CardDescription>
          </CardHeader>
          <CardContent>
            {assignedReviews.length > 0 ? (
              <div className="space-y-4">
                {assignedReviews.map(review => {
                  const task = personalizedTasks[review.taskId];
                  const newHire = users.find(u => task ? u.id === task.assignedTo : false);
                  
                  if (!task) return null;
                  
                  return (
                    <Card key={review.id} className="border shadow-none">
                      <CardHeader className="pb-2">
                        <div className="flex justify-between items-start">
                          <CardTitle className="text-base">{task.name}</CardTitle>
                          <Badge className="bg-status-assigned text-purple-800">Needs Review</Badge>
                        </div>
                        <CardDescription>Submitted by {newHire?.name || 'Unknown'}</CardDescription>
                      </CardHeader>
                      <CardContent className="pb-2">
                        <p className="text-sm line-clamp-2">{task.description}</p>
                        <div className="mt-2">
                          <p className="text-xs text-muted-foreground">Deliverables:</p>
                          <ul className="text-xs list-disc ml-4">
                            {task.deliverables.map((item, i) => (
                              <li key={i}>{item}</li>
                            ))}
                          </ul>
                        </div>
                      </CardContent>
                      <CardFooter>
                        <div className="flex gap-2 w-full">
                          {isImpersonating ? (
                            <Button 
                              size="sm" 
                              className="w-full"
                              disabled
                            >
                              Start Review (Disabled)
                            </Button>
                          ) : (
                            <Button 
                              size="sm" 
                              className="w-full"
                              onClick={async (e) => {
                                e.preventDefault();
                                try {
                                  // Update the status to 'in-progress'
                                  await updateReviewStatus(review.id, 'in-progress');
                                  toast.success('Review marked as In Progress');
                                } catch (error) {
                                  console.error('Failed to start review:', error);
                                  toast.error('Failed to start review');
                                }
                              }}
                            >
                              Start Review
                            </Button>
                          )}
                        <Link to={`/tasks/${task.id}`} className="w-full">
                          <Button size="sm" variant="outline" className="w-full">
                            View Task
                          </Button>
                        </Link>
                        </div>
                      </CardFooter>
                    </Card>
                  );
                })}
              </div>
            ) : (
              <p className="text-center py-8 text-muted-foreground">No assigned tasks pending review!</p>
            )}
          </CardContent>
        </Card>
        
        <Card>
          <CardHeader>
            <CardTitle>In Progress Reviews</CardTitle>
            <CardDescription>Reviews you've started but not completed</CardDescription>
          </CardHeader>
          <CardContent>
            {inProgressReviews.length > 0 ? (
              <div className="space-y-4">
                {inProgressReviews.map(review => {
                  const task = personalizedTasks[review.taskId];
                  const newHire = users.find(u => task ? u.id === task.assignedTo : false);
                  
                  if (!task) return null;
                  
                  return (
                    <div key={review.id} className="flex items-center justify-between border-b pb-3">
                      <div>
                        <p className="font-medium">{task.name}</p>
                        <p className="text-sm text-muted-foreground">By {newHire?.name || 'Unknown'}</p>
                      </div>
                      <div className="flex gap-2">
                        {!isImpersonating && (
                          <Link to={`/reviews/${review.id}`}>
                            <Button size="sm" variant="default">
                              Continue
                            </Button>
                          </Link>
                        )}
                        <Link to={`/tasks/${review.taskId}`}>
                          <Button variant={isImpersonating ? 'outline' : 'ghost'} size="sm">
                            View Task
                          </Button>
                        </Link>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <p className="text-center py-8 text-muted-foreground">No task reviews in progress</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
};

export default MentorDashboard;
