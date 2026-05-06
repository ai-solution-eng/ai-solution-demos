import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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
import { Pencil, Trash2, Eye, MoreHorizontal } from "lucide-react";
import { User } from "@/contexts/AuthContext";

interface UserActionsMenuProps {
  user: User;
  currentUser: User;
  canManage: boolean;
  onDelete: (userId: string) => void;
  onViewProgress: (userId: string, userRole: string) => void;
}

export const UserActionsMenu = ({
  user,
  currentUser,
  canManage,
  onDelete,
  onViewProgress,
}: UserActionsMenuProps) => (
  <DropdownMenu>
    <DropdownMenuTrigger asChild>
      <Button variant="ghost" size="sm">
        <MoreHorizontal size={16} />
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent align="end">
      {/* View Progress option for new hires and mentors */}
      {(user.role === "new-hire" || user.role === "mentor") && (currentUser.role === "admin" || currentUser.role === "manager") && (
        <DropdownMenuItem onClick={() => onViewProgress(user.id, user.role)}>
          <Eye size={14} className="mr-2" />
          View Progress
        </DropdownMenuItem>
      )}
      
      {/* Edit option */}
      {(user.id === currentUser.id || canManage) && (
        <Link to={`/users/${user.id}`}>
          <DropdownMenuItem>
            <Pencil size={14} className="mr-2" />
            Edit
          </DropdownMenuItem>
        </Link>
      )}

      {/* Delete option */}
      {canManage && user.id !== currentUser.id && (
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
                This will permanently delete {user.name}'s account. This action cannot be undone.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                className="bg-destructive"
                onClick={() => onDelete(user.id)}
              >
                Delete
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      )}
    </DropdownMenuContent>
  </DropdownMenu>
);
