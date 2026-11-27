import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium leading-none transition-colors",
  {
    variants: {
      variant: {
        default: "border-[var(--border)] bg-[var(--muted)] text-[var(--foreground)]",
        outline: "border-[var(--border)] bg-transparent text-[var(--muted-foreground)]",
        accent: "border-[var(--accent)]/30 bg-[var(--accent-soft)] text-[var(--accent)]",
        success: "border-[var(--success)]/30 bg-[var(--success-soft)] text-[var(--success)]",
        warning: "border-[var(--warning)]/30 bg-[var(--warning-soft)] text-[var(--warning)]",
        destructive: "border-[var(--destructive)]/30 bg-[var(--destructive-soft)] text-[var(--destructive)]",
      },
    },
    defaultVariants: { variant: "default" },
  }
)

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement>, VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}
