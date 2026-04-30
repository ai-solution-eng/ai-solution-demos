import React, { createContext, useContext, useState, useCallback, useEffect } from "react";
import { useAuth } from "../AuthContext";
import { useAIConfig } from "../AIConfigContext";
import { useUser } from "../user/UserContext";
import { useTaskTemplateContext } from "./taskTemplateContext";
import { usePersonalizedTaskContext } from "./personalizedTaskContext";
import { useTaskReviewContext } from "./taskReviewContext";
import { useTaskDependencyContext } from "./taskDependencyContext";
import { TaskContextType, TaskAssignment, TaskTemplate, TaskReview } from "@/types/tasks";

// Create and export the context
export const TaskContext = createContext<TaskContextType | undefined>(undefined);

// Define the hook first
function useTask() {
  const context = useContext(TaskContext);
  if (context === undefined) {
    throw new Error("useTask must be used within a TaskProvider");
  }
  return context;
}

// Export the hook as a named export
export { useTask };

// Export the provider component
export const TaskProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user } = useAuth();
  const { aiConfig } = useAIConfig();
  
  // Import all the separate contexts
  const taskTemplateContext = useTaskTemplateContext();
  
  // Initialize personalized task context
  const personalizedTaskContext = usePersonalizedTaskContext();
  
  // Initialize task review context first
  const taskReviewContext = useTaskReviewContext();
  const taskDependencyContext = useTaskDependencyContext(personalizedTaskContext.getPersonalizedTask);
  
  // Create a stable reference to the update function
  const updateTaskReviewId = useCallback((taskId: string, reviewId: string) => {
    if (personalizedTaskContext.updatePersonalizedTask) {
      console.log('Updating task with review ID:', { taskId, reviewId });
      personalizedTaskContext.updatePersonalizedTask(taskId, { reviewId });
    } else {
      console.error('updatePersonalizedTask is not available');
    }
  }, [personalizedTaskContext.updatePersonalizedTask]);

  // Register our update handler with the task review context
  useEffect(() => {
    if (!taskReviewContext.registerUpdateHandler) return;
    
    // Register the handler and get the cleanup function
    const unregister = taskReviewContext.registerUpdateHandler(updateTaskReviewId);
    
    // Return cleanup function to unregister the handler
    return () => {
      if (unregister) unregister();
    };
  }, [taskReviewContext.registerUpdateHandler, updateTaskReviewId]);

  // Link task and dependency operations
  const deletePersonalizedTask = (id: string) => {
    const dependents = taskDependencyContext.getTaskDependents(id);
    
    if (dependents.length > 0) {
      // This would be handled by a modal in the UI
      console.warn("This task has dependents. Deleting it will remove all dependent tasks.");
      
      // In a real implementation, we would ask for confirmation before deleting
      // For now, we'll just delete the task and all its dependents
      
      // Delete all dependents recursively
      const deleteDependents = (taskId: string) => {
        const taskDependents = taskDependencyContext.getTaskDependents(taskId);
        
        for (const dependentId of taskDependents) {
          deleteDependents(dependentId);
          personalizedTaskContext.deletePersonalizedTask(dependentId);
        }
      };
      
      deleteDependents(id);
    }
    
    // Delete the task itself
    personalizedTaskContext.deletePersonalizedTask(id);
    
    // Remove all dependencies involving this task by getting all dependencies and removing them
    const allDependencies = taskDependencyContext.taskDependencies.filter(
      dep => dep.taskId === id || dep.dependsOnTaskId === id
    );
    
    allDependencies.forEach(dep => {
      taskDependencyContext.removeTaskDependency(dep.taskId, dep.dependsOnTaskId);
    });
  };

  // Link task and review operations
  const addTaskReview = (
    review: Omit<TaskReview, "id" | "createdAt" | "status">,
    userId: string,
    updateTaskCallback?: (taskId: string, reviewId: string) => void
  ) => {
    if (!user) return;
    
    // Always pass the callback to taskReviewContext, it will handle whether to use it or not
    return taskReviewContext.addTaskReview(review, userId, updateTaskCallback);
  };

  // Wrapper for deleteTaskTemplate
  const deleteTaskTemplate = (id: string) => {
    const taskDependents = taskDependencyContext.getTaskDependents(id);
    // @ts-ignore - We know the implementation expects two arguments
    return taskTemplateContext.deleteTaskTemplate(id, taskDependents);
  };

  // Wrapper for addTaskTemplate
  const addTaskTemplate = (template: Omit<TaskTemplate, "id" | "createdAt" | "createdByRole">, userId: string, userRole?: 'admin' | 'manager' | 'mentor' | 'new-hire') => {
    if (!user) return;
    return taskTemplateContext.addTaskTemplate(template, userId, userRole);
  };

  // Wrapper for deleteTaskReview that handles clearing reviewId from tasks
  const deleteTaskReview = (id: string) => {
    const clearReviewId = (reviewId: string) => {
      // Find the task with this review ID and clear it
      const task = Object.values(personalizedTaskContext.personalizedTasks).find(
        (task: any) => task.reviewId === reviewId
      ) as any;
      if (task) {
        personalizedTaskContext.updatePersonalizedTask(task.id, { reviewId: undefined });
      }
    };
    
    // @ts-ignore - We know the implementation expects a second argument
    taskReviewContext.deleteTaskReview(id, clearReviewId);
  };

  // Wrapper for adding a personalized task
  const addPersonalizedTask = (taskData: any) => {
    if (!user) return;
    // @ts-ignore - We know the implementation expects two arguments
    personalizedTaskContext.addPersonalizedTask({
      ...taskData,
      createdBy: user.id
    }, user.id);
  };

  // Get users from UserContext
  const { users } = useUser();
  
  // Get users as an array
  const getUsers = (): any[] => {
    return users || [];
  };

  // Wrapper for getUserAccessibleTemplates
  const getUserAccessibleTemplates = (userId: string, usersList?: any[]) => {
    return taskTemplateContext.getUserAccessibleTemplates(userId, usersList || getUsers());
  };

  // Wrapper for getUnreviewedTasks
  const getUnreviewedTasks = (managerId: string, usersList?: any[]) => {
    return personalizedTaskContext.getUnreviewedTasks(managerId, usersList || getUsers());
  };

  // Wrapper for getReviewableTasks
  const getReviewableTasks = (mentorId: string, usersList?: any[]) => {
    return personalizedTaskContext.getReviewableTasks(
      mentorId, 
      usersList || getUsers(),
      taskReviewContext.taskReviews
    );
  };

  // Wrapper for generatePersonalizedTasks that includes AI and dependency checks
  const generatePersonalizedTasks = async (
    assignments: TaskAssignment[], 
    aiPrompt: string = ""
  ) => {
    if (!user) return;
    
    // Get all task templates for reference
    const templates = taskTemplateContext.taskTemplates;
    
    // Call the implementation with all required parameters
    return personalizedTaskContext.generatePersonalizedTasks(
      assignments,
      templates,
      user.id,
      taskDependencyContext.hasCompletedDependencies
    );
  };

  // Combine all context values
  const contextValue: TaskContextType = {
    // Task templates
    taskTemplates: taskTemplateContext.taskTemplates,
    addTaskTemplate: taskTemplateContext.addTaskTemplate,
    updateTaskTemplate: taskTemplateContext.updateTaskTemplate,
    deleteTaskTemplate: (id: string) => {
      const taskDependents = taskDependencyContext.getTaskDependents(id);
      return taskTemplateContext.deleteTaskTemplate(id, taskDependents);
    },
    getUserAccessibleTemplates: (userId: string, users: any[]) => 
      taskTemplateContext.getUserAccessibleTemplates(userId, users),
    
    // Task dependencies
    taskDependencies: taskDependencyContext.taskDependencies,
    addTaskDependency: taskDependencyContext.addTaskDependency,
    removeTaskDependency: taskDependencyContext.removeTaskDependency,
    getTaskDependencies: taskDependencyContext.getTaskDependencies,
    getTaskDependents: taskDependencyContext.getTaskDependents,
    isTaskDependencyValid: taskDependencyContext.isTaskDependencyValid,
    hasCompletedDependencies: taskDependencyContext.hasCompletedDependencies,
    
    // Personalized tasks - convert between array and object formats
    personalizedTasks: Object.fromEntries(
      personalizedTaskContext.personalizedTasks.map(task => [task.id, task])
    ),
    setPersonalizedTasks: (updater) => {
      if (typeof updater === 'function') {
        const currentTasks = Object.fromEntries(
          personalizedTaskContext.personalizedTasks.map(task => [task.id, task])
        );
        const newState = updater(currentTasks);
        personalizedTaskContext.setPersonalizedTasks(Object.values(newState));
      } else {
        personalizedTaskContext.setPersonalizedTasks(Object.values(updater));
      }
    },
    addPersonalizedTask: (task, userId) => {
      personalizedTaskContext.addPersonalizedTask(task, userId);
    },
    updateTaskStatus: personalizedTaskContext.updateTaskStatus,
    updatePersonalizedTask: personalizedTaskContext.updatePersonalizedTask,
    deletePersonalizedTask: deletePersonalizedTask,
    generatePersonalizedTasks: async (assignments, templates, userId, hasCompletedDependencies) => {
      // Implementation that matches the expected signature
      await personalizedTaskContext.generatePersonalizedTasks(assignments, templates, userId, hasCompletedDependencies);
    },
    
    // Task reviews
    taskReviews: taskReviewContext.taskReviews,
    addTaskReview: (review, userId, updateTaskCallback) => {
      if (!user) return;
      taskReviewContext.addTaskReview(review, userId, updateTaskCallback);
    },
    updateReviewStatus: taskReviewContext.updateReviewStatus,
    updateTaskReview: taskReviewContext.updateTaskReview,
    deleteTaskReview: (id, clearReviewIdCallback) => {
      // If no callback is provided, use a no-op function
      const callback = clearReviewIdCallback || (() => {});
      taskReviewContext.deleteTaskReview(id, callback);
    },
    
    // Additional methods
    getUnreviewedTasks: (managerId, users) => 
      personalizedTaskContext.getUnreviewedTasks(managerId, users || getUsers()),
    getReviewableTasks: (mentorId, users) => 
      personalizedTaskContext.getReviewableTasks(mentorId, users || getUsers(), taskReviewContext.taskReviews),
    
    // User context
    getUsers: () => getUsers(),
    
    // AI Config - cast to any to handle the AIConfigType to TaskContextType.aiConfig conversion
    aiConfig: aiConfig as any
  };

  return (
    <TaskContext.Provider value={contextValue}>
      {children}
    </TaskContext.Provider>
  );
};

