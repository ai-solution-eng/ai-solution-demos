
import { useState, useCallback, useRef, useEffect } from "react";
import { toast } from "sonner";
import { TaskReview, ReviewStatus } from "@/types/tasks";
import { mockTaskReviews } from "@/mocks/tasks";

type UpdateTaskReviewIdFn = (taskId: string, reviewId: string) => void;

export const useTaskReviewContext = () => {
  const [taskReviews, setTaskReviews] = useState<TaskReview[]>(mockTaskReviews);
  const updateTaskReviewIdRef = useRef<UpdateTaskReviewIdFn>((taskId: string, reviewId: string) => {
    console.warn('No task review update handler registered for task:', taskId, 'review:', reviewId);
  });

  // Allow other contexts to register their update handler
  const registerUpdateHandler = useCallback((handler: UpdateTaskReviewIdFn) => {
    updateTaskReviewIdRef.current = handler;
    return () => {
      updateTaskReviewIdRef.current = () => {
        console.warn('Task review update handler has been unregistered');
      };
    };
  }, []);
  
  // Create a stable reference to the update function
  const updateTaskReviewId = useCallback((taskId: string, reviewId: string) => {
    updateTaskReviewIdRef.current(taskId, reviewId);
  }, []);

  // Task Review CRUD operations
  const addTaskReview = async (
    reviewData: Omit<TaskReview, "id" | "createdAt" | "status">, 
    userId: string,
    updateTaskCallback?: (taskId: string, reviewId: string) => Promise<void> | void
  ) => {
    if (!userId) return null;
    
    const newReview: TaskReview = {
      ...reviewData,
      id: Math.random().toString(36).substring(2, 9),
      status: "assigned",
      createdAt: new Date(),
    };
    
    try {
      // First update the task with the new review ID
      if (updateTaskCallback) {
        console.log('Using callback to update task with review ID:', newReview.id);
        await updateTaskCallback(reviewData.taskId, newReview.id);
      } else {
        console.log('Updating task with review ID:', {
          taskId: reviewData.taskId,
          reviewId: newReview.id
        });
        await updateTaskReviewId(reviewData.taskId, newReview.id);
      }
      
      // Then update the reviews state
      setTaskReviews(prev => [...prev, newReview]);
      
      toast.success("Task review assigned successfully");
      return newReview;
    } catch (error) {
      console.error('Error adding task review:', error);
      toast.error("Failed to add task review");
      return null;
    }
  };

  const updateReviewStatus = (id: string, status: ReviewStatus, notes?: string, score?: number) => {
    setTaskReviews((prev) => 
      prev.map((review) => {
        if (review.id === id) {
          const updatedReview = { 
            ...review, 
            status,
            ...(notes !== undefined ? { notes } : {}),
            ...(score !== undefined ? { score } : {})
          };
          
          if (status === "completed" && !review.completedDate) {
            updatedReview.completedDate = new Date();
          }
          
          return updatedReview;
        }
        return review;
      })
    );
    toast.success(`Review marked as ${status}`);
  };

  const updateTaskReview = (id: string, data: Partial<TaskReview>) => {
    setTaskReviews((prev) => 
      prev.map((review) => 
        review.id === id ? { ...review, ...data } : review
      )
    );
    toast.success("Review updated successfully");
  };

  const deleteTaskReview = (id: string, clearReviewIdCallback: (reviewId: string) => void) => {
    // First call the callback to remove the link from tasks
    clearReviewIdCallback(id);
    
    // Then delete the review
    setTaskReviews((prev) => prev.filter((review) => review.id !== id));
    toast.success("Review deleted successfully");
  };

  return {
    taskReviews,
    addTaskReview,
    updateReviewStatus,
    updateTaskReview,
    deleteTaskReview,
    // Expose the register function so TaskProvider can register its handler
    registerUpdateHandler
  };
};
