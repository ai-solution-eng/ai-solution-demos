import { User } from "@/contexts/AuthContext";

export interface UserTableProps {
  users: User[];
  currentUser: User;
  allUsers: User[];
  sortField: string | null;
  sortDirection: 'asc' | 'desc' | null;
  onSort: (field: string) => void;
  onDelete: (userId: string) => void;
  onViewProgress: (userId: string, userRole: string) => void;
  canManageUser: (currentUser: User, targetUserId: string) => boolean;
}

export interface UserRowProps {
  user: User;
  currentUser: User;
  manager?: User;
  onDelete: (userId: string) => void;
  onViewProgress: (userId: string, userRole: string) => void;
  canManage: boolean;
  canManageUser: (currentUser: User, targetUserId: string) => boolean;
}

export interface UserFiltersProps {
  search: string;
  roleFilter: string | null;
  onSearchChange: (value: string) => void;
  onRoleFilterChange: (value: string | null) => void;
  currentUserRole?: 'admin' | 'manager' | 'mentor' | 'new-hire';
}

export interface UserPaginationProps {
  currentPage: number;
  itemsPerPage: number;
  totalItems: number;
  onPageChange: (page: number) => void;
  onItemsPerPageChange: (value: number) => void;
}
