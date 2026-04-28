
import { useState, useCallback } from "react";
import { toast } from "sonner";
import { PersonalizedTask, TaskStatus, TaskAssignment } from "@/types/tasks";
import { mockPersonalizedTasks } from "@/mocks/tasks";
import { generatePersonalizedTask } from "../../utils/aiUtils";
import { useAIConfig } from "../AIConfigContext";

export const usePersonalizedTaskContext = () => {
  const [personalizedTasks, setPersonalizedTasks] = useState<PersonalizedTask[]>(mockPersonalizedTasks);

  // Get a single personalized task by ID
  const getPersonalizedTask = (id: string): PersonalizedTask | undefined => {
    return personalizedTasks.find(task => task.id === id);
  };

  // Personalized Task CRUD operations
  const addPersonalizedTask = (taskData: Omit<PersonalizedTask, "id" | "createdAt" | "status">, userId: string) => {
    if (!userId) return;
    
    const newTask: PersonalizedTask = {
      ...taskData,
      id: Math.random().toString(36).substring(2, 9),
      status: "assigned",
      createdAt: new Date(),
      attachments: [],
    };
    
    setPersonalizedTasks((prev) => [...prev, newTask]);
    toast.success("Task created successfully");
  };

  const updateTaskStatus = (id: string, status: TaskStatus, completedDate?: Date) => {
    setPersonalizedTasks((prev) => 
      prev.map((task) => {
        if (task.id === id) {
          const updatedTask = { ...task, status };
          
          if (status === "completed" && !task.completedDate) {
            updatedTask.completedDate = completedDate || new Date();
            updatedTask.isOnTime = updatedTask.dueDate ? updatedTask.completedDate <= updatedTask.dueDate : true;
          }
          
          return updatedTask;
        }
        return task;
      })
    );
    toast.success(`Task marked as ${status}`);
  };

  const updatePersonalizedTask = (id: string, data: Partial<PersonalizedTask>) => {
    setPersonalizedTasks((prevTasks) => {
      const updatedTasks = prevTasks.map((task) => 
        task.id === id ? { ...task, ...data } : task
      );
      return updatedTasks;
    });
    
    // Only show toast if there are actual changes
    if (Object.keys(data).length > 0) {
      console.log('Updated task with data:', { id, data });
      toast.success("Task updated successfully");
    }
    
    return Promise.resolve();
  };

  const deletePersonalizedTask = (id: string) => {
    setPersonalizedTasks((prev) => prev.filter((task) => task.id !== id));
    toast.success("Task deleted successfully");
  };

  // Get unreviewed completed tasks
  const getUnreviewedTasks = (managerId: string, users: any[]): PersonalizedTask[] => {
    // Find users who report to this manager
    const subordinateUserIds = users
      .filter(u => u.managerId === managerId)
      .map(u => u.id);
    
    // Find completed tasks without reviews from subordinates
    return personalizedTasks.filter(task => 
      task.status === "completed" && 
      !task.reviewId && 
      subordinateUserIds.includes(task.assignedTo)
    );
  };

  // Get tasks that a mentor can review
  const getReviewableTasks = (mentorId: string, users: any[], taskReviews: any[] = []): PersonalizedTask[] => {
    // Find the mentor's manager
    const mentor = users.find(u => u.id === mentorId);
    if (!mentor || !mentor.managerId) return [];
    
    const managerId = mentor.managerId;
    
    // Find new hires who report to the same manager
    const newHireIds = users
      .filter(u => u.managerId === managerId && u.role === "new-hire")
      .map(u => u.id);
    
    // Find completed tasks from these new hires that don't have a review assigned yet
    return personalizedTasks.filter(task => {
      // Check if task is completed and assigned to a new hire under the same manager
      if (task.status !== "completed" || !newHireIds.includes(task.assignedTo)) {
        return false;
      }
      
      // Check if task already has a reviewId
      if (task.reviewId) {
        return false;
      }
      
      // Double-check if there's any review in taskReviews for this task
      const hasExistingReview = taskReviews.some(review => review.taskId === task.id);
      if (hasExistingReview) {
        return false;
      }
      
      return true;
    });
  };

  const { aiConfig } = useAIConfig();
  
  const createPersonalizedTask = useCallback(async (template: any, assignment: TaskAssignment, currentUserId: string) => {
    // Calculate dates
    const startDate = new Date();
    const dueDate = new Date();
    dueDate.setDate(dueDate.getDate() + template.estimatedEffort);

    // Prepare task
    const taskId = Math.random().toString(36).substring(2, 9);
    
    // Generate personalized task description using LLM
    const expandedDescription = await generatePersonalizedTask(
      {
        name: template.name,
        description: template.description,
        deliverables: template.deliverables
      },
      assignment.difficulty,
      assignment.priority,
      aiConfig,
      template.estimatedEffort
    ).catch((error) => {
      console.error("Error generating personalized task:", error);
      toast.error("Failed to generate personalized task description. Please try again later.");
      throw new Error("Failed to generate personalized task description");
    });
    
    const newTask: PersonalizedTask = {
      id: taskId,
      templateId: template.id,
      name: template.name,
      description: template.description,
      expandedDescription,
      deliverables: template.deliverables,
      assignedTo: assignment.assignedToId,
      difficulty: assignment.difficulty,
      priority: assignment.priority,
      estimatedEffort: template.estimatedEffort,
      status: "assigned",
      dependencies: assignment.dependencies,
      startDate,
      dueDate,
      attachments: [],
      createdBy: currentUserId,
      createdAt: new Date(),
    };
    
    setPersonalizedTasks((prev) => [...prev, newTask]);
    toast.success("Task created successfully");
  }, [aiConfig]);

  const generatePersonalizedTasks = useCallback(async (
    assignments: TaskAssignment[], 
    taskTemplates: any[],
    currentUserId: string,
    hasCompletedDependencies: (taskId: string) => boolean
  ): Promise<boolean> => {
    if (!currentUserId) {
      toast.error("User not authenticated");
      return false;
    }
    
    try {
      const newTasks: PersonalizedTask[] = [];
      
      for (const assignment of assignments) {
        const template = taskTemplates.find(t => t.id === assignment.templateId);
        if (!template) {
          console.error(`Template not found for assignment: ${assignment.templateId}`);
          continue;
        }
        
        // Calculate dates
        const startDate = new Date();
        const dueDate = new Date();
        dueDate.setDate(dueDate.getDate() + template.estimatedEffort);

        // Prepare task
        const taskId = Math.random().toString(36).substring(2, 9);
        
        // Generate personalized task description using LLM
        const expandedDescription = await generatePersonalizedTask(
          {
            name: template.name,
            description: template.description,
            deliverables: template.deliverables
          },
          assignment.difficulty,
          assignment.priority,
          aiConfig,
          template.estimatedEffort
        ).catch((error) => {
          console.error(`Error generating personalized task for ${template.name}:`, error);
          throw new Error(`Failed to generate task: ${template.name}. ${error.message || 'Please try again later.'}`);
        });
        
        const newTask: PersonalizedTask = {
          id: taskId,
          templateId: template.id,
          name: template.name,
          description: template.description,
          expandedDescription,
          deliverables: template.deliverables,
          assignedTo: assignment.assignedToId,
          difficulty: assignment.difficulty,
          priority: assignment.priority,
          estimatedEffort: template.estimatedEffort,
          status: "assigned",
          dependencies: assignment.dependencies,
          startDate,
          dueDate,
          attachments: [],
          createdBy: currentUserId,
          createdAt: new Date(),
        };
        
        newTasks.push(newTask);
      }
      
      if (newTasks.length === 0) {
        throw new Error("No tasks were created. Please check if the selected templates exist.");
      }
      
      setPersonalizedTasks((prev) => [...prev, ...newTasks]);
      return true;
    } catch (error) {
      console.error("Error in generatePersonalizedTasks:", error);
      throw error; // Re-throw to be handled by the caller
    }
  }, [aiConfig]);

  return {
    personalizedTasks,
    setPersonalizedTasks,
    getPersonalizedTask,
    addPersonalizedTask,
    updateTaskStatus,
    updatePersonalizedTask,
    deletePersonalizedTask,
    generatePersonalizedTasks,
    getUnreviewedTasks,
    getReviewableTasks,
    createPersonalizedTask,
  };
};
