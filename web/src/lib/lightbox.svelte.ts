/** Click-to-zoom lightbox state — shared by every image in the transcript.
 *  Module-level so any image (tool chip, attachment thumbnail, sub-agent
 *  activity) can open it without prop drilling through the shells. */
export const lightbox = $state({ src: "", alt: "" });

export function openImage(src: string, alt = ""): void {
  lightbox.src = src;
  lightbox.alt = alt;
}

export function closeImage(): void {
  lightbox.src = "";
  lightbox.alt = "";
}