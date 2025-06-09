import { useState } from "react";
import { toast } from "sonner";
import { TaskTemplate } from "@/types/tasks";
import { mockTaskTemplates } from "@/mocks/tasks";

export const useTaskTemplateContext = () => {
  const [taskTemplates, setTaskTemplates] = useState<TaskTemplate[]>(mockTaskTemplates);

  // Task Template CRUD operations
  const addTaskTemplate = (templateData: Omit<TaskTemplate, "id" | "createdAt" | "createdByRole">, userId: string, userRole?: 'admin' | 'manager' | 'mentor' | 'new-hire') => {
    if (!userId) return;
    
    const newTemplate: TaskTemplate = {
      ...templateData,
      id: Math.random().toString(36).substring(2, 9),
      createdByRole: userRole || 'manager', // Default to 'manager' if role not provided
      createdAt: new Date(),
    };
    
    setTaskTemplates((prev) => [...prev, newTemplate]);
    toast.success("Task template created successfully");
  };

  const updateTaskTemplate = (id: string, data: Partial<TaskTemplate>) => {
    setTaskTemplates((prev) => 
      prev.map((template) => 
        template.id === id ? { ...template, ...data } : template
      )
    );
    toast.success("Task template updated successfully");
  };

  const deleteTaskTemplate = (id: string, taskDependents: string[]) => {
    if (taskDependents.length > 0) {
      return false; // Cannot delete template with dependents
    }
    
    setTaskTemplates((prev) => prev.filter((template) => template.id !== id));
    toast.success("Task template deleted successfully");
    return true;
  };

  // Get templates accessible to a specific user
  const getUserAccessibleTemplates = (userId: string, users: any[]): TaskTemplate[] => {
    const user = users.find(u => u.id === userId);
    
    if (!user) return [];
    
    // Admin can access all templates
    if (user.role === "admin") {
      return taskTemplates;
    }
    
    // Managers can access their own templates and templates created by admins
    if (user.role === "manager") {
      return taskTemplates.filter(t => 
        t.createdBy === userId || // Their own templates
        t.createdByRole === 'admin' // Templates created by admins
      );
    }
    
    // Other roles have no access to templates directly
    return [];
  };

  return {
    taskTemplates,
    addTaskTemplate,
    updateTaskTemplate,
    deleteTaskTemplate,
    getUserAccessibleTemplates,
  };
};
