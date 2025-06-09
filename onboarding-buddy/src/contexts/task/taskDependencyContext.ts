
import { useState } from "react";
import { toast } from "sonner";
import { TaskDependency } from "@/types/tasks";
import { mockTaskDependencies } from "@/mocks/tasks";

export const useTaskDependencyContext = (
  getPersonalizedTask: (id: string) => any | undefined
) => {
  const [taskDependencies, setTaskDependencies] = useState<TaskDependency[]>(mockTaskDependencies || []);

  // Add a task dependency
  const addTaskDependency = (taskId: string, dependsOnTaskId: string) => {
    // Prevent circular dependencies
    if (taskId === dependsOnTaskId) {
      toast.error("A task cannot depend on itself");
      return;
    }
    
    // Check if dependency already exists
    const existingDependency = taskDependencies.find(
      (dep) => dep.taskId === taskId && dep.dependsOnTaskId === dependsOnTaskId
    );

    if (existingDependency) {
      toast.error("This dependency already exists");
      return;
    }
    
    // Prevent circular dependencies by checking if the target task already depends on the source task
    const circularDependency = checkForCircularDependency(dependsOnTaskId, taskId);
    if (circularDependency) {
      toast.error("Adding this dependency would create a circular reference");
      return;
    }

    const newDependency: TaskDependency = {
      taskId,
      dependsOnTaskId,
    };

    setTaskDependencies((prev) => [...prev, newDependency]);
    toast.success("Dependency added successfully");
  };

  // Remove a task dependency
  const removeTaskDependency = (taskId: string, dependsOnTaskId: string) => {
    setTaskDependencies((prev) =>
      prev.filter(
        (dep) => !(dep.taskId === taskId && dep.dependsOnTaskId === dependsOnTaskId)
      )
    );
    toast.success("Dependency removed successfully");
  };

  // Check for circular dependencies recursively
  const checkForCircularDependency = (taskId: string, targetDependencyId: string): boolean => {
    // Get direct dependencies of the task
    const directDependencies = taskDependencies
      .filter((dep) => dep.taskId === taskId)
      .map((dep) => dep.dependsOnTaskId);

    // Check if any direct dependency is the target
    if (directDependencies.includes(targetDependencyId)) {
      return true;
    }

    // Check recursively for each direct dependency
    for (const depId of directDependencies) {
      if (checkForCircularDependency(depId, targetDependencyId)) {
        return true;
      }
    }

    return false;
  };

  // Get all dependencies for a task
  const getTaskDependencies = (taskId: string): string[] => {
    return taskDependencies
      .filter((dep) => dep.taskId === taskId)
      .map((dep) => dep.dependsOnTaskId);
  };

  // Get all tasks that depend on a given task
  const getTaskDependents = (taskId: string): string[] => {
    return taskDependencies
      .filter((dep) => dep.dependsOnTaskId === taskId)
      .map((dep) => dep.taskId);
  };

  // Check if a dependency is valid (no circular dependencies)
  const isTaskDependencyValid = (taskId: string, dependsOnTaskId: string): boolean => {
    return !checkForCircularDependency(dependsOnTaskId, taskId);
  };

  // Check if all dependencies are completed for a task
  const hasCompletedDependencies = (taskId: string): boolean => {
    const dependencies = getTaskDependencies(taskId);
    
    // If no dependencies, return true
    if (dependencies.length === 0) {
      return true;
    }
    
    // Check if all dependency tasks are completed
    for (const depId of dependencies) {
      const depTask = getPersonalizedTask(depId);
      if (!depTask || depTask.status !== "completed") {
        return false;
      }
    }
    
    return true;
  };

  return {
    taskDependencies,
    addTaskDependency,
    removeTaskDependency,
    getTaskDependencies,
    getTaskDependents,
    isTaskDependencyValid,
    hasCompletedDependencies
  };
};
