/** Shared attachment helpers for composer inputs.
 *
 * Images are read as data URLs (sent to the API as image_url parts).
 * Text files (.txt/.md/code) are read as UTF-8 text and spliced into the
 * composer as a fenced block, so any provider can consume them.
 */

const TEXT_EXT =
  /\.(txt|md|markdown|json|ya?ml|toml|ini|cfg|csv|tsv|log|py|js|jsx|ts|tsx|svelte|vue|html|htm|css|scss|sh|bash|zsh|fish|rs|go|java|kt|c|h|cpp|hpp|cs|rb|php|sql|xml|svg|diff|patch|env|gitignore|dockerfile)$/i;

const MAX_TEXT_BYTES = 512 * 1024; // 512KB guard for text splice
const MAX_IMAGE_BYTES = 8 * 1024 * 1024; // 8MB guard per image

export interface AttachResult {
  /** data URLs of chosen images */
  images: string[];
  /** text blocks to splice into the composer, one per chosen text file */
  texts: string[];
  /** names of files that were skipped (unsupported/too large) */
  skipped: string[];
}

export function isTextFile(file: File): boolean {
  if (file.type.startsWith("text/")) return true;
  if (/json|yaml|xml|javascript|typescript|x-sh|x-python/.test(file.type)) return true;
  return TEXT_EXT.test(file.name);
}

/** Read the picked FileList: images → data URLs, text files → fenced blocks. */
export async function readAttachments(files: FileList | null): Promise<AttachResult> {
  const out: AttachResult = { images: [], texts: [], skipped: [] };
  if (!files) return out;
  for (const f of files) {
    try {
      if (f.type.startsWith("image/")) {
        if (f.size > MAX_IMAGE_BYTES) { out.skipped.push(f.name); continue; }
        out.images.push(await readAsDataUrl(f));
      } else if (isTextFile(f)) {
        if (f.size > MAX_TEXT_BYTES) { out.skipped.push(f.name); continue; }
        const text = await readAsText(f);
        if (text.trim()) {
          const lang = guessLang(f.name);
          out.texts.push(`\n[${f.name}]\n\`\`\`${lang}\n${text}\n\`\`\`\n`);
        }
      } else {
        out.skipped.push(f.name);
      }
    } catch {
      out.skipped.push(f.name);
    }
  }
  return out;
}

function readAsDataUrl(f: File): Promise<string> {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(typeof r.result === "string" ? r.result : "");
    r.onerror = () => rej(r.error);
    r.readAsDataURL(f);
  });
}

function readAsText(f: File): Promise<string> {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(typeof r.result === "string" ? r.result : "");
    r.onerror = () => rej(r.error);
    r.readAsText(f);
  });
}

function guessLang(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return ext || "";
}
