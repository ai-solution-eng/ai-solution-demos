
import React from "react";
import { Card } from "@/components/ui/card";
import { TaskStatus } from "@/types/tasks";

interface TaskStatusCardProps {
  title: string;
  count: number;
  icon: React.ReactNode;
  status: TaskStatus;
}

const TaskStatusCard: React.FC<TaskStatusCardProps> = ({ 
  title, 
  count, 
  icon,
  status
}) => {
  return (
    <Card className="border hover:shadow transition-shadow">
      <div className="flex items-center p-4">
        <div className={`w-10 h-10 rounded-full bg-status-${status} flex items-center justify-center mr-3`}>
          {icon}
        </div>
        <div>
          <p className="text-sm font-medium">{title}</p>
          <p className="text-2xl font-bold">{count}</p>
        </div>
      </div>
    </Card>
  );
};

export default TaskStatusCard;
