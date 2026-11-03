import { type RefObject, useCallback, useEffect, useRef } from "react";

// Keeps a scrolling list on its newest entry while the reader is at, or near, the bottom; once they
// scroll up to read, it leaves them there. It scrolls only that list: scrollIntoView would also
// scroll the page, which then cannot be scrolled back.
//
// Returns the list's onScroll handler.
export function useStickToBottom<T extends HTMLElement>(
  ref: RefObject<T | null>,
  trigger: unknown,
): () => void {
  const atBottom = useRef(true);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (el) atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }, [ref]);

  useEffect(() => {
    const el = ref.current;
    if (el && atBottom.current) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [ref, trigger]);

  return onScroll;
}
