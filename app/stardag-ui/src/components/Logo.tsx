interface LogoProps {
  size?: "sm" | "md" | "lg" | "xl";
  showText?: boolean;
  className?: string;
}

const sizeClasses = {
  sm: "text-lg",
  md: "text-2xl",
  lg: "text-4xl",
  xl: "text-6xl",
};

/**
 * Stardag logo component using IBM Plex Mono font.
 * Displays "*dag" or just "*" depending on showText prop.
 */
export function Logo({
  size = "md",
  showText = true,
  className = "",
}: LogoProps) {
  return (
    <span
      className={`font-mono font-medium select-none ${sizeClasses[size]} ${className}`}
      style={{ fontFamily: "'IBM Plex Mono', monospace" }}
    >
      *{showText && "dag"}
    </span>
  );
}

/**
 * Just the asterisk icon for compact displays.
 */
export function LogoIcon({
  size = "md",
  className = "",
}: Omit<LogoProps, "showText">) {
  return <Logo size={size} showText={false} className={className} />;
}
