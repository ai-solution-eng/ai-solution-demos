
import React, { useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useTask } from "@/contexts/task";
import { useUser } from "@/contexts/user/UserContext";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { 
  Search, 
  Plus, 
  MoreHorizontal,
  Pencil,
  Trash2,
  ClipboardList,
  Clock,
  ChevronUp,
  ChevronDown,
} from "lucide-react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { TaskTemplatePagination } from "@/components/tasks/TaskTemplateListComponents/TaskTemplatePagination";

const TaskTemplateList: React.FC = () => {
  const { user } = useAuth();
  const { users } = useUser();
  const { taskTemplates, deleteTaskTemplate, getTaskDependents } = useTask();
  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState<string>("all");
  const [userFilter, setUserFilter] = useState<string>("all");
  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(10);
  const [sortField, setSortField] = useState<string | null>(null);
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc' | null>(null);

  // Get unique users who have created task templates, filtered by role
  const filteredUsers = React.useMemo(() => {
    // Get all unique creator IDs from task templates
    const creatorIds = new Set(taskTemplates.map(template => template.createdBy));
    
    // Since we don't have a users list, we'll return an empty array
    // You might want to fetch users from your API or context if needed
    return [];
    
    // If you have a users list, you would do something like:
    // return users.filter(user => {
    //   const hasCreatedTemplates = creatorIds.has(user.id);
    //   const matchesRole = roleFilter === "all" || user.role === roleFilter;
    //   return hasCreatedTemplates && matchesRole;
    // });
  }, [roleFilter, taskTemplates]);

  // Reset user filter when role changes
  React.useEffect(() => {
    setUserFilter("all");
  }, [roleFilter]);

  if (!user) return null;

  // Sort templates based on current sort field and direction
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

  // Filter templates based on user role, search, role filter, and user filter
  const visibleTemplates = taskTemplates.filter((template) => {
    // Admin can see all templates
    // Manager can see their own templates and templates created by admins
    // Get the creator of the template
    const creator = users.find(u => u.id === template.createdBy);
    
    // Admin can see all templates
    // Manager can see their own templates and templates created by admins
    const isVisibleForRole = 
      user.role === "admin" || 
      (user.role === "manager" && (template.createdBy === user.id || 
        creator?.role === "admin"));
    
    // Apply role filter if set
    const matchesRoleFilter = roleFilter === "all" || creator.role === roleFilter;
    
    // Apply user filter if set
    const matchesUserFilter = userFilter === "all" || template.createdBy === userFilter;
    
    // Apply search filter (searches in template name, description, and creator name)
    const searchLower = search.toLowerCase();
    const matchesSearch = 
      searchLower === "" ||
      template.name.toLowerCase().includes(searchLower) || 
      template.description.toLowerCase().includes(searchLower) ||
      (creator.name?.toLowerCase().includes(searchLower) || false);
    
    return isVisibleForRole && matchesRoleFilter && matchesUserFilter && matchesSearch;
  });

  // Apply sorting
  const sortedTemplates = [...visibleTemplates].sort((a, b) => {
    if (!sortField || !sortDirection) return 0;
    
    if (sortField === 'name') {
      return sortDirection === 'asc' 
        ? a.name.localeCompare(b.name) 
        : b.name.localeCompare(a.name);
    }
    
    if (sortField === 'description') {
      return sortDirection === 'asc' 
        ? a.description.localeCompare(b.description) 
        : b.description.localeCompare(a.description);
    }
    
    if (sortField === 'estimatedEffort') {
      return sortDirection === 'asc' 
        ? a.estimatedEffort - b.estimatedEffort 
        : b.estimatedEffort - a.estimatedEffort;
    }
    
    if (sortField === 'createdBy') {
      const creatorA = users.find(u => u.id === a.createdBy)?.name || '';
      const creatorB = users.find(u => u.id === b.createdBy)?.name || '';
      return sortDirection === 'asc'
        ? creatorA.localeCompare(creatorB)
        : creatorB.localeCompare(creatorA);
    }
    
    return 0;
  });

  // Implement pagination
  const totalPages = Math.ceil(sortedTemplates.length / itemsPerPage);
  const paginatedTemplates = sortedTemplates.slice(
    (currentPage - 1) * itemsPerPage,
    currentPage * itemsPerPage
  );

  // Find creator name
  const getCreatorName = (userId: string) => {
    const user = users.find(u => u.id === userId);
    return user ? user.name : "Unknown";
  };

  // Check if template was created by an admin
  const isTemplateCreatedByAdmin = (template: any) => {
    const creator = users.find(u => u.id === template.createdBy);
    return creator?.role === "admin";
  };

  // Get sort icon for a column
  const getSortIcon = (field: string) => {
    if (sortField !== field) return null;
    return sortDirection === 'asc' ? <ChevronUp size={16} /> : <ChevronDown size={16} />;
  };

  return (
    <div className="container mx-auto">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">Task Templates</h1>
        <Link to="/task-templates/new">
          <Button className="w-[200px]">
            <Plus size={16} className="mr-2 w-" />
            Create Template
          </Button>
        </Link>
      </div>

      <Card className="mb-8">
        <CardHeader>
          <CardTitle>About Task Templates</CardTitle>
          <CardDescription>
            Task templates are the foundation for creating personalized onboarding tasks for new hires.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
            <div className="flex gap-4 items-start">
              <div className="bg-blue-100 p-2 rounded-full">
                <ClipboardList size={20} className="text-blue-600" />
              </div>
              <div>
                <h3 className="text-sm font-medium">Reusable</h3>
                <p className="text-sm text-muted-foreground">
                  Create once and assign to multiple new hires
                </p>
              </div>
            </div>
            
            <div className="flex gap-4 items-start">
              <div className="bg-green-100 p-2 rounded-full">
                <Clock size={20} className="text-green-600" />
              </div>
              <div>
                <h3 className="text-sm font-medium">Time-saving</h3>
                <p className="text-sm text-muted-foreground">
                  Define estimated effort for better planning
                </p>
              </div>
            </div>
            
            <div className="flex gap-4 items-start">
              <div className="bg-purple-100 p-2 rounded-full">
                <ClipboardList size={20} className="text-purple-600" />
              </div>
              <div>
                <h3 className="text-sm font-medium">AI-enhanced</h3>
                <p className="text-sm text-muted-foreground">
                  Generate personalized versions for each new hire
                </p>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="bg-card rounded-lg border shadow-sm">
        <div className="p-4 space-y-4">
          <div className="flex flex-col sm:flex-row gap-4">
            <div className="relative flex-1">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Search by name, description, or creator..."
                className="pl-8"
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setCurrentPage(1);
                }}
              />
            </div>
            <div className="flex gap-2">
              <Select 
                value={roleFilter}
                onValueChange={(value) => {
                  setRoleFilter(value);
                  setCurrentPage(1);
                }}
              >
                <SelectTrigger className="w-[150px]">
                  <SelectValue placeholder="Role" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Roles</SelectItem>
                  <SelectItem value="admin">Admin</SelectItem>
                  <SelectItem value="manager">Manager</SelectItem>
                </SelectContent>
              </Select>
              
              <Select 
                value={userFilter}
                onValueChange={(value) => {
                  setUserFilter(value);
                  setCurrentPage(1);
                }}
                disabled={filteredUsers.length === 0}
              >
                <SelectTrigger className="w-[180px]">
                  <SelectValue placeholder="Select user" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All Users</SelectItem>
                  {filteredUsers.map((user) => (
                    <SelectItem key={user.id} value={user.id}>
                      {user.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>

        <div className="rounded-md border">
          <Table className="table-fixed w-full">
            <TableHeader>
              <TableRow>
                <TableHead 
                  className="cursor-pointer w-[250px] whitespace-nowrap"
                  onClick={() => handleSort('name')}
                >
                  <div className="flex items-center">
                    Name
                    {getSortIcon('name')}
                  </div>
                </TableHead>
                <TableHead 
                  className="cursor-pointer w-[400px] whitespace-nowrap"
                  onClick={() => handleSort('description')}
                >
                  <div className="flex items-center">
                    Description
                    {getSortIcon('description')}
                  </div>
                </TableHead>
                <TableHead 
                  className="cursor-pointer w-[120px] whitespace-nowrap"
                  onClick={() => handleSort('estimatedEffort')}
                >
                  <div className="flex items-center">
                    Est. Effort
                    {getSortIcon('estimatedEffort')}
                  </div>
                </TableHead>
                <TableHead 
                  className="cursor-pointer w-[200px] whitespace-nowrap"
                  onClick={() => handleSort('createdBy')}
                >
                  <div className="flex items-center">
                    Created By
                    {getSortIcon('createdBy')}
                  </div>
                </TableHead>
                <TableHead className="w-[120px] text-right pr-6">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {paginatedTemplates.length > 0 ? (
                paginatedTemplates.map((template) => (
                  <TableRow key={template.id}>
                    <TableCell className="font-medium w-[250px]">
                      <div className="flex items-center">
                        <ClipboardList size={16} className="mr-2 text-muted-foreground" />
                        <Link 
                          to={`/task-templates/${template.id}`}
                          className="hover:underline hover:text-primary truncate"
                        >
                          {template.name}
                        </Link>
                      </div>
                    </TableCell>
                    <TableCell className="w-[400px]">
                      <div className="truncate">{template.description}</div>
                    </TableCell>
                    <TableCell className="w-[120px]">
                      {template.estimatedEffort} {template.estimatedEffort === 1 ? 'day' : 'days'}
                    </TableCell>
                    <TableCell className="w-[200px]">{getCreatorName(template.createdBy)}</TableCell>
                    <TableCell className="w-[120px] text-right pr-6">
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="ghost" size="sm">
                            <MoreHorizontal size={16} />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          {/* Show View option for all users, but Edit only for admins or non-admin created templates */}
                          <Link to={`/task-templates/${template.id}`}>
                            <DropdownMenuItem>
                              <Pencil size={14} className="mr-2" />
                              {user.role === "admin" || !isTemplateCreatedByAdmin(template) ? 'Edit' : 'View'}
                            </DropdownMenuItem>
                          </Link>
                          
                          {/* Only allow deleting templates not created by admin, unless current user is admin */}
                          {(user.role === "admin" || !isTemplateCreatedByAdmin(template)) && (
                            <AlertDialog>
                              <AlertDialogTrigger asChild>
                                <DropdownMenuItem onSelect={(e) => e.preventDefault()}>
                                  <Trash2 size={14} className="mr-2" />
                                  Delete
                                </DropdownMenuItem>
                              </AlertDialogTrigger>
                              <AlertDialogContent>
                                <AlertDialogHeader>
                                  <AlertDialogTitle>Are you sure?</AlertDialogTitle>
                                  <AlertDialogDescription>
                                    This will permanently delete this task template.
                                    This action cannot be undone.
                                  </AlertDialogDescription>
                                </AlertDialogHeader>
                                <AlertDialogFooter>
                                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                                  <AlertDialogAction
                                    className="bg-destructive"
                                    onClick={() => {
                                      const taskDependents = getTaskDependents(template.id);
                                      deleteTaskTemplate(template.id, taskDependents);
                                    }}
                                  >
                                    Delete
                                  </AlertDialogAction>
                                </AlertDialogFooter>
                              </AlertDialogContent>
                            </AlertDialog>
                          )}
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </TableCell>
                  </TableRow>
                ))
              ) : (
                <TableRow>
                  <TableCell colSpan={5} className="text-center py-6">
                    No task templates found
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
        
        <TaskTemplatePagination
          currentPage={currentPage}
          itemsPerPage={itemsPerPage}
          totalItems={sortedTemplates.length}
          onPageChange={setCurrentPage}
          onItemsPerPageChange={(value) => {
            setItemsPerPage(value);
            setCurrentPage(1);
          }}
        />
      </div>
    </div>
  );
};

export default TaskTemplateList;
