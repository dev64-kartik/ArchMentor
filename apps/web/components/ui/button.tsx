import { forwardRef } from "react";
import type { ButtonHTMLAttributes } from "react";

import { cn } from "@/lib/utils/cn";

type ButtonVariant = "primary" | "secondary" | "ghost";
type ButtonSize = "sm" | "md" | "lg";

const VARIANT_STYLES: Record<ButtonVariant, string> = {
  primary:
    "bg-neutral-900 text-white hover:bg-neutral-800 dark:bg-neutral-100 dark:text-neutral-900 dark:hover:bg-neutral-200",
  secondary:
    "border border-neutral-300 bg-white hover:bg-neutral-100 dark:border-neutral-700 dark:bg-neutral-900 dark:hover:bg-neutral-800",
  ghost: "hover:bg-neutral-100 dark:hover:bg-neutral-800",
};

const SIZE_STYLES: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-sm",
  md: "h-10 px-4 text-sm",
  lg: "h-11 px-6 text-base",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = "primary", size = "md", type = "button", ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cn(
        "inline-flex items-center justify-center rounded-md font-medium",
        "transition-colors focus-visible:outline-none focus-visible:ring-2",
        "focus-visible:ring-neutral-400 disabled:pointer-events-none disabled:opacity-50",
        VARIANT_STYLES[variant],
        SIZE_STYLES[size],
        className,
      )}
      {...props}
    />
  );
});
