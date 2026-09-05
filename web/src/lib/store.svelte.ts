import {
  loadNotify, pushNotify, saveNotify, notifyPermission, requestNotifyPermission,
  type NotifySettings,
} from "./notify";
import { moveAcross, moveBefore, shiftBy } from "./tab-order";
import type {
  AppConfig,
  ChatMessage,
  SessionInfo,
  StatePanel,
  Step,
} from "./types";
import { DEFAULT_STATE, EMPTY_DRAFT, type CompactionResult, type SessionStatus, type TabState, type TurnDraft, type ViewName } from "./store/shared";
export { EMPTY_DRAFT } from "./store/shared";
export type { ViewName, TurnDraft } from "./store/shared";

import { StoreCoreBase } from "./store/core.svelte";
import { MemoryMixin } from "./store/memory.svelte";
import { SkillsMixin } from "./store/skills.svelte";
import { ToolsMixin } from "./store/tools.svelte";
import { ProvidersMixin } from "./store/providers.svelte";
import { PersonasMixin } from "./store/personas.svelte";
import { SubagentsMixin } from "./store/subagents.svelte";
import { PresetsMixin } from "./store/presets.svelte";

import { ApprovalsMixin } from "./store/approvals.svelte";
import { PluginsMixin } from "./store/plugins.svelte";
import { AppearanceMixin } from "./store/appearance.svelte";
import { EventsMixin } from "./store/events.svelte";

// all of these names from the store.
export type { CustomLayout, LayoutId, ThemePreset } from "./types";
export { BUILTIN_THEMES, PALETTE } from "./layouts/registry";



/**
 * Composition root. Concern mixins live in ./store/*.svelte.ts and are
 * composed here; the un-extracted concerns (approvals, sessions/tabs, turn
 * streaming, the event router, connection/bootstrap) are still class members
 * below. Reading order: innermost mixin first.
 *
 * `StoreCore` (store/core.svelte.ts) declares everything a mixin may rely
 * on — the rpc client plus the seams this class implements for them
 * (`activeSessionId`) and the seams the concerns implement for each other.
 *
 * Appearance wraps Plugins so `plugins` is initialised before any appearance
 * field that derives off it.
 */
