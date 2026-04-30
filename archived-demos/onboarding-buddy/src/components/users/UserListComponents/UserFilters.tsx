import { Search } from "lucide-react";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { UserFiltersProps } from "@/types";

export const UserFilters = ({
  search,
  roleFilter,
  onSearchChange,
  onRoleFilterChange,
  currentUserRole,
}: UserFiltersProps) => (
  <div className="flex flex-col gap-4 p-4 md:flex-row md:items-center md:justify-between">
    <div className="relative w-full md:max-w-sm">
      <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
      <Input
        placeholder="Search users..."
        className="pl-8"
        value={search}
        onChange={(e) => onSearchChange(e.target.value)}
      />
    </div>

    <div className="flex items-center gap-2">
      <span className="text-sm text-muted-foreground">Role:</span>
      <Select
        value={roleFilter || "all"}
        onValueChange={(value) => onRoleFilterChange(value === "all" ? null : value)}
      >
        <SelectTrigger className="w-[150px]">
          <SelectValue placeholder="All Roles" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All Roles</SelectItem>
          {currentUserRole === 'admin' && <SelectItem value="admin">Admin</SelectItem>}
          <SelectItem value="manager">Manager</SelectItem>
          <SelectItem value="mentor">Mentor</SelectItem>
          <SelectItem value="new-hire">New Hire</SelectItem>
        </SelectContent>
      </Select>
    </div>
  </div>
);
