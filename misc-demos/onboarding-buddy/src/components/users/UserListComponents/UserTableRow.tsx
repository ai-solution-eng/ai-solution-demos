import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { TableCell } from "@/components/ui/table";
import { User } from "@/contexts/AuthContext";
import { UserActionsMenu } from "./UserActionsMenu";
import { getRoleBadgeVariant } from "@/lib/user-utils";
import { UserRowProps } from "@/types";
import { Link } from "react-router-dom";

export const UserTableRow = ({
  user,
  currentUser,
  manager,
  onDelete,
  onViewProgress,
  canManage,
}: UserRowProps) => {
  const { className: badgeVariant } = getRoleBadgeVariant(user.role);

  return (
    <>
      <TableCell className="font-medium">
        <div className="flex items-center space-x-3">
          <Link to={`/users/${user.id}`} className="flex items-center space-x-3 hover:underline">
            <Avatar className="h-8 w-8">
              <AvatarImage src={user.avatar} alt={user.name} />
              <AvatarFallback>{user.name.charAt(0)}</AvatarFallback>
            </Avatar>
            <span className="hover:text-primary">{user.name}</span>
          </Link>
        </div>
      </TableCell>
      <TableCell>{user.email}</TableCell>
      <TableCell>
        <Badge variant="outline" className={badgeVariant}>
          {user.role.charAt(0).toUpperCase() + user.role.slice(1)}
        </Badge>
      </TableCell>
      <TableCell>{manager?.name || ""}</TableCell>
      <TableCell>
        <UserActionsMenu
          user={user}
          currentUser={currentUser}
          canManage={canManage}
          onDelete={onDelete}
          onViewProgress={onViewProgress}
        />
      </TableCell>
    </>
  );
};
