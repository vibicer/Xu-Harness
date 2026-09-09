import { BrainClient } from "../rpc";
import type { PluginInfo, StatePanel } from "../types";
import type { TabState, TurnDraft } from "./shared";


/** Constructor shape a concern mixin accepts as its base. */
export type Ctor<T = object> = new (...args: any[]) => T;

/**
 * The contract between the composition root (`store.svelte.ts`) and the
 * concern mixins in this folder — and nothing else. A mixin may rely on the
 * members below plus its own state; anything more must be added here
 * deliberately, which is the point: this file IS the list of couplings.
 *
 * `client` is real and lives here — every extracted concern is a wrapper over
 * it. The rest are seams owned by ANOTHER concern's mixin, or by a concern that
 * has not been extracted yet, so one can reach the other without importing it:
 *
 *   state                — active session's panel state (sessions concern)
 *   onboarded            — provider-presence flag read by bootstrap/view logic
 *   plugins              — the plugin list (plugins concern); appearance derives
 *                          `pluginThemes` off it
 *   activeSessionId      — the sessions/tabs concern owns the private `sessionId`
 *   refreshPlugins       — plugins concern; `refreshToolsets` chains it
 *   applyAppearance      — appearance concern; adopting a plugin list repaints
 *   reconcilePluginTheme — appearance concern; adopting a plugin list must fall
 *                          back off a scheme the list no longer contains
 *
 * Every mixin constrains its Base to `Ctor<StoreCoreBase>` — NOT
 * `Ctor<StoreCore>` — because the seam methods are `protected` (kept off the
 * store's public surface, pinned by checks/store-surface.test.ts) and a plain
 * interface erases protected members, so the type system would not see them
 * through the mixin factory's generic. `StoreCoreBase`'s own instance type
 * carries the seams with their protected visibility, and subclass access via
 * `this` inside a mixin is legal.
 *
 * `declare` fields are type-only and emit nothing. The seam methods throw:
 * reaching one means the composed store stopped overriding it, which is a
 * wiring bug, not something to paper over with a default.
 */
export interface StoreCore {
  readonly client: BrainClient;
  state: StatePanel;
  onboarded: boolean;
  plugins: PluginInfo[];
}

export class StoreCoreBase implements StoreCore {
  readonly client = new BrainClient();

  /** Active session's panel state (model, persona, rules, preset, ...). */
  declare state: StatePanel;
  /** False until at least one provider exists (drives the onboarding view). */
  declare onboarded: boolean;
  /** Live `plugin.list` snapshot. The plugins concern owns it; the appearance
   *  concern derives the plugin-contributed colour schemes off it. */
  declare plugins: PluginInfo[];

  /** Id of the active session tab, or null when no tab is open. Protected:
   *  consumed by mixins (providers, subagents, presets), not by components. */
  protected get activeSessionId(): string | null {
    throw new Error("StoreCore.activeSessionId: not implemented by XuBrainStore");
  }

  /** Re-fetch `plugin.list` and reconcile the active colour scheme. Owned by
   *  the plugins concern; declared here because `refreshToolsets` chains it. */
  protected refreshPlugins(): Promise<void> {
    throw new Error("StoreCore.refreshPlugins: not implemented by XuBrainStore");
  }

  /** Repaint <html> for the active layout + colour scheme. Owned by the
   *  appearance concern; declared here because adopting a plugin list has to
   *  repaint (a plugin can contribute the active scheme). */
  protected applyAppearance(): void {
    throw new Error("StoreCore.applyAppearance: not implemented by XuBrainStore");
  }

  /** Fall back off a plugin-contributed scheme that just disappeared. Owned by
   *  the appearance concern (it owns `theme`); called by the plugins concern,
   *  which is the only thing that knows the list changed. */
  protected reconcilePluginTheme(): void {
    throw new Error("StoreCore.reconcilePluginTheme: not implemented by XuBrainStore");
  }
  protected ensureTab(_id: string): TabState {
    throw new Error("StoreCore.ensureTab: not implemented by XuBrainStore");
  }
  protected isOpenTab(_id: string): boolean {
    throw new Error("StoreCore.isOpenTab: not implemented by XuBrainStore");
  }
  protected applyDraftFor(_sid: string, _updater: (d: TurnDraft) => TurnDraft): void {
    throw new Error("StoreCore.applyDraftFor: not implemented by XuBrainStore");
  }
  protected dismissSessionApprovals(_sid: string): void {
    throw new Error("StoreCore.dismissSessionApprovals: not implemented by XuBrainStore");
  }
  protected notifyTurn(_sessionId: string | null, _failed: boolean, _error?: unknown): void {
    throw new Error("StoreCore.notifyTurn: not implemented by XuBrainStore");
  }
  protected notifyAwaiting(_sessionId: string | null | undefined, _summary: string): void {
    throw new Error("StoreCore.notifyAwaiting: not implemented by XuBrainStore");
  }
  protected finalizeTurn(_id: string, _failed: boolean): void {
    throw new Error("StoreCore.finalizeTurn: not implemented by XuBrainStore");
  }
  protected loadSession(_sid: string, _activate = true): Promise<void> {
    throw new Error("StoreCore.loadSession: not implemented by XuBrainStore");
  }
}
