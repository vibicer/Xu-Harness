/** Desktop notifications (Config → Notifications) — frontend-only module.
 *
* The brain already pushes `turn.finished` / `turn.failed` over
 * the WebSocket; this turns them into OS notifications via the Web
 * Notifications API so Xu can ping you while you're elsewhere (the tab just
 * needs to stay open — background tabs notify fine). Settings persist in
 * localStorage next to the theme, like the Themes module.
 */

export interface NotifySettings {
  /** master switch — nothing goes out unless this is on AND permission granted */
  enabled: boolean;
  /** agent response finished (suppressed while Xu has focus, see onlyUnfocused) */
  turnDone: boolean;
  /** a turn failed with an error */
  turnFailed: boolean;
  /** the agent is waiting for your input (approval card or ask question) */
  awaitInput: boolean;
  /** suppress response notifications while the Xu window has focus */
  onlyUnfocused: boolean;
  /** let the browser play its notification sound */
  sound: boolean;
}

const KEY = "xu-notify";

export const DEFAULT_NOTIFY: NotifySettings = {
  enabled: false,
  turnDone: true,
  turnFailed: true,
  awaitInput: true,
  onlyUnfocused: true,
  sound: false,
};

export function notifySupported(): boolean {
  return typeof Notification !== "undefined";
}

export function notifyPermission(): NotificationPermission | "unsupported" {
  if (!notifySupported()) return "unsupported";
  return Notification.permission;
}

export async function requestNotifyPermission(): Promise<NotificationPermission | "unsupported"> {
  if (!notifySupported()) return "unsupported";
  try {
    return await Notification.requestPermission();
  } catch {
    return Notification.permission;
  }
}

export function loadNotify(): NotifySettings {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULT_NOTIFY };
    return { ...DEFAULT_NOTIFY, ...(JSON.parse(raw) as Partial<NotifySettings>) };
  } catch {
    return { ...DEFAULT_NOTIFY };
  }
}

export function saveNotify(settings: NotifySettings): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(settings));
  } catch { /* storage can be unavailable */ }
}

/** Fire one OS notification if enabled + permitted. Clicking focuses Xu. */
export function pushNotify(title: string, body: string, tag?: string): void {
  const s = loadNotify();
  if (!s.enabled || !notifySupported() || Notification.permission !== "granted") return;
  try {
    const n = new Notification(title, { body, tag, silent: !s.sound });
    if (s.sound) {
      const audio = new Audio("/audio/notification-ding.mp3");
      audio.volume = 1;
      void audio.play().catch(() => { /* autoplay policy or unavailable asset */ });
    }
    n.onclick = () => {
      window.focus();
      n.close();
    };
  } catch { /* some platforms refuse notifications from background tabs */ }
}
