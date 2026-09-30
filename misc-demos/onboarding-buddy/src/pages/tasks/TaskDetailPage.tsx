import React, { useState, useEffect } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { useUser } from "@/contexts/user/UserContext";
import { useTask } from "@/contexts/task";
import { PersonalizedTask, TaskStatus, ReviewStatus, TaskReview } from "@/types/tasks";

interface TaskDetailPageProps {
  fromReview?: boolean;
}
import { MarkdownRenderer } from "@/components/ui/MarkdownRenderer";
import { Button } from "@/components/ui/button";
import { ChevronLeft, Clock, Calendar, CheckSquare, AlertTriangle, Star as StarIcon } from "lucide-react";
import { StarRating } from "@/components/ui/star-rating";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import AIAssistant from "@/components/ai/AIAssistant";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { toast } from "sonner";

const TaskDetailPage: React.FC<TaskDetailPageProps> = ({ fromReview = false }) => {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { user, isImpersonating } = useAuth();
  const { users } = useUser();
  const { 
    personalizedTasks, 
    taskReviews,
    updateTaskStatus,
    updatePersonalizedTask,
    updateTaskReview,
    addTaskReview,
  } = useTask();
  
  const [activeTab, setActiveTab] = useState("task");
  const [completionNotes, setCompletionNotes] = useState("");
  const [reviewNotes, setReviewNotes] = useState("");
  const [score, setScore] = useState<number | undefined>(undefined);
  const [isSubmitting, setIsSubmitting] = useState(false);
  
  // Update URL when tab changes
  const handleTabChange = (value: string) => {
    setActiveTab(value);
    const newSearchParams = new URLSearchParams();
    if (value === 'review') {
      newSearchParams.set('tab', 'review');
    }
    // Always update the URL to reflect the current state
    setSearchParams(newSearchParams, { replace: true });
  };
  
  // Get the task from the personalizedTasks object
  const task = id ? personalizedTasks[id] : null;
  
  // Get the review if it exists
  const review = task?.reviewId 
    ? taskReviews.find(r => r.id === task.reviewId) 
    : null;

  // Derived state
  const assignedUser = task?.assignedTo ? users.find(user => user.id === task.assignedTo) : null;
  const isAssignedToCurrentUser = assignedUser?.id === user?.id;
  const isReviewer = review && review.assignedTo === user?.id;
  const isManagerOrMentor = user?.role === 'manager' || user?.role === 'mentor';
  const isReadOnly = isImpersonating || (assignedUser?.id === user?.id && task?.status !== 'completed');
  
  // Handle initial tab from URL search params and load data
  useEffect(() => {
    if (!task) return;
    
    // Load task data
    if (task.completionNotes) {
      setCompletionNotes(task.completionNotes);
    }
    
    if (review) {
      setReviewNotes(review.notes || "");
      setScore(review.score || undefined);
    }
    
    // Only set the tab from URL params if we haven't changed it yet
    // This prevents the tab from resetting when other state changes
    const tabParam = searchParams.get('tab');
    if (tabParam === 'review' && 
        review && 
        (user?.role === 'manager' || user?.role === 'mentor') && 
        task.status === 'completed' &&
        activeTab !== 'review') {
      setActiveTab('review');
    } else if (fromReview && 
               review && 
               (user?.role === 'manager' || user?.role === 'mentor') && 
               task.status === 'completed' &&
               activeTab !== 'review') {
      // Handle the fromReview prop case
      setActiveTab('review');
    }
  }, [task, review, fromReview, user?.role, searchParams, activeTab]);
  
  if (!task) {
    return (
      <div className="container mx-auto p-6">
        <h1 className="text-2xl font-bold mb-6">Task Not Found</h1>
        <p>The requested task could not be found.</p>
        <Button 
          variant="outline" 
          className="mt-4"
          onClick={() => navigate("/tasks")}
        >
          <ChevronLeft size={16} className="mr-1" />
          Back to Tasks
        </Button>
      </div>
    );
  }
  
  // Get status display info with consistent styling from TaskList.tsx
  const getStatusInfo = () => {
    switch (task.status) {
      case "assigned":
        return { 
          text: "Assigned", 
          icon: <Clock size={20} className="mr-2" />, 
          className: "bg-status-assigned text-purple-800" 
        };
      case "in-progress":
        return { 
          text: "In Progress", 
          icon: <Clock size={20} className="mr-2" />, 
          className: "bg-status-in-progress text-amber-800"
        };
      case "completed":
        return { 
          text: "Completed", 
          icon: <CheckSquare size={20} className="mr-2" />, 
          className: "bg-status-completed text-green-800"
        };
      case "cancelled":
        return { 
          text: "Cancelled", 
          icon: <AlertTriangle size={20} className="mr-2" />, 
          className: "bg-status-cancelled text-red-800"
        };
      default:
        return { 
          text: task.status, 
          icon: null, 
          className: "" 
        };
    }
  };
  
  const statusInfo = getStatusInfo();
  
  // Handle task status update
  const handleStatusChange = (newStatus: TaskStatus) => {
    updateTaskStatus(task.id, newStatus);
  };
  
  // Handle completion notes update
  const handleNotesUpdate = () => {
    updatePersonalizedTask(task.id, { completionNotes });
    toast.success("Completion notes updated");
  };
  
  // Handle volunteer to review
  const handleVolunteerToReview = () => {
    if (!user || !task) return;
    
    setIsSubmitting(true);
    
    try {
      const reviewId = Math.random().toString(36).substring(2, 9);
      
      // First update the task with the new review ID
      updatePersonalizedTask(task.id, { reviewId });
      
      // Create the review data
      const reviewData = {
        taskId: task.id,
        assignedTo: user.id,
        notes: "",
        score: undefined,
        createdBy: user.id,
      };
      
      // Add the review
      addTaskReview(
        reviewData,
        user.id,
        async (taskId: string, reviewId: string) => {
          // This callback will be called by addTaskReview after the review is created
          await updatePersonalizedTask(taskId, { reviewId });
        }
      );
      
      // Update the active tab and show success message
      setActiveTab("review");
      toast.success("Successfully volunteered to review this task");
    } catch (error) {
      console.error('Error volunteering to review:', error);
      toast.error("Failed to volunteer for review. Please try again.");
    } finally {
      setIsSubmitting(false);
    }
  };
  
  // Handle review submission
  const handleReviewSubmit = async (action: 'start' | 'save' | 'submit') => {
    if (!user || !task || !review) return;
    
    setIsSubmitting(true);
    
    try {
      const updates: any = {};
      
      // For start action, just update the status to in-progress
      if (action === 'start') {
        updates.status = 'in-progress';
      } 
      // For save action, just save the current notes
      else if (action === 'save') {
        updates.notes = reviewNotes;
        updates.score = score;
      }
      // For submit action, prepare the complete review submission
      else if (action === 'submit') {
        updates.notes = reviewNotes;
        updates.score = score;
        updates.status = 'completed';
        updates.completedAt = new Date().toISOString();
      }
      
      // Only update if there are changes to make
      if (Object.keys(updates).length > 0) {
        updateTaskReview(review.id, updates);
      }
      
      if (action === 'submit') {
        toast.success('Review submitted successfully!');
      } else if (action === 'save') {
        toast.success('Review notes saved');
      } else {
        toast.success('Review started');
      }
       
      // Switch to the task tab after submission
      if (action === 'submit') {
        setActiveTab('task');
      }
      
    } catch (error) {
      console.error('Error processing review:', error);
      const actionText = action === 'submit' ? 'submit' : 
                        action === 'save' ? 'save' : 'start';
      toast.error(`Failed to ${actionText} review. Please try again.`);
    } finally {
      setIsSubmitting(false);
    }
  };
  
  // Render task details tab
  const renderTaskDetails = () => (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <div className="flex justify-between items-start">
            <div>
              <CardTitle className="text-2xl font-bold">{task.name}</CardTitle>
              <CardDescription className="mt-1">
                Assigned to: {users.find(user => user.id === task.assignedTo)?.name || 'Unassigned'}
              </CardDescription>
            </div>
            <Badge className={`text-sm ${statusInfo.className}`}>
              {statusInfo.icon}
              {statusInfo.text}
            </Badge>
          </div>
        </CardHeader>
        
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-4">
              <div>
                <div className="text-sm font-medium text-muted-foreground">Due Date</div>
                <div className="flex items-center">
                  <Calendar size={16} className="mr-2" />
                  {task.dueDate ? new Date(task.dueDate).toLocaleDateString() : 'No due date'}
                </div>
              </div>
              <div>
                <div className="text-sm font-medium text-muted-foreground">Priority</div>
                <StarRating value={task.priority || 3} max={5} />
              </div>
            </div>
            <div className="space-y-4">
              <div>
                <div className="text-sm font-medium text-muted-foreground">Estimated Effort</div>
                <div>{task.estimatedEffort || 'Not specified'} days</div>
              </div>
              {(isManagerOrMentor || user?.role === 'admin') && (
                <div>
                  <div className="text-sm font-medium text-muted-foreground">Difficulty</div>
                  <StarRating value={task.difficulty || 3} max={5} />
                </div>
              )}
            </div>
          </div>
          
          <div className="mt-6">
            <h3 className="text-lg font-semibold mb-2">Description</h3>
            <div className="prose max-w-none">
              <MarkdownRenderer content={task.expandedDescription || task.description} />
            </div>
          </div>
          
          {task.deliverables && task.deliverables.length > 0 && (
            <div className="mt-6">
              <h3 className="text-lg font-semibold mb-2">Deliverables</h3>
              <ul className="list-disc pl-5 space-y-1">
                {task.deliverables.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
          )}
          
          {isAssignedToCurrentUser && task.status !== 'completed' && task.status !== 'cancelled' && (
            <div className="mt-6">
              <h3 className="text-lg font-semibold mb-2">Update Progress</h3>
              <div className="space-y-4">
                <div>
                  <Label htmlFor="completion-notes">
                    Completion Notes {task.status === 'in-progress' && <span className="text-red-500">*</span>}
                    {task.status === 'assigned' && (
                      <span className="text-muted-foreground text-sm ml-2">(Available when task is in progress)</span>
                    )}
                  </Label>
                  <Textarea
                    id="completion-notes"
                    value={completionNotes}
                    onChange={(e) => task.status === 'in-progress' && setCompletionNotes(e.target.value)}
                    placeholder={
                      task.status === 'in-progress' 
                        ? "Add any notes about your progress..." 
                        : "Start the task to add completion notes"
                    }
                    className="mt-1 min-h-[100px]"
                    required={task.status === 'in-progress'}
                    disabled={task.status !== 'in-progress'}
                  />
                </div>
                <div className="flex space-x-2">
                  {task.status === 'assigned' && (
                    <Button 
                      onClick={() => handleStatusChange('in-progress')}
                      variant="default"
                    >
                      Start Task
                    </Button>
                  )}
                  {task.status === 'in-progress' && (
                    <>
                      <Button 
                        onClick={() => {
                          if (!completionNotes.trim()) {
                            toast.error("Please add completion notes before marking as complete");
                            return;
                          }
                          handleNotesUpdate();
                          handleStatusChange('completed');
                        }}
                        variant="default"
                        disabled={!completionNotes.trim()}
                      >
                        Mark as Complete
                      </Button>
                      <Button 
                        variant="outline"
                        onClick={handleNotesUpdate}
                        disabled={!completionNotes.trim()}
                        className="mr-2"
                      >
                        Save Notes
                      </Button>
                    </>
                  )}
                </div>
              </div>
            </div>
          )}
          
    {isManagerOrMentor && task.status === 'completed' && !review && !isImpersonating && user && (
            <div className="mt-6 pt-4 border-t">
              <Button 
                onClick={handleVolunteerToReview}
                variant="default"
              >
                Volunteer to Review
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
      
      {isAssignedToCurrentUser && !isImpersonating && task && (
        <div className="mt-6">
          <AIAssistant task={task} />
        </div>
      )}
    </div>
  );
  
  // Get review status display info with consistent styling
  const getReviewStatusInfo = (status: string) => {
    switch (status) {
      case "assigned":
        return { 
          text: "Assigned", 
          icon: <Clock size={20} className="mr-2" />, 
          className: "bg-status-assigned text-purple-800" 
        };
      case "in-progress":
        return { 
          text: "In Progress", 
          icon: <Clock size={20} className="mr-2" />, 
          className: "bg-status-in-progress text-amber-800"
        };
      case "completed":
        return { 
          text: "Completed", 
          icon: <CheckSquare size={20} className="mr-2" />, 
          className: "bg-status-completed text-green-800"
        };
      default:
        return { 
          text: status, 
          icon: null, 
          className: "" 
        };
    }
  };

  // Render review tab
  const renderReviewTab = () => {
    if (!review) return null;
    
    const reviewerUser = users.find(user => user.id === review.assignedTo);
    const reviewStatusInfo = getReviewStatusInfo(review.status);
    
    return (
      <Card>
        <CardHeader>
          <div className="flex justify-between items-start">
            <div>
              <CardTitle className="text-2xl font-bold">{task.name}</CardTitle>
              <CardDescription className="mt-1">
                Assigned to: {users.find(user => user.id === task.assignedTo)?.name || 'Unassigned'}
              </CardDescription>
            </div>
            <Badge className={`text-sm ${reviewStatusInfo.className}`}>
              {reviewStatusInfo.icon}
              {reviewStatusInfo.text}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-2">
            <div className="grid grid-cols-1 gap-4">
              <div>
                <Label>Reviewer</Label>
                <div>{reviewerUser?.name || 'Unknown'}</div>
              </div>
            </div>
            
            <div className="pt-4">
              <Label>Score</Label>
              <div className="flex items-center space-x-4">
                <Slider
                  value={[score]}
                  min={1}
                  max={10}
                  step={1}
                  onValueChange={([value]) => setScore(value)}
                  disabled={isImpersonating || !isReviewer || review?.status !== 'in-progress'}
                  className="w-60"
                />
                <span className="text-lg font-medium">{score ? score : '?'}/10</span>
              </div>
            </div>
            
            <div className="pt-4">
              <Label htmlFor="review-notes">Review Notes</Label>
              <Textarea
                id="review-notes"
                value={reviewNotes}
                onChange={(e) => setReviewNotes(e.target.value)}
                placeholder="Add your review notes here..."
                className="mt-1 min-h-[200px]"
                disabled={isImpersonating || !isReviewer || review?.status !== 'in-progress'}
              />
            </div>
            
            {isReviewer && !isImpersonating && (
              <div className="flex flex-wrap items-center gap-4 pt-4">
                {review?.status === 'assigned' && (
                  <div className="flex items-center gap-4">
                    <Button 
                      onClick={() => handleReviewSubmit('start')}
                      disabled={isSubmitting}
                    >
                      {isSubmitting ? 'Starting...' : 'Start Review'}
                    </Button>
                    <div className="text-sm text-muted-foreground whitespace-nowrap">
                      Click 'Start Review' to begin reviewing this task
                    </div>
                  </div>
                )}
                
                {review?.status === 'in-progress' && (
                  <div className="flex flex-wrap items-center gap-4">
                    <div className="flex items-center gap-2">
                      <Button 
                        onClick={() => handleReviewSubmit('submit')}
                        disabled={isSubmitting || !reviewNotes.trim() || score === undefined}
                      >
                        {isSubmitting ? 'Submitting...' : 'Submit Review'}
                      </Button>
                      <Button 
                        variant="outline"
                        onClick={() => handleReviewSubmit('save')}
                        disabled={isSubmitting || !reviewNotes.trim()}
                      >
                        {isSubmitting ? 'Saving...' : 'Save Review'}
                      </Button>
                    </div>
                    {(!reviewNotes.trim() || score === undefined) && (
                      <div className="text-sm text-destructive whitespace-nowrap">
                        Please add review notes and score before submitting
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    );
  };
  
  return (
    <div className="container mx-auto p-6">
      <div className="mb-6">
        <Button 
          variant="outline" 
          onClick={() => navigate(-1)}
          className="mb-4"
        >
          <ChevronLeft size={16} className="mr-1" />
          Back
        </Button>
        
        {isManagerOrMentor || user?.role === 'admin' ? (
          <Tabs value={activeTab} onValueChange={handleTabChange} className="w-full">
            <TabsList className="mb-6">
              <TabsTrigger value="task">Task Details</TabsTrigger>
              <div className="relative group">
                <TabsTrigger 
                  value="review" 
                  disabled={task.status !== 'completed' || !review}
                  data-state={activeTab === 'review' ? 'active' : 'inactive'}
                  className="disabled:opacity-50 disabled:pointer-events-auto disabled:cursor-not-allowed"
                >
                  {task.status === 'completed' && review ? 'Review' : 'No Review'}
                </TabsTrigger>
                {(task.status !== 'completed' || !review) && (
                  <div className="absolute -top-8 left-1/2 -translate-x-1/2 bg-foreground text-background text-xs px-2 py-1 rounded opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
                    {task.status !== 'completed' ? 'Task must be completed first' : 'No review assigned yet'}
                  </div>
                )}
              </div>
            </TabsList>
            
            <TabsContent value="task">
              {renderTaskDetails()}
            </TabsContent>
            
            <TabsContent value="review">
              {task.status === 'completed' && review ? (
                renderReviewTab()
              ) : (
                <div className="text-center py-8 text-muted-foreground">
                  {!review 
                    ? 'No review has been assigned for this task yet.' 
                    : 'This task needs to be completed before it can be reviewed.'}
                </div>
              )}
            </TabsContent>
          </Tabs>
        ) : (
          // For new hires, only show task details
          renderTaskDetails()
        )}
      </div>
    </div>
  );
};

export default TaskDetailPage;
