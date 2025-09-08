
import { User } from "../AuthContext";

// Task status types
export type TaskStatus = "assigned" | "in-progress" | "completed" | "cancelled";
export type ReviewStatus = "assigned" | "in-progress" | "completed" | "cancelled";

// Task template interface
export interface TaskTemplate {
  id: string;
  name: string;
  description: string;
  deliverables: string[];
  estimatedEffort: number; // in days
  createdBy: string; // user ID
  createdAt: Date;
}

// Task dependency interface
export interface TaskDependency {
  taskId: string;
  dependsOnTaskId: string;
}

// Task assignment interface with difficulty and priority
export interface TaskAssignment {
  templateId: string;
  assignedToId: string;
  difficulty: number;  // 1-5
  priority: number;    // 1-5
  dependencies: string[]; // Array of task template IDs
}

// Personalized task interface
export interface PersonalizedTask {
  id: string;
  templateId: string;
  name: string;
  description: string;
  expandedDescription: string;
  deliverables: string[];
  assignedTo: string; // user ID of new hire
  difficulty: number; // 1-5
  priority: number; // 1-5
  estimatedEffort: number; // in days
  actualEffort?: number; // in days
  status: TaskStatus;
  dependencies: string[]; // array of task IDs
  startDate?: Date;
  dueDate?: Date;
  completedDate?: Date;
  isOnTime?: boolean;
  attachments: string[]; // array of file URLs
  createdBy: string; // user ID
  createdAt: Date;
  reviewId?: string;
  completionNotes?: string; // Optional notes for task completion
}

// Task review interface
export interface TaskReview {
  id: string;
  taskId: string;
  assignedTo: string; // user ID of mentor
  status: ReviewStatus;
  notes?: string;
  score?: number; // 1-10
  createdBy: string; // user ID
  createdAt: Date;
  completedDate?: Date;
}

// Context interface
export interface TaskContextType {
  // Task Template methods
  taskTemplates: TaskTemplate[];
  addTaskTemplate: (template: Omit<TaskTemplate, "id" | "createdAt">, userId: string) => void;
  updateTaskTemplate: (id: string, data: Partial<TaskTemplate>) => void;
  deleteTaskTemplate: (id: string, taskDependents: string[]) => boolean;
  getUserAccessibleTemplates: (userId: string, users: any[]) => TaskTemplate[];
  
  // Task Dependency methods
  taskDependencies: TaskDependency[];
  addTaskDependency: (taskId: string, dependsOnTaskId: string) => void;
  removeTaskDependency: (taskId: string, dependsOnTaskId: string) => void;
  getTaskDependencies: (taskId: string) => string[];
  getTaskDependents: (taskId: string) => string[];
  isTaskDependencyValid: (taskId: string, dependsOnTaskId: string) => boolean;
  hasCompletedDependencies: (taskId: string) => boolean;
  
  // Personalized Task methods
  personalizedTasks: { [key: string]: PersonalizedTask };
  setPersonalizedTasks: React.Dispatch<React.SetStateAction<{ [key: string]: PersonalizedTask }>>;
  addPersonalizedTask: (task: Omit<PersonalizedTask, "id" | "createdAt" | "status">, userId: string) => void;
  updateTaskStatus: (id: string, status: TaskStatus, completedDate?: Date) => void;
  updatePersonalizedTask: (id: string, data: Partial<PersonalizedTask>) => void;
  deletePersonalizedTask: (id: string) => void;
  generatePersonalizedTasks: (
    assignments: TaskAssignment[], 
    templates: TaskTemplate[],
    userId: string,
    hasCompletedDependencies: (taskId: string) => boolean
  ) => Promise<void>;
  
  // Task Review methods
  taskReviews: TaskReview[];
  addTaskReview: (
    review: Omit<TaskReview, "id" | "createdAt" | "status">, 
    userId: string, 
    updateTaskCallback: (taskId: string, reviewId: string) => void
  ) => void;
  updateReviewStatus: (id: string, status: ReviewStatus, notes?: string, score?: number) => void;
  updateTaskReview: (id: string, data: Partial<TaskReview>) => void;
  deleteTaskReview: (id: string, clearReviewIdCallback?: (reviewId: string) => void) => void;
  
  // Additional methods
  getUnreviewedTasks: (managerId: string, users: any[]) => PersonalizedTask[];
  getReviewableTasks: (mentorId: string, users: any[]) => PersonalizedTask[];
  
  // User context methods
  getUsers: () => any[];
  
  // AI Config
  aiConfig?: {
    apiKey?: string;
    model?: string;
  };
}
