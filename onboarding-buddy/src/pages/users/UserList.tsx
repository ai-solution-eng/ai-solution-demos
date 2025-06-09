
import React, { useState, useMemo, useCallback } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useUser } from "@/contexts/user";
import { Link, useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Plus } from "lucide-react";
import { toast } from "sonner";

// Components
import { UserFilters } from "@/components/users/UserListComponents/UserFilters";
import { UserTable } from "@/components/users/UserListComponents/UserTable";
import { UserPagination } from "@/components/users/UserListComponents/UserPagination";

// Utils & Types
import { filterVisibleUsers, sortUsers } from "@/lib/user-utils";
import { User, UserRole } from "@/contexts/AuthContext";
import { UserFiltersProps, UserPaginationProps, UserTableProps } from "@/types";

export const UserList: React.FC = () => {
  const { user } = useAuth();
  const { users, deleteUser, canManageUser, impersonateUser } = useUser();
  const navigate = useNavigate();
  
  // State
  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [itemsPerPage, setItemsPerPage] = useState(10);
  const [sortField, setSortField] = useState<string | null>(null);
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc' | null>(null);

  // Memoized filtered and sorted users
  const { visibleUsers, sortedUsers } = useMemo(() => {
    if (!user) return { visibleUsers: [], sortedUsers: [] };
    
    const filtered = filterVisibleUsers(users, user, search, roleFilter);
    const sorted = sortUsers(filtered, sortField, sortDirection, users);
    
    return { visibleUsers: filtered, sortedUsers: sorted };
  }, [user, users, search, roleFilter, sortField, sortDirection]);

  // Pagination
  const paginatedUsers = useMemo(() => {
    return sortedUsers.slice(
      (currentPage - 1) * itemsPerPage,
      currentPage * itemsPerPage
    );
  }, [sortedUsers, currentPage, itemsPerPage]);

  // Handlers
  const handleSort = useCallback((field: string) => {
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
  }, [sortField, sortDirection]);

  const handleDeleteUser = useCallback((userId: string) => {
    deleteUser(userId);
    // Reset to first page if the current page becomes empty after deletion
    if (paginatedUsers.length === 1 && currentPage > 1) {
      setCurrentPage(currentPage - 1);
    }
  }, [deleteUser, paginatedUsers.length, currentPage]);

  const handleViewProgress = useCallback((userId: string, userRole: UserRole) => {
    // Only allow impersonation of new-hire and mentor users
    if (userRole !== 'new-hire' && userRole !== 'mentor') {
      toast.error('You can only view progress of New Hire or Mentor users');
      return;
    }
    
    // Check if current user has permission to impersonate
    if (user?.role !== 'admin' && user?.role !== 'manager') {
      toast.error('You do not have permission to view user progress');
      return;
    }
    
    impersonateUser(userId);
    navigate("/dashboard");
  }, [impersonateUser, navigate, user?.role]);

  const handlePageChange = useCallback((page: number) => {
    setCurrentPage(page);
  }, []);

  const handleItemsPerPageChange = useCallback((value: number) => {
    setItemsPerPage(value);
    setCurrentPage(1);
  }, []);

  if (!user) return null;

  return (
    <div className="container mx-auto">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold">Users</h1>
        {(user.role === "admin" || user.role === "manager") && (
          <Link to="/users/new">
            <Button className="w-[200px]">
              <Plus size={16} className="mr-2" />
              Add User
            </Button>
          </Link>
        )}
      </div>

      <div className="bg-card rounded-lg border shadow-sm">
        <UserFilters 
          search={search}
          roleFilter={roleFilter}
          onSearchChange={setSearch}
          onRoleFilterChange={setRoleFilter}
          currentUserRole={user?.role}
        />

        <UserTable
          users={paginatedUsers}
          currentUser={user}
          allUsers={users}
          sortField={sortField}
          sortDirection={sortDirection}
          onSort={handleSort}
          onDelete={handleDeleteUser}
          onViewProgress={handleViewProgress}
          canManageUser={(_, targetUserId) => canManageUser(targetUserId)}
        />
        
        <UserPagination
          currentPage={currentPage}
          itemsPerPage={itemsPerPage}
          totalItems={sortedUsers.length}
          onPageChange={handlePageChange}
          onItemsPerPageChange={handleItemsPerPageChange}
        />
      </div>
    </div>
  );
};

export default UserList;