export class XuBrainStore extends EventsMixin(
  AppearanceMixin(
  PluginsMixin(
    ApprovalsMixin(
      PresetsMixin(
        SubagentsMixin(
          PersonasMixin(ProvidersMixin(ToolsMixin(SkillsMixin(MemoryMixin(StoreCoreBase))))),
        ),
      ),
    ),
  ),
),
) {
  protected override get activeSessionId(): string | null {
    return this.sessionId;
  }

  connected = $state(false);
  view = $state<ViewName>("workspace");
  turns = $state(0);
  // ---- per-tab session state ----
  // Tabs (openSessionIds) each own their session, message history and live
  // streaming draft. The derived fields below expose the ACTIVE tab so the
  // components keep their existing `brain.session / messages / draft` reads.
  private sessionId = $state<string | null>(null);
  protected tabs = $state<Record<string, TabState>>({});
  session = $derived<SessionInfo | null>(
    (this.sessionId ? this.tabs[this.sessionId] : undefined)?.session ?? null,
  );
  messages = $derived<ChatMessage[]>(
    (this.sessionId ? this.tabs[this.sessionId] : undefined)?.messages ?? [],
  );
  draft = $derived<TurnDraft | null>(
    (this.sessionId ? this.tabs[this.sessionId] : undefined)?.draft ?? null,
  );
  /** Messages queued server-side while the active session's turn runs —
   * rendered above the composer until the turn picks them up. */
  queued = $derived<{ text: string; images?: string[]; id?: string; key?: string }[]>(
    (this.sessionId ? this.tabs[this.sessionId] : undefined)?.queued ?? [],
  );

  /** Whether the given open session currently has a running turn (for tab badge). */
  isBusy(id: string): boolean {
    return this.tabs[id]?.busy ?? false;
  }

  /** Whether the given session is currently running a compaction. */
  isCompressing(id?: string | null): boolean {
    return !!id && this.compressingSessions.includes(id);
  }

  state = $state<StatePanel>({ ...DEFAULT_STATE });
  /** Result of the last manual compaction (async — pushed via compaction.done). */
  compactionResult = $state<CompactionResult | null>(null);
  /** Session ids currently running a compaction (auto or manual). */
  compressingSessions = $state<string[]>([]);
  sessions = $state<SessionInfo[]>([]);
  openSessionIds = $state<string[]>([]);
  status = $state<SessionStatus>({ brain: "off" });
  config = $state<AppConfig>({
    context_length: 128000,
    compress_threshold: 60,
    approval_mode: "manual",
    job_timeout: null,
    max_parallel_subagents: 100,
    approval_modes: {},
    retry_max: 10,
    vision_model: null,
    model_fallbacks: [],
    firecrawl_enabled: false,
    firecrawl_key: null,
    retry_interval: 3,
    retain_ratio: 0.16,
    compaction_retries: 1,
    context_skill_budget: 6000,
    prune_keep: 30,
  });
  onboarded = $state(true);

  // ---- desktop notifications (Config → Notifications) ----
  /** Mirrors `notify.ts`'s localStorage record. It lives in the store rather
   *  than in ConfigView so a plugin layout can render the panel too — two
   *  components each holding their own copy would drift the moment one wrote. */
  notify = $state<NotifySettings>(loadNotify());
  notifyPerm = $state(notifyPermission());

  setNotify(patch: Partial<NotifySettings>): void {
    this.notify = { ...this.notify, ...patch };
    saveNotify(this.notify);
  }

  /** The browser only prompts from a user gesture, so this must be called from
   *  a click — not on boot. Enabling on grant is deliberate: nobody grants
   *  permission intending to leave alerts off. */
  async grantNotify(): Promise<void> {
    this.notifyPerm = await requestNotifyPermission();
    if (this.notifyPerm === "granted") this.setNotify({ enabled: true });
  }

  testNotify(): void {
    pushNotify("Xu — test", "notifications are working; click to focus Xu", "xu-test");
  }


  // ---- brand avatar (Dock mark): custom image/GIF stored as a data URL ----
  avatar = $state<string | null>(null);


  private listed = false;
  private readonly openSessionsKey = "xu.open-session-ids";
  private readonly activeSessionKey = "xu.active-session-id";
  private readonly avatarKey = "xu.avatar";

  setAvatar(dataUrl: string | null): void {
    this.avatar = dataUrl;
    try {
      if (dataUrl) localStorage.setItem(this.avatarKey, dataUrl);
      else localStorage.removeItem(this.avatarKey);
    } catch { /* storage can be unavailable */ }
  }



  constructor() {
    super();
    this.restoreAppearance();

    try {
      const savedAvatar = localStorage.getItem(this.avatarKey);
      if (savedAvatar) this.avatar = savedAvatar;
    } catch { /* storage can be unavailable */ }

    this.restoreSessionTabs();
    this.client.onStatusChange = (up) => {
      this.connected = up;
      if (up && !this.listed) {
        this.listed = true;
        this.bootstrap().catch(() => {});
      }
    };
    this.client.onEvent((event, params) => this.onEvent(event, params));
    this.client.connect();
  }

  private restoreSessionTabs(): void {
    if (typeof localStorage === "undefined") return;
    try {
      const raw = JSON.parse(localStorage.getItem(this.openSessionsKey) ?? "[]");
      if (Array.isArray(raw)) this.openSessionIds = raw.filter((id): id is string => typeof id === "string");
    } catch { /* corrupt browser state is ignored */ }
  }

  private persistSessionTabs(): void {
    if (typeof localStorage === "undefined") return;
    try {
      localStorage.setItem(this.openSessionsKey, JSON.stringify(this.openSessionIds));
      if (this.sessionId) localStorage.setItem(this.activeSessionKey, this.sessionId);
      else localStorage.removeItem(this.activeSessionKey);
    } catch { /* storage can be unavailable in private browsing */ }
  }

  private openSessionTab(id: string): void {
    if (!this.openSessionIds.includes(id)) this.openSessionIds = [...this.openSessionIds, id];
    this.persistSessionTabs();
  }

  /** Reorder the tab strip: put `dragged` immediately before `target`, or last
   *  when `target` is null. Tab order is frontend-only state, so this persists
   *  to localStorage and touches nothing on the brain. */
  moveSessionTab(dragged: string, target: string | null): void {
    const next = moveBefore(this.openSessionIds, dragged, target);
    if (next === this.openSessionIds) return;
    this.openSessionIds = next;
    this.persistSessionTabs();
  }

  /** Live reorder while a drag is in flight: swap as the pointer crosses a
   *  tab's midpoint, so the strip is already in its final order by the time the
   *  tab is released. Returns true when the order actually changed. */
  dragSessionTabOver(
    dragged: string,
    target: string,
    pointerX: number,
    rect: { left: number; width: number },
  ): boolean {
    const next = moveAcross(this.openSessionIds, dragged, target, pointerX, rect);
    if (next === this.openSessionIds) return false;
    this.openSessionIds = next;
    return true; // persisted once on drop, not on every pointer move
  }

  /** Commit the order a live drag settled on. */
  commitSessionTabs(): void {
    this.persistSessionTabs();
  }

  /** Keyboard path for reordering (Alt+←/→ on a focused tab) so the strip is
   *  not drag-only. */
  shiftSessionTab(id: string, delta: number): void {
    const next = shiftBy(this.openSessionIds, id, delta);
    if (next === this.openSessionIds) return;
    this.openSessionIds = next;
    this.persistSessionTabs();
  }

  /** Called once on first connect: fetch config, providers, detect onboarding. */
  private async bootstrap(): Promise<void> {
    await this.listSessions();
    const saved = typeof localStorage === "undefined" ? null : localStorage.getItem(this.activeSessionKey);
    const restoreId = saved && this.sessions.some((s) => s.id === saved)
      ? saved
      : this.openSessionIds.find((id) => this.sessions.some((s) => s.id === id));
    if (restoreId) {
      try { await this.openSession(restoreId); } catch { this.closeSessionTab(restoreId); }
    }
    try {
      const cfg = await this.client.call<AppConfig>("config.get");
      this.config = { ...this.config, ...cfg };
    } catch { /* config not ready yet */ }
    await this.refreshLayouts();
    try {
      await this.refreshProviders();  // also sets `onboarded` off the list length
      if (!this.onboarded) {
        this.view = "onboarding";
        try { sessionStorage.removeItem("cfg-return"); } catch { /* ignore */ }
      } else if (typeof sessionStorage !== "undefined" && sessionStorage.getItem("cfg-return")) {
        // Appearance change auto-reloaded while Config was open: land back.
        this.view = "config";
      }
    } catch { /* providers not ready */ }
    try {
      await this.refreshToolsets();
    } catch { /* tools not ready */ }
    try {
      await this.refreshMemory();
    } catch { /* memory not ready */ }
    try {
      // Plugins must load on EVERY boot, not just when Config is opened. A
      // plugin-contributed layout OR colour scheme is selected by id from
      // localStorage, and the shell resolves that id against this list — so an
      // empty list means a `plugin:*` choice cold-starts as "not enabled".
      // Kept explicit even though refreshToolsets chains it: an implicit-only
      // path is what broke before. One extra idempotent fetch per cold boot.
      await this.refreshPlugins();
    } catch { /* plugins not ready */ }
    try {
      await this.refreshPersonas();
    } catch { /* personas not ready */ }
    try {
      await this.refreshSkills();
    } catch { /* skills not ready */ }
    try {
      await this.refreshSubagents();
    } catch { /* subagents not ready */ }
  }

  private closeSessionTab(id: string): void {
    this.openSessionIds = this.openSessionIds.filter((sessionId) => sessionId !== id);
    if (this.compressingSessions.includes(id)) {
      this.compressingSessions = this.compressingSessions.filter((s) => s !== id);
    }
    this.persistSessionTabs();
  }

  protected ensureTab(id: string): TabState {
    let tab = this.tabs[id];
    if (!tab) {
      tab = { session: null, messages: [], draft: null, busy: false, queued: [] };
      this.tabs = { ...this.tabs, [id]: tab };
    }
    return tab;
  }

  /** Only tabs the user has open own live state; other sessions (e.g. subagent
   * sessions) never render here. */
  protected isOpenTab(id: string): boolean {
    return this.openSessionIds.includes(id);
  }

  protected applyDraftFor(sid: string, updater: (d: TurnDraft) => TurnDraft): void {
    const tab = this.ensureTab(sid);
    tab.draft = updater(tab.draft ?? EMPTY_DRAFT);
  }



  protected async loadSession(id: string, activate = true): Promise<void> {
    const got = await this.client.call<{
      session: SessionInfo;
      messages: ChatMessage[];
      live?: { steps?: Step[]; reasoning?: string } | null;
    }>("session.get", { id });
    const tab = this.ensureTab(id);
    tab.session = got.session;
    tab.messages = got.messages;
    // keep the tab strip / sidebar entry in sync (the server auto-titles a
    // fresh session on first send and emits session.updated for it)
    this.sessions = this.sessions.map((s) => (s.id === id ? { ...s, ...got.session } : s));
    if (!tab.draft && got.live && (got.live.steps?.length || got.live.reasoning)) {
      tab.draft = { steps: got.live.steps ?? [], reasoning: got.live.reasoning ?? "" };
    }
    // A live snapshot means a turn is still streaming on the brain. Without
    // busy=true, a shell refreshed mid-response hides the draft (isBusy gates
    // the live transcript) until the turn finishes and history reloads.
    if (got.live) tab.busy = true;
    if (activate) {
      this.sessionId = id;
      this.subagentActivityUpdate = null;
      this.subRuns = {};
    }
    this.openSessionTab(id);
    // Session-scoped panel state belongs to the ACTIVE tab only — a background
    // refresh (a turn finishing in another open tab, a subagent's ephemeral
    // session) must not overwrite what the Agent State panel shows.
    if (this.sessionId === id) {
      this.state = { ...this.state, cwd: got.session.cwd };
      if (got.session.model) this.state = { ...this.state, model: got.session.model };
      this.state = { ...this.state, persona: got.session.persona ?? null };
      // Merge everything state.get returns (rules, preset, git, context,
      // tokens...) so a load/switch reflects THIS session, not stale pushes.
      const state = await this.client.call<Partial<StatePanel>>("state.get", { session_id: id });
      this.state = { ...this.state, ...state };
      void this.refreshTodo();
    }
this.persistSessionTabs();
  }

  /** Settle a finished/failed turn: reload authoritative history from the
   *  brain, and if that reload fails, fall back to the live draft we were
   *  about to discard so the turn can never render as vanished. */
  protected finalizeTurn(id: string, failed: boolean): void {
    const tab = this.ensureTab(id);
    tab.busy = false;
    tab.queued = []; // history reload lands the real rows
    const draft = tab.draft;
    tab.draft = null;
    void this.loadSession(id, false).catch(() => {
      // The brain may be mid-rewrite (compaction + history commit both
      // replace the whole transcript in one write). If session.get failed,
      // the draft we just dropped is the only record the shell has — put it
      // back as a finished row. Only runs when the reload never landed, so
      // it can't duplicate a row that did load.
      if (!draft || !(draft.steps.length || draft.reasoning)) return;
      tab.messages = [
        ...tab.messages,
        {
          role: "assistant",
          content: draft.steps.filter((s) => s.kind === "text").map((s) => s.text ?? "").join(""),
          reasoning: draft.reasoning || undefined,
          steps: draft.steps,
        },
      ];
    });
    void failed; // reserved for failure-specific handling
  }

  /** OS notification when a turn settles — the "response done" alert. */
  protected notifyTurn(sessionId: string | null, failed: boolean, error?: unknown): void {
    const sid = sessionId ?? "";
    const s = loadNotify();
    if (!s.enabled || typeof document === "undefined") return;
    if (failed ? !s.turnFailed : !s.turnDone) return;
    // while you're actively in Xu the inline reply/error is in front of you
    if (s.onlyUnfocused && document.hasFocus()) return;
    const title = (sid && this.tabs[sid]?.session?.title) || "Xu";
    pushNotify(
      failed ? `✗ ${title}` : `✓ ${title}`,
      failed ? String(error ?? "turn failed") : "response finished",
      sid ? `turn-${sid}` : undefined,
    );
  }

  async switchSession(id: string): Promise<void> {
    // Clicking the already-active tab is also the workspace shortcut when
    // another dock view is open.
    if (id === this.sessionId) {
      this.view = "workspace";
      return;
    }
    await this.openSession(id);
  }

  closeSession(id: string): void {
    this.closeSessionTab(id);
    const next = { ...this.tabs };
    delete next[id];
    this.tabs = next;
    if (this.sessionId !== id) return;
    const fallback = this.openSessionIds.find((sessionId) => sessionId !== id);
    if (fallback) {
      void this.openSession(fallback);
    } else {
      this.sessionId = null;
      this.persistSessionTabs();
    }
  }

  setView(view: ViewName): void {
    this.view = view;
  }

  async openSession(id: string): Promise<void> {
    await this.loadSession(id);
    this.view = "workspace";
  }

  async newSession(): Promise<void> {
    const created = await this.client.call<{ session: SessionInfo }>("session.create", {
      model: this.state.model ?? undefined,
      persona: this.state.persona ?? undefined,
    });
    await this.openSession(created.session.id);
    this.turns = 0;
    // refresh the sidebar list so the new tab shows its title
    await this.listSessions();
  }

  async send(text: string, images?: string[]): Promise<void> {
    let sid = this.sessionId;
    if (!sid) {
      const created = await this.client.call<{ session: SessionInfo }>("session.create", {
        model: this.state.model ?? undefined,
        persona: this.state.persona ?? undefined,
      });
      sid = created.session.id;
      await this.loadSession(sid);
    }
    const tab = this.ensureTab(sid);
    // A send while the session is mid-turn is queued server-side (flushed at
    // the next tool round). Show it as a queued bubble above the composer
    // instead of the normal optimistic history bubble, so the timeline keeps
    // reading "messages → queued → (running turn)".
    if (tab.busy || this.draft !== null) {
      // Local correlation key: lets this send's response claim its own bubble
      // even if a concurrent queued send's response returns out of order, or
      // two sends share the same text.
      const key = crypto.randomUUID();
      tab.queued = [...tab.queued, { text, key, ...(images?.length ? { images } : {}) }];
      try {
        const res = await this.client.call<{ turn_id: string; queued_id?: string }>("session.send", {
          id: sid,
          text,
          ...(images && images.length ? { images } : {}),
        });
        // Attach the server's cancel handle to our bubble (by local key).
        if (res?.queued_id) {
          const qid = res.queued_id;
          tab.queued = tab.queued.map((q) => (q.key === key ? { ...q, id: qid } : q));
        }
      } catch (e) {
        // the RPC itself failed — pull the bubble back out of the queue
        tab.queued = tab.queued.filter((q) => q.key !== key);
        throw e;
      }
      return;
    }
    // Mirror the server's persisted content shape: text-only stays a string,
    // with images it becomes OpenAI content parts (same as session.get returns
    // after reload), so the optimistic bubble matches the stored one.
    const userContent = images?.length
      ? [
          { type: "text", text },
          ...images.map((u) => ({ type: "image_url", image_url: { url: u } })),
        ]
      : text;
    tab.messages = [...tab.messages, { role: "user", content: userContent }];
    await this.client.call("session.send", {
      id: sid,
      text,
      ...(images && images.length ? { images } : {}),
    });
  }

  async listSessions(): Promise<void> {
    const got = await this.client.call<{ sessions: SessionInfo[] }>("session.list");
    this.sessions = got.sessions;
    const available = new Set(got.sessions.map((s) => s.id));
    const validOpen = this.openSessionIds.filter((id) => available.has(id));
    if (validOpen.length !== this.openSessionIds.length) {
      this.openSessionIds = validOpen;
      this.persistSessionTabs();
    }
    // drop tab state for sessions that no longer exist server-side
    const pruned = Object.fromEntries(Object.entries(this.tabs).filter(([id]) => available.has(id)));
    if (Object.keys(pruned).length !== Object.keys(this.tabs).length) {
      this.tabs = pruned;
    }
  }

  async renameSession(id: string, title: string): Promise<void> {
    await this.client.call("session.title", { id, title });
    await this.listSessions();
    const tab = this.tabs[id];
    if (tab?.session) tab.session = { ...tab.session, title };
  }
  async deleteSession(id: string): Promise<void> {
    await this.client.call("session.delete", { id });
    this.closeSessionTab(id);
    const next = { ...this.tabs };
    delete next[id];
    this.tabs = next;
    await this.listSessions();
    if (this.sessionId === id) {
      this.sessionId = null;
      const fallback = this.openSessionIds[0];
      if (fallback) await this.openSession(fallback);
      else this.persistSessionTabs();
    }
  }

  async setRules(rules: string[]): Promise<void> {
    if (!this.sessionId) return;
    const clean = rules.map((rule) => rule.trim()).filter(Boolean);
    const got = await this.client.call<{ rules: string[] }>("state.set_rules", {
      session_id: this.sessionId,
      rules: clean,
    });
    this.state = { ...this.state, rules: got.rules };
  }

  async setPersona(persona: string | null): Promise<void> {
    await this.client.call("state.set_persona", { session_id: this.sessionId, persona });
    this.state = { ...this.state, persona };
  }

  async setCwd(cwd: string): Promise<void> {
    if (!this.sessionId) return;
    await this.client.call("workspace.set_cwd", { session_id: this.sessionId, cwd });
    this.state = { ...this.state, cwd };
  }

  async listDirs(path: string): Promise<{ path: string; dirs: string[] }> {
    return this.client.call("fs.list", { path });
  }

  async stop(): Promise<void> {
    if (!this.sessionId) return;
    await this.client.call("session.stop", { id: this.sessionId });
  }

  /** Cancel a specific queued (not yet flushed) message. The server replies
   *  with a queue.cancelled event on success, which removes the bubble.
   *  Entries the server already spliced into the live turn can't be
   *  cancelled — their bubble clears on the next turn-end event instead. */
  async cancelQueued(q: { id?: string }): Promise<void> {
    const sid = this.sessionId;
    if (!sid || !q.id) return; // not confirmed by the server yet — nothing to cancel
    await this.client.call("session.queue.cancel", { id: sid, queued_id: q.id });
  }

  /** Steer the queue: interrupt the running turn so the queued messages run
   *  now — the brain stops the turn and its chaining tail starts the first
   *  queued message as a fresh turn; the rest follow in order. */
  async steerQueue(): Promise<void> {
    const sid = this.sessionId;
    const q = this.queued.find((x) => x.id);
    if (!sid || !q?.id) return; // nothing confirmed by the server yet
    await this.client.call("session.queue.steer", { id: sid, queued_id: q.id });
  }

  async compressSession(): Promise<{ ok: boolean; async?: boolean; error?: string }> {
    if (!this.sessionId) return { ok: false, error: "no session" };
    // Fire-and-forget: the brain runs compaction off the request path and
    // pushes the result via `compaction.done`, then `session.updated` reloads
    // the transcript. Awaiting the full result here would freeze navigation.
    return await this.client.call<{ ok: boolean; async?: boolean; error?: string }>(
      "session.compress",
      { id: this.sessionId },
    );
  }

  /** All approval modes in cycle order: built-ins first, then custom (sorted). */
  get approvalModeCycle(): string[] {
    return ["manual", "yolo", ...Object.keys(this.config.approval_modes ?? {}).sort()];
  }

  /** Composer chip: step to the next available approval mode (wraps around). */
  async cycleApprovalMode(): Promise<void> {
    const cycle = this.approvalModeCycle;
    const i = cycle.indexOf(this.config.approval_mode);
    const next = cycle[(i + 1) % cycle.length] ?? "manual";
    await this.saveConfig("approval_mode", next);
  }

  /** Flip between yolo and manual — the single-button approval control. */
  async toggleYolo(): Promise<void> {
    await this.setApprovalMode(this.config.approval_mode === "yolo" ? "manual" : "yolo");
  }

  /** Set the approval mode explicitly (segmented control — no toggle ambiguity:
   * clicking the already-active mode is a no-op instead of flipping it). */
  async setApprovalMode(mode: string): Promise<void> {
    if (this.config.approval_mode === mode) return;
    await this.saveConfig("approval_mode", mode);
  }




  async refreshConfig(): Promise<void> {
    const cfg = await this.client.call<AppConfig>("config.get");
    this.config = { ...this.config, ...cfg };
  }

  async saveConfig(key: string, value: unknown): Promise<void> {
    await this.client.call("config.set", { key, value });
    if (key === "approval_mode") this.config.approval_mode = String(value);
    if (key === "context_length") this.config.context_length = Number(value);
    if (key === "approval_modes") this.config.approval_modes = (value ?? {}) as AppConfig["approval_modes"];
    if (key === "compress_threshold") this.config.compress_threshold = Number(value);
    if (key === "retain_ratio") this.config.retain_ratio = Number(value);
    if (key === "compaction_retries") this.config.compaction_retries = Number(value);
    if (key === "context_skill_budget") this.config.context_skill_budget = Number(value);
    if (key === "job_timeout") this.config.job_timeout = value == null || value === "" ? null : Number(value);
    if (key === "max_parallel_subagents") this.config.max_parallel_subagents = Number(value);
    if (key === "vision_model") this.config.vision_model = value ? String(value) : null;
    if (key === "model_fallbacks") this.config.model_fallbacks = (value ?? []) as string[];
    if (key === "firecrawl_enabled") this.config.firecrawl_enabled = Boolean(value);
    if (key === "firecrawl_key") this.config.firecrawl_key = value ? String(value) : null;
    if (key === "prune_keep") this.config.prune_keep = Number(value);
  }


}


export const brain = new XuBrainStore();
