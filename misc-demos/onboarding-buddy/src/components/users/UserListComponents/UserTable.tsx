import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ChevronDown, ChevronUp } from "lucide-react";
import { User } from "@/contexts/AuthContext";
import { UserTableRow } from "./UserTableRow";
import { UserTableProps } from "@/types";

export const UserTable = ({
  users,
  currentUser,
  allUsers,
  sortField,
  sortDirection,
  onSort,
  onDelete,
  onViewProgress,
  canManageUser,
}: UserTableProps) => {
  const getSortIcon = (field: string) => {
    if (sortField !== field) return null;
    return sortDirection === 'asc' ? <ChevronUp size={16} /> : <ChevronDown size={16} />;
  };

  return (
    <div className="rounded-md border">
      <Table className="table-fixed w-full">
        <TableHeader>
          <TableRow>
            <TableHead 
              className="cursor-pointer w-[250px] whitespace-nowrap" 
              onClick={() => onSort('name')}
            >
              <div className="flex items-center">
                Name
                {getSortIcon('name')}
              </div>
            </TableHead>
            <TableHead 
              className="cursor-pointer w-[300px] whitespace-nowrap" 
              onClick={() => onSort('email')}
            >
              <div className="flex items-center">
                Email
                {getSortIcon('email')}
              </div>
            </TableHead>
            <TableHead 
              className="cursor-pointer w-[100px] whitespace-nowrap" 
              onClick={() => onSort('role')}
            >
              <div className="flex items-center">
                Role
                {getSortIcon('role')}
              </div>
            </TableHead>
            <TableHead 
              className="cursor-pointer w-[250px] whitespace-nowrap" 
              onClick={() => onSort('manager')}
            >
              <div className="flex items-center">
                Manager
                {getSortIcon('manager')}
              </div>
            </TableHead>
            <TableHead className="w-[150px] w-[70px] whitespace-nowrap">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {users.length > 0 ? (
            users.map((user) => {
              const manager = allUsers.find((u) => u.id === user.managerId);
              const canManage = canManageUser(currentUser, user.id);
              
              return (
                <TableRow key={user.id}>
                  <UserTableRow
                    user={user}
                    currentUser={currentUser}
                    manager={manager}
                    onDelete={onDelete}
                    onViewProgress={onViewProgress}
                    canManage={canManage}
                    canManageUser={canManageUser}
                  />
                </TableRow>
              );
            })
          ) : (
            <TableRow>
              <TableCell colSpan={5} className="text-center py-6">
                No users found
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
};
