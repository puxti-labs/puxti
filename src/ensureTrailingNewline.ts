/** Ensure text ends with a single trailing newline. */
export function ensureTrailingNewline(text: string): string {
  if (text.length === 0) return "\n";
  return text.endsWith("\n") ? text : text + "\n";
}
