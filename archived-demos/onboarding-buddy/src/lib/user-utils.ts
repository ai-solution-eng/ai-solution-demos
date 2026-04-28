import { User } from "@/contexts/AuthContext";

export const filterVisibleUsers = (users: User[], currentUser: User, search: string, roleFilter: string | null) => {
  return users.filter((user) => {
    // Visibility rules
    const isVisibleForRole =
      currentUser.role === "admin" ||
      (currentUser.role === "manager" && (user.id === currentUser.id || user.managerId === currentUser.id)) ||
      (["mentor", "new-hire"].includes(currentUser.role) && user.id === currentUser.id);

    // Search filter
    const matchesSearch =
      user.name.toLowerCase().includes(search.toLowerCase()) ||
      user.email.toLowerCase().includes(search.toLowerCase()) ||
      user.role.toLowerCase().includes(search.toLowerCase());

    // Role filter
    const matchesRoleFilter = !roleFilter || user.role === roleFilter;

    return isVisibleForRole && matchesSearch && matchesRoleFilter;
  });
};

export const sortUsers = (users: User[], field: string | null, direction: 'asc' | 'desc' | null, allUsers: User[] = []) => {
  if (!field || !direction) return [...users];
  
  return [...users].sort((a, b) => {
    if (field === 'name' || field === 'email' || field === 'role') {
      return direction === 'asc' 
        ? a[field].localeCompare(b[field]) 
        : b[field].localeCompare(a[field]);
    } else if (field === 'manager') {
      const getManagerName = (user: User) => {
        const manager = allUsers.find(u => u.id === user.managerId);
        return manager ? manager.name : '';
      };
      
      const aManager = getManagerName(a);
      const bManager = getManagerName(b);
      
      return direction === 'asc'
        ? aManager.localeCompare(bManager)
        : bManager.localeCompare(aManager);
    }
    return 0;
  });
};

export const getPaginationRange = (currentPage: number, itemsPerPage: number, totalItems: number) => {
  const startIndex = (currentPage - 1) * itemsPerPage + 1;
  const endIndex = Math.min(currentPage * itemsPerPage, totalItems);
  return { startIndex, endIndex };
};

export const getRoleBadgeVariant = (role: string) => {
  switch (role) {
    case 'admin':
      return { className: 'bg-red-100 text-red-800 hover:bg-red-100' };
    case 'manager':
      return { className: 'bg-purple-100 text-purple-800 hover:bg-purple-100' };
    case 'mentor':
      return { className: 'bg-blue-100 text-blue-800 hover:bg-blue-100' };
    default:
      return { className: 'bg-gray-100 text-gray-800 hover:bg-gray-100' };
  }
};
