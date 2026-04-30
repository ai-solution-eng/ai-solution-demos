import { Star } from "lucide-react";
import { cn } from "@/lib/utils";

interface StarRatingProps {
  value: number;
  max?: number;
  className?: string;
  size?: number;
}

export function StarRating({ value, max = 5, className, size = 16 }: StarRatingProps) {
  return (
    <div className={cn("flex items-center", className)}>
      {[...Array(max)].map((_, i) => (
        <Star
          key={i}
          size={size}
          className={cn(
            i < value ? "fill-yellow-400 text-yellow-400" : "text-muted-foreground/30",
            "mr-0.5"
          )}
        />
      ))}
      <span className="ml-1 text-sm text-muted-foreground">{value}/{max}</span>
    </div>
  );
}
