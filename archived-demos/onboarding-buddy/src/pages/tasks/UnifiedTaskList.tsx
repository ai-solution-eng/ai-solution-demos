import React, { useState, useMemo, useEffect, useCallback } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useUser } from "@/contexts/user";
import { useTask } from "@/contexts/task";
import { useAIConfig } from "@/contexts/AIConfigContext";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { TaskStatus, ReviewStatus, TaskReview } from "@/types/tasks";
import { Label } from "@/components/ui/label";
import {Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Badge } from "@/components/ui/badge";
import {
  Search,
  Pencil,
  MoreHorizontal,
  Eye,
  CheckSquare,
  ChevronDown,
  ChevronUp,
  Star,
  UserCheck,
  UserPlus,
  ClipboardCheck,
  ClipboardList,
  Clock,
  AlertCircle,
  CheckCircle,
  AlertTriangle,
} from "lucide-react";
import { toast } from "sonner";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { TaskPagination } from "@/components/tasks/TaskPagination";

// Helper function to get user name by ID
const getAssignedUserName = (users: any[], userId: string) => {
  const user = users.find((u) => u.id === userId);
  return user ? user.name : "Unknown";
};

// Helper function to render priority stars
const PriorityStars = ({ priority }: { priority: number }) => (
  <div className="flex">
    {[...Array(5)].map((_, i) => (
      <Star
        key={i}
        size={16}
        className={`${i < priority ? "fill-current" : "text-muted-foreground"} text-amber-500`}
      />
    ))}
  </div>
);

// Helper function to get status display info
interface StatusInfo {
  text: string;
  icon: React.ReactNode;
  className: string;
}

const getStatusInfo = (
  status: TaskStatus, 
  reviewStatus?: ReviewStatus, 
  userRole?: string
): StatusInfo => {
  // For new hires, we show simplified statuses
  if (userRole === 'new-hire') {
    switch (status) {
      case 'assigned':
        return { 
          text: 'Assigned', 
          icon: <Clock size={16} className="mr-1" />, 
          className: 'bg-status-assigned text-purple-800' 
        };
      case 'in-progress':
        return { 
          text: 'In Progress', 
          icon: <Clock size={16} className="mr-1" />, 
          className: 'bg-status-in-progress text-amber-800' 
        };
      case 'completed':
      default:
        return { 
          text: 'Completed', 
          icon: <CheckCircle size={16} className="mr-1" />, 
          className: 'bg-status-completed text-green-800' 
        };
    }
  }
  
  // For managers/mentors/admins, we show more detailed statuses
  if (status === 'completed' && !reviewStatus) {
    return { 
      text: 'Review Pending', 
      icon: <AlertCircle size={16} className="mr-1" />, 
      className: 'bg-status-in-progress text-amber-800' 
    };
  }
  
  if (reviewStatus === 'assigned') {
    return { 
      text: 'Review Assigned', 
      icon: <UserCheck size={16} className="mr-1" />, 
      className: 'bg-status-assigned text-blue-800' 
    };
  }
  
  if (reviewStatus === 'in-progress') {
    return { 
      text: 'Review In Progress', 
      icon: <ClipboardCheck size={16} className="mr-1" />, 
      className: 'bg-status-in-progress text-amber-800' 
    };
  }
  
  if (reviewStatus === 'completed') {
    return { 
      text: 'Completed', 
      icon: <CheckCircle size={16} className="mr-1" />, 
      className: 'bg-status-completed text-green-800' 
    };
  }
  
  // Default statuses for non-completed tasks
  switch (status) {
    case 'assigned':
      return { 
        text: 'Assigned', 
        icon: <Clock size={16} className="mr-1" />, 
        className: 'bg-status-assigned text-purple-800' 
      };
    case 'in-progress':
      return { 
        text: 'In Progress', 
        icon: <Clock size={16} className="mr-1" />, 
        className: 'bg-status-in-progress text-amber-800' 
      };
    case 'cancelled':
      return { 
        text: 'Cancelled', 
        icon: <AlertTriangle size={16} className="mr-1" />, 
        className: 'bg-status-cancelled text-red-800' 
      };
    case 'completed':
      // This case should be handled above, but TypeScript needs it here for exhaustiveness
      return { 
        text: 'Completed', 
        icon: <CheckCircle size={16} className="mr-1" />, 
        className: 'bg-status-completed text-green-800' 
      };
    default:
      // This should never happen with proper types, but we need to handle it for TypeScript
      const _exhaustiveCheck: never = status;
      return { 
        text: 'Unknown Status', 
        icon: null, 
        className: '' 
      };
  }
};

