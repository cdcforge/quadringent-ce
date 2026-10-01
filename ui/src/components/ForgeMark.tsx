export function ForgeMark({ size = 30 }: { readonly size?: number }) {
  return (
    <svg
      className="forge-mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M4.5 8.5h8.75c3.7 0 5.75 2.35 5.75 5.75v3.5c0 3.4 2.05 5.75 5.75 5.75h2.75" />
      <path d="M4.5 23.5h7.75c3.7 0 5.75-2.35 5.75-5.75v-3.5c0-3.4 2.05-5.75 5.75-5.75h3.75" />
      <circle cx="27.5" cy="23.5" r="2.25" />
    </svg>
  );
}