// Helper function to check if a task's due date is overdue
// A task is considered overdue if:
// 1. It has a due date
// 2. The due date is before today
// 3. The task status is not 'completed'
const isDateOverdue = (date: Date | null, status: string): boolean => {
  if (!date || status === 'completed') return false;
  const today = new Date();
  // Set hours to 0 for accurate date comparison
  today.setHours(0, 0, 0, 0);
  return date < today;
};

const UnifiedTaskList: React.FC = () => {
  const { user, isImpersonating } = useAuth();
  const { users, getSubordinates } = useUser();
  const { aiConfig } = useAIConfig();
  const { 
    taskTemplates, 
    taskReviews,
    personalizedTasks, 
    updateTaskStatus, 
    generatePersonalizedTasks,
    getTaskDependents,
    deletePersonalizedTask,
    updatePersonalizedTask,
    addTaskReview,
    updateTaskReview,
    deleteTaskReview
  } = useTask();
  
  // State for filters and sorting
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [userFilter, setUserFilter] = useState<string>("all");
  const [reviewerFilter, setReviewerFilter] = useState<string>("all");
  const [sortField, setSortField] = useState<string | null>(null);
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc' | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(10);
  
  if (!user) return null;
  
  // Get subordinates for manager/mentor
  const subordinates = user.role === "manager" || user.role === "mentor" 
    ? getSubordinates(user.id) 
    : [];
  const subordinateIds = subordinates.map(sub => sub.id);
  
  // Get tasks with their review information
  const tasksWithReviews = useMemo(() => {
    return Object.values(personalizedTasks).map(task => {
      const review = task.reviewId ? taskReviews.find((r: any) => r.id === task.reviewId) : null;
      return {
        ...task,
        review,
        reviewer: review ? users.find(u => u.id === review.assignedTo) : null
      };
    });
  }, [personalizedTasks, taskReviews, users]);
  
  // Get all reviewers based on user role
  const availableReviewers = useMemo(() => {
    if (user.role === 'new-hire') {
      return [];
    }

    // For admins, show all managers and mentors
    if (user.role === 'admin') {
      return users.filter(u => u.role === 'manager' || u.role === 'mentor');
    }
    
    // For managers/mentors, show themselves and their direct reports
    return users.filter(u => 
      (u.role === 'mentor' && u.managerId === user.id) || // Mentors they manage
      u.id === user.id // Themselves
    );
  }, [user, users]);

  // Filter tasks based on user role
  let viewableTasks = tasksWithReviews;
  
  if (user.role === "new-hire") {
    viewableTasks = viewableTasks.filter(task => task.assignedTo === user.id);
  } else if (user.role === "mentor") {
    // Mentors see:
    // 1. Tasks they are reviewing
    // 2. Tasks assigned to their subordinates
    // 3. Completed tasks from peers (same manager) that need review
    viewableTasks = viewableTasks.filter(task => {
      // Tasks the mentor is reviewing
      if (task.review?.assignedTo === user.id) return true;
      
      // Tasks assigned to their subordinates
      if (task.assignedTo && subordinateIds.includes(task.assignedTo)) return true;
      
      // Completed tasks from peers (same manager) that need review
      if (task.status === 'completed' && !task.reviewId) {
        const taskAssignee = users.find(u => u.id === task.assignedTo);
        return taskAssignee?.managerId === user.managerId;
      }
      
      return false;
    });
  } else if (user.role === "manager") {
    // Managers see tasks from their subordinates
    viewableTasks = viewableTasks.filter(task => 
      task.assignedTo && subordinateIds.includes(task.assignedTo)
    );
  }
  
  // Apply filters and sorting
  const filteredTasks = useMemo(() => {
    let result = viewableTasks.filter((task) => {
      // Status filter logic
      let statusMatch = true;
      if (statusFilter !== "all") {
        if (statusFilter === 'review-pending') {
          statusMatch = task.status === 'completed' && !task.reviewId;
        } else if (statusFilter === 'review-assigned') {
          statusMatch = task.review?.status === 'assigned';
        } else if (statusFilter === 'review-in-progress') {
          statusMatch = task.review?.status === 'in-progress';
        } else if (statusFilter === 'completed') {
          statusMatch = task.status === 'completed' && task.review?.status === 'completed';
        } else {
          statusMatch = task.status === statusFilter;
        }
      }
      
      // User filter logic
      const userMatch = userFilter === "all" || task.assignedTo === userFilter;
      
      // Reviewer filter logic
      let reviewerMatch = true;
      if (reviewerFilter !== "all" && user.role !== 'new-hire') {
        if (reviewerFilter === 'unassigned') {
          reviewerMatch = !task.reviewId;
        } else {
          // Check if the task has a review and if it's assigned to the selected reviewer
          reviewerMatch = task.reviewId ? 
            task.review?.assignedTo === reviewerFilter : false;
        }
      }
      
      // Search logic
      const searchLower = search.toLowerCase();
      const taskName = task.name.toLowerCase();
      const assignedUser = users.find(u => u.id === task.assignedTo);
      const assignedUserName = assignedUser?.name.toLowerCase() || '';
      const reviewerName = task.reviewer?.name.toLowerCase() || '';
      
      const searchMatch = 
        taskName.includes(searchLower) || 
        assignedUserName.includes(searchLower) ||
        reviewerName.includes(searchLower) ||
        task.description.toLowerCase().includes(searchLower);
      
      return statusMatch && userMatch && reviewerMatch && searchMatch;
    });

    // Apply sorting
    if (sortField && sortDirection) {
      result.sort((a, b) => {
        let aValue: any, bValue: any;
        
        switch (sortField) {
          case 'name':
            aValue = a.name.toLowerCase();
            bValue = b.name.toLowerCase();
            break;
          case 'assignedTo':
            aValue = getAssignedUserName(users, a.assignedTo).toLowerCase();
            bValue = getAssignedUserName(users, b.assignedTo).toLowerCase();
            break;
          case 'reviewer':
            aValue = a.reviewer ? a.reviewer.name.toLowerCase() : '';
            bValue = b.reviewer ? b.reviewer.name.toLowerCase() : '';
            break;
          case 'priority':
            aValue = a.priority;
            bValue = a.priority;
            break;
          case 'dueDate':
            aValue = a.dueDate ? new Date(a.dueDate).getTime() : 0;
            bValue = a.dueDate ? new Date(a.dueDate).getTime() : 0;
            break;
          case 'score':
            aValue = a.review?.score || 0;
            bValue = a.review?.score || 0;
            break;
          default:
            return 0;
        }

        if (aValue < bValue) return sortDirection === 'asc' ? -1 : 1;
        if (aValue > bValue) return sortDirection === 'asc' ? 1 : -1;
        return 0;
      });
    }

    return result;
  }, [viewableTasks, statusFilter, userFilter, reviewerFilter, search, sortField, sortDirection, users, user.role]);

  // Apply pagination
  const paginatedTasks = useMemo(() => {
    return filteredTasks.slice(
      (currentPage - 1) * itemsPerPage,
      currentPage * itemsPerPage
    );
  }, [filteredTasks, currentPage, itemsPerPage]);

  // Handle page change
  const handlePageChange = useCallback((page: number) => {
    setCurrentPage(page);
  }, []);

  // Handle items per page change
  const handleItemsPerPageChange = useCallback((value: number) => {
    setItemsPerPage(value);
    setCurrentPage(1);
  }, []);

  // Handle sort
  const handleSort = (field: string) => {
    if (sortField === field) {
      // Toggle through sort directions: asc -> desc -> null
      if (sortDirection === 'asc') {
        setSortDirection('desc');
      } else if (sortDirection === 'desc') {
        setSortField(null);
        setSortDirection(null);
      } else {
        setSortDirection('asc');
      }
    } else {
      setSortField(field);
      setSortDirection('asc');
    }
  };

  // Handle task status update
  const handleStatusUpdate = (taskId: string, newStatus: TaskStatus) => {
    updateTaskStatus(taskId, newStatus);
    const statusText = {
      'assigned': 'assigned',
      'in-progress': 'in progress',
      'completed': 'completed',
      'cancelled': 'cancelled'
    }[newStatus] || 'updated';
    toast.success(`Task marked as ${statusText}`);
  };

  // Handle volunteer to review
  const handleVolunteerToReview = (taskId: string) => {
    if (!user) return;
    
    const task = personalizedTasks[taskId];
    if (!task) {
      console.error('Task not found:', taskId);
      toast.error('Task not found');
      return;
    }
    
    const newReview = {
      taskId,
      assignedTo: user.id,
      status: 'assigned' as const,
      notes: "",
      score: undefined,
      createdBy: user.id,
      createdAt: new Date().toISOString(),
    };
    
    addTaskReview(
      newReview,
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
    toast.success("You have volunteered to review this task");
  };

  // Handle start review
  const handleStartReview = (reviewId: string) => {
    updateTaskReview(reviewId, { status: 'in-progress' });
    toast.success("Review started");
  };

  // Get sort icon
  const getSortIcon = (field: string) => {
    if (sortField !== field) return null;
    return sortDirection === 'asc' ? <ChevronUp size={16} className="ml-1" /> : <ChevronDown size={16} className="ml-1" />;
  };

  // Get available status options for a task
  const getStatusOptions = (task: any) => {
    const options = [];
    
    // New hire actions
    if (!isImpersonating && user.role === 'new-hire' && task.assignedTo === user.id) {
      if (task.status === 'assigned') {
        options.push({
          label: 'Start Task',
          onClick: () => handleStatusUpdate(task.id, 'in-progress'),
          icon: <Clock size={16} className="mr-2" />
        });
      } else if (task.status === 'in-progress') {
        options.push({
          label: 'Complete Task',
          onClick: () => handleStatusUpdate(task.id, 'completed'),
          disabled: !task.completionNotes,
          icon: <CheckSquare size={16} className="mr-2" />,
          className: 'group relative',
          children: !task.completionNotes?.trim() && (
            <div className="fixed z-[100] left-1/2 -translate-x-1/2 -translate-y-10 bg-foreground text-background text-xs px-2 py-1 rounded opacity-100 whitespace-nowrap pointer-events-none">
              Task completion notes are required
            </div>
          )
        });
      }
    }
    
    // Manager/Mentor actions
    if (!isImpersonating && (user.role === 'manager' || user.role === 'mentor') && task.status === 'completed') {
      if (!task.review) {
        options.push({
          label: 'Volunteer to Review',
          onClick: () => handleVolunteerToReview(task.id),
          icon: <UserPlus size={16} className="mr-2" />
        });
      } else if (task.review.assignedTo === user.id) {
        if (task.review.status === 'assigned') {
          options.push({
            label: 'Start Review',
            onClick: () => handleStartReview(task.review.id),
            icon: <ClipboardCheck size={16} className="mr-2" />
          });
        } else if (task.review.status === 'in-progress') {
          options.push({
            label: 'Continue Review',
            onClick: () => {},
            icon: <ClipboardCheck size={16} className="mr-2" />,
            link: `/tasks/${task.id}?tab=review`
          });
        }
      }
    }
    
    // Always add view task option
    options.push({
      label: 'View Task',
      onClick: () => {},
      icon: <Eye size={16} className="mr-2" />,
      link: `/tasks/${task.id}`
    });
    
    return options;
  };

  // Get available status filter options based on user role
  const statusFilterOptions = useMemo(() => {
    const options = [
      { value: 'all', label: 'All Statuses' },
      { value: 'assigned', label: 'Assigned' },
      { value: 'in-progress', label: 'In Progress' },
    ];
    
    // Add review-related statuses for managers/mentors
    if (user.role === 'manager' || user.role === 'mentor' || user.role === 'admin') {
      options.push(
        { value: 'review-pending', label: 'Review Pending' },
        { value: 'review-assigned', label: 'Review Assigned' },
        { value: 'review-in-progress', label: 'Review In Progress' }
      );
    }
    
    // Add completed status
    options.push({ value: 'completed', label: 'Completed' });
    
    return options;
  }, [user.role]);
  
  // Get user filter options
  const userFilterOptions = useMemo(() => {
    const options = [{ value: 'all', label: 'All New Hires' }];
    
    if (user.role === 'new-hire') {
      // New hires can only see their own tasks
      return [];
    } else {
      let userList = [];
      
      if (user.role === 'admin') {
        // Admins can see all users
        userList = users.filter(u => u.role === 'new-hire');
      } else {
        // Managers and mentors can see their subordinates
        userList = users.filter(u => subordinateIds.includes(u.id));
      }
      
      // Sort users alphabetically by name
      const sortedUsers = [...userList].sort((a, b) => 
        (a.name || '').localeCompare(b.name || '')
      );
      
      return [
        ...options,
        ...sortedUsers.map(u => ({
          value: u.id,
          label: u.name || `User ${u.id.slice(0, 6)}`
        }))
      ];
    }
  }, [user, users, subordinateIds]);
  
  // Get reviewer filter options
  const reviewerFilterOptions = useMemo(() => {
    // Only show reviewer filter for non-new-hire users
    if (user.role === 'new-hire') {
      return [];
    }
    
    const options = [
      { value: 'all', label: 'All Reviewers' },
      { value: 'unassigned', label: 'Unassigned' }
    ];
    
    // Get all possible reviewers based on user role
    let reviewers = [];
    
    if (user.role === 'admin') {
      // Admins see all managers and mentors
      reviewers = users.filter(u => u.role === 'manager' || u.role === 'mentor');
    } else if (user.role === 'manager') {
      // Managers see themselves and their direct reports (mentors they manage)
      reviewers = [
        ...users.filter(u => u.role === 'mentor' && u.managerId === user.id),
        users.find(u => u.id === user.id)
      ].filter(Boolean);
    } else {
      // Mentors see themselves and their manager
      const mentorUser = users.find(u => u.id === user.id);
      const manager = mentorUser?.managerId ? users.find(u => u.id === mentorUser.managerId) : null;
      
      reviewers = [
        mentorUser,
        ...(manager ? [manager] : [])
      ].filter(Boolean);
    }
    
    // Sort reviewers alphabetically by name
    const sortedReviewers = [...reviewers].sort((a, b) => 
      (a.name || '').localeCompare(b.name || '')
    );
    
    // Add reviewers to options
    const reviewerOptions = sortedReviewers.map(reviewer => ({
      value: reviewer.id,
      label: `${reviewer.name || `User ${reviewer.id.slice(0, 6)}`} (${reviewer.role})`
    }));
    
    return [...options, ...reviewerOptions];
  }, [user, users]);
  
  // Reset reviewer filter when it's no longer valid
  useEffect(() => {
    if (user.role !== 'new-hire' && reviewerFilter !== 'all' && reviewerFilter !== 'unassigned') {
      const reviewerExists = reviewerFilterOptions.some(option => option.value === reviewerFilter);
      if (!reviewerExists) {
        setReviewerFilter('all');
      }
    }
  }, [reviewerFilter, reviewerFilterOptions, user.role]);

  // Reset reviewer filter when user changes
  useEffect(() => {
    setReviewerFilter('all');
  }, [user.id]);

  const [selectedUser, setSelectedUser] = useState<string | null>(null);
  const [selectedTemplates, setSelectedTemplates] = useState<string[]>([]);
  const [difficultyLevels, setDifficultyLevels] = useState<Record<string, number>>({});
  const [priorityLevels, setPriorityLevels] = useState<Record<string, number>>({});
  const [isGenerating, setIsGenerating] = useState(false);

  // Get filterable users based on role
  const getFilterableUsers = useCallback(() => {
    if (user.role === 'admin') {
      return Object.values(users).filter(u => u.role === 'new-hire');
    } else if (user.role === 'manager') {
      return getSubordinates(user.id).filter(u => u.role === 'new-hire');
    }
    return [];
  }, [user, users, getSubordinates]);

  const handleTemplateSelection = (templateId: string) => {
    if (selectedTemplates.includes(templateId)) {
      setSelectedTemplates(selectedTemplates.filter(id => id !== templateId));
    } else {
      setSelectedTemplates([...selectedTemplates, templateId]);
    }
  };

  const handleDifficultyChange = (templateId: string, value: string) => {
    setDifficultyLevels(prev => ({ ...prev, [templateId]: parseInt(value) }));
  };

  const handlePriorityChange = (templateId: string, value: string) => {
    setPriorityLevels(prev => ({ ...prev, [templateId]: parseInt(value) }));
  };

  const handleGenerateTasks = async () => {
    if (!selectedUser) {
      toast.error('Please select a new hire');
      return;
    }
    
    if (selectedTemplates.length === 0) {
      toast.error('Please select at least one task template');
      return;
    }
    
    setIsGenerating(true);
    try {
      // Create an array of TaskAssignment objects
      const assignments = selectedTemplates.map(templateId => ({
        templateId,
        assignedToId: selectedUser,
        difficulty: difficultyLevels[templateId] || 3, // Default to medium difficulty
        priority: priorityLevels[templateId] || 3,    // Default to medium priority
        dependencies: [] // No dependencies by default
      }));
      
      // Call generatePersonalizedTasks with all required parameters
      await generatePersonalizedTasks(
        assignments,
        taskTemplates,
        user.id,
        (taskId: string) => {
          // Simple implementation of hasCompletedDependencies
          // This should be replaced with the actual implementation from your task context
          const task = Object.values(personalizedTasks).find(t => t.id === taskId);
          if (!task || !task.dependencies || task.dependencies.length === 0) return true;
          
          // Check if all dependencies are completed
          return task.dependencies.every(depId => {
            const depTask = Object.values(personalizedTasks).find(t => t.id === depId);
            return depTask?.status === 'completed';
          });
        }
      );
      
      // Reset form
      setSelectedTemplates([]);
      setDifficultyLevels({});
      setPriorityLevels({});
      setSelectedUser(null);
      
      toast.success('Tasks generated successfully');
    } catch (error) {
      console.error('Error generating tasks:', error);
      toast.error(error instanceof Error ? error.message : 'Failed to generate tasks');
    } finally {
      setIsGenerating(false);
    }
  };

  return (
    <div className="container mx-auto">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">Onboarding Tasks</h1>
        
        {/* Generate Task button for admins/managers */}
        {(user.role === 'admin' || user.role === 'manager') && (
          <>
            {!aiConfig.apiKey ? (
              <div className="flex items-center gap-2">
                <span className="text-xs text-amber-600 dark:text-amber-400 whitespace-nowrap">
                  Personalized Task Generator AI is not properly configured. Please contact your administrator to set it up.
                </span>
                <Button disabled>
                  <ClipboardList size={16} className="mr-2" />
                  Generate Tasks
                </Button>
              </div>
            ) : (
              <Dialog>
                <DialogTrigger asChild>
                  <Button className="w-[200px]">
                    <ClipboardList size={16} className="mr-2" />
                    Generate Tasks
                  </Button>
                </DialogTrigger>
                <DialogContent className="max-w-lg">
                  <DialogHeader>
                    <DialogTitle>Generate Personalized Tasks</DialogTitle>
                    <DialogDescription>
                      Select task templates and a new hire to create personalized tasks.
                    </DialogDescription>
                  </DialogHeader>
        
                  <div className="py-2 space-y-4">
                    <div className="space-y-2">
                      <Label>Select New Hire</Label>
                      <Select value={selectedUser} onValueChange={setSelectedUser}>
                        <SelectTrigger>
                          <SelectValue placeholder="Select a new hire" />
                        </SelectTrigger>
                        <SelectContent>
                          {getFilterableUsers()
                            .filter(u => u.role === 'new-hire')
                            .map(u => (
                              <SelectItem key={u.id} value={u.id}>
                                {u.name}
                              </SelectItem>
                            ))}
                        </SelectContent>
                      </Select>
                    </div>
                    
                    <div className="space-y-2">
                      <Label>Select Templates</Label>
                      <div className="max-h-60 overflow-y-auto border rounded-md p-2">
                        {taskTemplates
                          .filter(template => {
                            // Admins see all templates, managers see their own and admin-created templates
                            return user.role === "admin" || 
                                  template.createdBy === user.id || 
                                  template.createdByRole === "admin";
                          })
                          .map(template => (
                            <div key={template.id} className="border-b py-2 last:border-0">
                              <div className="flex items-center mb-2">
                                <input 
                                  type="checkbox" 
                                  id={`template-${template.id}`}
                                  className="mr-2"
                                  checked={selectedTemplates.includes(template.id)}
                                  onChange={() => handleTemplateSelection(template.id)}
                                />
                                <Label htmlFor={`template-${template.id}`} className="text-sm font-medium">
                                  {template.name}
                                </Label>
                              </div>
                              
                              {selectedTemplates.includes(template.id) && (
                                <div className="ml-6 grid grid-cols-2 gap-2">
                                  <div>
                                    <Label htmlFor={`difficulty-${template.id}`} className="text-xs">
                                      Difficulty
                                    </Label>
                                    <Select 
                                      value={String(difficultyLevels[template.id] || 3)}
                                      onValueChange={(value) => handleDifficultyChange(template.id, value)}
                                    >
                                      <SelectTrigger id={`difficulty-${template.id}`}>
                                        <SelectValue />
                                      </SelectTrigger>
                                      <SelectContent>
                                        <SelectItem value="1">1 - Very Easy</SelectItem>
                                        <SelectItem value="2">2 - Easy</SelectItem>
                                        <SelectItem value="3">3 - Medium</SelectItem>
                                        <SelectItem value="4">4 - Hard</SelectItem>
                                        <SelectItem value="5">5 - Very Hard</SelectItem>
                                      </SelectContent>
                                    </Select>
                                  </div>
                                  <div>
                                    <Label htmlFor={`priority-${template.id}`} className="text-xs">
                                      Priority
                                    </Label>
                                    <Select 
                                      value={String(priorityLevels[template.id] || 3)}
                                      onValueChange={(value) => handlePriorityChange(template.id, value)}
                                    >
                                      <SelectTrigger id={`priority-${template.id}`}>
                                        <SelectValue />
                                      </SelectTrigger>
                                      <SelectContent>
                                        <SelectItem value="1">1 - Lowest</SelectItem>
                                        <SelectItem value="2">2 - Low</SelectItem>
                                        <SelectItem value="3">3 - Medium</SelectItem>
                                        <SelectItem value="4">4 - High</SelectItem>
                                        <SelectItem value="5">5 - Highest</SelectItem>
                                      </SelectContent>
                                    </Select>
                                  </div>
                                </div>
                              )}
                            </div>
                          ))}
                      </div>
                    </div>
                  </div>
                  
                  <DialogFooter>
                    <Button 
                      onClick={handleGenerateTasks} 
                      disabled={selectedTemplates.length === 0 || !selectedUser || isGenerating || !aiConfig.apiKey}
                    >
                      {isGenerating ? (
                        <>
                          <svg className="animate-spin -ml-1 mr-2 h-4 w-4 text-white" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                          </svg>
                          Generating...
                        </>
                      ) : 'Generate Tasks'}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            )}
          </>
        )}
      </div>
      
      <div className="bg-card rounded-lg border shadow-sm">
        {/* Filters */}
        <div className="flex flex-col gap-4 p-4 md:flex-row md:items-center md:justify-between">
          <div className="relative w-full md:max-w-sm">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              type="search"
              placeholder="Search tasks..."
              className="w-full pl-8"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          
          <div className="flex flex-wrap gap-2">
            <Select value={statusFilter} onValueChange={setStatusFilter}>
              <SelectTrigger className="w-[180px]">
                <SelectValue placeholder="Status" />
              </SelectTrigger>
              <SelectContent>
                {statusFilterOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            
            {userFilterOptions.length > 0 && (
              <Select value={userFilter} onValueChange={setUserFilter}>
                <SelectTrigger className="w-[180px]">
                  <SelectValue placeholder="Assigned To" />
                </SelectTrigger>
                <SelectContent>
                  {userFilterOptions.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}

            {user.role !== 'new-hire' && reviewerFilterOptions.length > 0 && (
              <Select value={reviewerFilter} onValueChange={setReviewerFilter}>
                <SelectTrigger className="w-[180px]">
                  <SelectValue placeholder="Reviewer" />
                </SelectTrigger>
                <SelectContent>
                  {reviewerFilterOptions.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
        </div>
        
        {/* Tasks Table */}
        <div className="rounded-md border">
          <Table className="table-fixed w-full">
            <TableHeader>
              <TableRow>
                <TableHead 
                  className="cursor-pointer w-[300px] whitespace-nowrap"
                  onClick={() => handleSort('name')}
                >
                  <div className="flex items-center">
                    Name
                    {getSortIcon('name')}
                  </div>
                </TableHead>
                <TableHead 
                  className="cursor-pointer w-[150px] whitespace-nowrap"
                  onClick={() => handleSort('assignedTo')}
                >
                  <div className="flex items-center">
                    Assigned To
                    {getSortIcon('assignedTo')}
                  </div>
                </TableHead>
                <TableHead 
                  className="cursor-pointer w-[100px] whitespace-nowrap"
                  onClick={() => handleSort('priority')}
                >
                  <div className="flex items-center">
                    Priority
                    {getSortIcon('priority')}
                  </div>
                </TableHead>
                <TableHead 
                  className="cursor-pointer w-[100px] whitespace-nowrap"
                  onClick={() => handleSort('dueDate')}
                >
                  <div className="flex items-center">
                    Due Date
                    {getSortIcon('dueDate')}
                  </div>
                </TableHead>
                {(user.role !== 'new-hire') && (
                  <TableHead 
                    className="cursor-pointer w-[150px] whitespace-nowrap"
                    onClick={() => handleSort('reviewer')}
                  >
                    <div className="flex items-center">
                      Reviewer
                      {getSortIcon('reviewer')}
                    </div>
                  </TableHead>
                )}
                {user.role !== 'new-hire' && (
                  <TableHead 
                    className="cursor-pointer w-[80px] whitespace-nowrap"
                    onClick={() => handleSort('score')}
                  >
                    <div className="flex items-center">
                      Score
                      {getSortIcon('score')}
                    </div>
                  </TableHead>
                )}
                <TableHead 
                  className="cursor-pointer w-[180px] whitespace-nowrap"
                  onClick={() => handleSort('status')}
                >
                  <div className="flex items-center">
                    Status
                    {getSortIcon('status')}
                  </div>
                </TableHead>
                <TableHead className="w-[100px] text-right pr-6">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredTasks.length > 0 ? (
                paginatedTasks.map((task) => {
                  const statusInfo = getStatusInfo(task.status, task.review?.status, user.role);
                  const assignedUser = users.find(u => u.id === task.assignedTo);
                  const dueDate = task.dueDate ? new Date(task.dueDate) : null;
                  
                  return (
                    <TableRow key={task.id}>
                      <TableCell className="font-medium w-[200px]">
                        <Link 
                          to={`/tasks/${task.id}`}
                          className="hover:underline hover:text-primary block truncate"
                          title={task.name}
                        >
                          {task.name}
                        </Link>
                      </TableCell>
                      <TableCell className="w-[150px]">
                        <div className="truncate">
                          <span className="h-2 w-2 rounded-full bg-primary" />
                          {assignedUser?.name || 'Unknown User'}
                        </div>
                      </TableCell>
                      <TableCell className="w-[100px]">
                        <PriorityStars priority={task.priority} />
                      </TableCell>
                      <TableCell className="w-[120px] whitespace-nowrap">
                        {dueDate ? (
                          <span className={`${isDateOverdue(dueDate, task.status) ? 'font-bold text-red-600' : ''}`}>
                            {dueDate.toLocaleDateString()}
                            <br />
                            {isDateOverdue(dueDate, task.status) ? '(Overdue)' : ''}
                          </span>
                        ) : 'No due date'}
                      </TableCell>
                      {user.role !== 'new-hire' && (
                        <TableCell className="w-[150px]">
                          <div className="truncate">{task.reviewer?.name || '-'}</div>
                        </TableCell>
                      )}
                      {user.role !== 'new-hire' && (
                        <TableCell className="w-[80px]">
                          {task.review?.score ? (
                            <div className="flex items-center">
                              <span className="font-medium mr-1">{task.review.score}/10</span>
                              {task.review.score < 5 && (
                                <AlertCircle size={16} className="text-red-500 flex-shrink-0" />
                              )}
                              {task.review.score >= 8 && (
                                <Star size={16} className="text-amber-500 fill-current flex-shrink-0" />
                              )}
                            </div>
                          ) : '-'}
                        </TableCell>
                      )}
                      <TableCell className="w-[150px]">
                        <Badge className={statusInfo.className}>
                          <div className="flex items-center">
                            {statusInfo.icon}
                            {statusInfo.text}
                          </div>
                        </Badge>
                      </TableCell>
                      <TableCell className="w-[100px] text-right pr-6">
                        <div className="flex justify-end">
                          <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button variant="ghost" size="icon">
                              <MoreHorizontal size={16} />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end">
                            {getStatusOptions(task).map((option, index) => (
                              <DropdownMenuItem 
                                key={index}
                                onClick={option.onClick}
                                disabled={option.disabled}
                                className={`${option.disabled ? 'opacity-50 cursor-not-allowed' : ''} ${option.className || ''}`}
                              >
                                {option.link ? (
                                  <Link to={option.link} className="flex items-center w-full">
                                    {option.icon}
                                    {option.label}
                                  </Link>
                                ) : (
                                  <>
                                    {option.icon}
                                    {option.label}
                                  </>
                                )}
                                {option.children}
                              </DropdownMenuItem>
                            ))}
                          </DropdownMenuContent>
                          </DropdownMenu>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })
              ) : (
                <TableRow>
                  <TableCell colSpan={user.role === 'new-hire' ? 7 : 8} className="h-24 text-center">
                    No tasks found.
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>

        <div className="flex flex-col gap-4 p-4 sm:flex-row sm:items-center sm:justify-between">
        {filteredTasks.length > 0 && (
          <TaskPagination
            currentPage={currentPage}
            itemsPerPage={itemsPerPage}
            totalItems={filteredTasks.length}
            onPageChange={handlePageChange}
            onItemsPerPageChange={handleItemsPerPageChange}
          />
        )}
        </div>
      </div>
    </div>
  );
};

export default UnifiedTaskList;